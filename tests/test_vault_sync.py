"""Regression tests for the HuggingFace source-vault sync workflow."""

# Test modules legitimately exercise private helpers; production keeps the
# protected-access check enabled.
# pylint: disable=protected-access

import importlib.util
import io
import json
import struct
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_module(name, relative_path):
    """Loads a repository module by file path, avoiding static imports."""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VAULT = _load_module("builder_vault_sync", "vault_sync.py")
APK_MANIFEST = _load_module("builder_apk_manifest", "core/apk_manifest.py")
CONTEXT = _load_module("builder_context", "core/context.py")


def _utf8_length(value: int) -> bytes:
    """Encodes a ResStringPool UTF-8 length prefix."""
    if value > 0x7F:
        return bytes([0x80 | (value >> 8), value & 0xFF])
    return bytes([value])


def _axml_string_pool(strings):
    """Builds a UTF-8 ResStringPool chunk for the test manifest."""
    encoded = [value.encode("utf-8") for value in strings]
    string_data = b""
    offsets = []
    for value, raw in zip(strings, encoded):
        offsets.append(len(string_data))
        string_data += (
            _utf8_length(len(value))
            + _utf8_length(len(raw))
            + raw
            + b"\x00"
        )

    table_size = 28 + (4 * len(strings))
    strings_start = (table_size + 3) // 4 * 4
    chunk_size = (strings_start + len(string_data) + 3) // 4 * 4

    chunk = struct.pack("<HHI", 0x0001, 28, chunk_size)
    chunk += struct.pack(
        "<IIIII", len(strings), 0, 0x0100, strings_start, 0
    )
    chunk += b"".join(struct.pack("<I", offset) for offset in offsets)
    chunk += b"\x00" * (strings_start - len(chunk))
    chunk += string_data
    chunk += b"\x00" * (chunk_size - len(chunk))
    return chunk


def _axml_manifest(version):
    """Builds a minimal binary AndroidManifest.xml with one versionName."""
    strings = [
        "manifest",
        "versionName",
        version,
        "android",
        "http://schemas.android.com/apk/res/android",
    ]
    tag_index = strings.index("manifest")
    version_name_index = strings.index("versionName")
    version_value_index = strings.index(version)
    android_index = strings.index("android")

    attribute = struct.pack(
        "<III", android_index, version_name_index, 0xFFFFFFFF
    )
    attribute += struct.pack("<HBBI", 8, 0, 0x03, version_value_index)
    extension = struct.pack(
        "<IIHHHHHH",
        0xFFFFFFFF,
        tag_index,
        20,
        20,
        1,
        0,
        0,
        0,
    )
    element_size = 16 + len(extension) + len(attribute)
    element = struct.pack(
        "<HHIII", 0x0102, 16, element_size, 1, 0xFFFFFFFF
    )
    element += extension + attribute

    pool = _axml_string_pool(strings)
    total_size = 8 + len(pool) + len(element)
    header = struct.pack("<HHI", 0x0003, 8, total_size)
    return header + pool + element


def _make_zip(members):
    """Builds an in-memory ZIP archive from a member mapping."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _make_apk(version):
    """Builds a minimal APK-like ZIP containing an AndroidManifest.xml."""
    return _make_zip({
        "AndroidManifest.xml": _axml_manifest(version),
        "classes.dex": b"dex\n035\x00",
    })


def test_vault_filename_matches_tier0_sanitizer():
    """Tier 0 and the uploader must agree on slash/colon handling."""
    name = CONTEXT.vault_filename(
        "com.microsoft.teams", "1416/1.0.0.2026152302", ".apk"
    )
    assert name == "com.microsoft.teams_1.0.0.2026152302.apk"

    name = CONTEXT.vault_filename(
        "com.example.app", "2026.11-10189:RELEASE", ".apks"
    )
    assert name == "com.example.app_2026.11-10189_RELEASE.apks"


def test_versions_match_accepts_suffix_base_version():
    """APKMirror's numeric base must still match Build/Stable suffixes."""
    assert VAULT.versions_match("351.11", ["351.11 - Stable"])
    assert VAULT.versions_match("2.75.0", ["2.75.0 Build 3"])
    assert VAULT.versions_match("v1.2.3", ["1.2.3"])


def test_versions_match_rejects_slash_tail_only():
    """A slash version must not accept its tail as a different build."""
    target = ["1416/1.0.0.2026152302"]
    assert VAULT.versions_match("1416/1.0.0.2026152302", target)
    assert not VAULT.versions_match("1.0.0.2026152302", target)


def test_versions_match_handles_channel_and_build_suffixes():
    """Channel labels are metadata, but a different build number is not."""
    target = ["2026.11-10189:RELEASE"]
    assert VAULT.versions_match("2026.11-10189:RELEASE", target)
    assert VAULT.versions_match("2026.11-10189", target)
    assert VAULT.versions_match("2026.11", target)
    assert not VAULT.versions_match("2026.11-10188", target)


def test_apk_manifest_reader_extracts_plain_apk_version(tmp_path):
    """A plain APK manifest is parsed without third-party APK libraries."""
    apk_path = tmp_path / "sample.apk"
    apk_path.write_bytes(_make_apk("8.10.0"))
    assert APK_MANIFEST.read_version_name(str(apk_path)) == "8.10.0"


def test_apk_manifest_reader_finds_base_apk_in_bundle(tmp_path):
    """Split bundles are inspected through their base APK."""
    bundle_path = tmp_path / "sample.apks"
    bundle_path.write_bytes(_make_zip({
        "split_config.arm64_v8a.apk": _make_apk("8.10.0"),
        "base.apk": _make_apk("8.10.0"),
    }))
    assert APK_MANIFEST.read_version_name(str(bundle_path)) == "8.10.0"


def test_apk_manifest_reader_returns_none_for_garbage(tmp_path):
    """Malformed containers are rejected instead of returning a guess."""
    bad_path = tmp_path / "broken.apk"
    bad_path.write_bytes(b"not a zip")
    assert APK_MANIFEST.read_version_name(str(bad_path)) is None


def _write_ecosystem(root, ecosystem, toggles, apps):
    """Creates the minimal config/ecosystem pair used by plan tests."""
    config_dir = root / "config"
    ecosystem_dir = root / "ecosystem"
    config_dir.mkdir(parents=True, exist_ok=True)
    ecosystem_dir.mkdir(parents=True, exist_ok=True)

    lines = ["[apps]"]
    for key, enabled in toggles.items():
        lines.append(f'"{key}" = {"true" if enabled else "false"}')
    (config_dir / f"apps-{ecosystem}.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    (ecosystem_dir / f"{ecosystem}.json").write_text(
        json.dumps({ecosystem: {"apps": apps}}, indent=2),
        encoding="utf-8",
    )


def _app(package, stable, beta=None, **extra):
    """Builds one ecosystem app entry."""
    value = {
        "package": package,
        "stable": stable,
        "beta": beta or [],
        "archive_id": "ZeNiX",
    }
    value.update(extra)
    return value


def test_build_plans_respects_toml_and_version_selection(tmp_path):
    """Only enabled TOML apps with a concrete version become plans."""
    _write_ecosystem(
        tmp_path,
        "eco",
        {"enabled-a": True, "disabled": False, "enabled-b": True},
        {
            "enabled-a": _app("com.example.a", ["1.2.3"]),
            "disabled": _app("com.example.disabled", ["9.9.9"]),
            "enabled-b": _app("com.example.b", [], ["2.0.0"]),
            "not-in-toml": _app("com.example.notoml", ["3.0.0"]),
        },
    )

    plans, errors, matched = VAULT._build_plans(
        tmp_path, ["eco"], ["all"], "stable", {}, ""
    )
    assert [plan.package for plan in plans] == ["com.example.a"]
    assert any("enabled-b" == error.get("app") for error in errors)
    assert not any(plan.package == "com.example.disabled" for plan in plans)
    assert not any(plan.package == "com.example.notoml" for plan in plans)
    assert matched == set()


def test_build_plans_beta_and_custom_version(tmp_path):
    """Beta selection and explicit pins resolve the same way as Builder."""
    _write_ecosystem(
        tmp_path,
        "eco",
        {"enabled-b": True},
        {"enabled-b": _app("com.example.b", ["1.0.0"], ["2.0.0"])},
    )

    plans, errors, _ = VAULT._build_plans(
        tmp_path, ["eco"], ["all"], "beta", {}, ""
    )
    assert not errors
    assert plans[0].version_raw == "2.0.0"

    plans, errors, _ = VAULT._build_plans(
        tmp_path, ["eco"], ["all"], "stable", {"enabled-b": "7.7.7"}, ""
    )
    assert not errors
    assert plans[0].version_raw == "7.7.7"


def test_build_plans_dedupes_shared_package_and_prefers_bundle(tmp_path):
    """One package/version is downloaded once even across ecosystems."""
    _write_ecosystem(
        tmp_path,
        "one",
        {"shared": True},
        {"shared": _app("com.example.shared", ["5.0.0"])},
    )
    _write_ecosystem(
        tmp_path,
        "two",
        {"shared": True},
        {"shared": _app("com.example.shared", ["5.0.0"], force_bundle=True)},
    )

    plans, errors, _ = VAULT._build_plans(
        tmp_path, ["one", "two"], ["all"], "stable", {}, ""
    )
    assert not errors
    assert len(plans) == 1
    assert plans[0].related == ["one/shared", "two/shared"]
    assert plans[0].app_data["force_bundle"] is True


def test_run_blank_apps_follows_enabled_toml(tmp_path):
    """An empty workflow app filter means every true TOML entry, nothing else."""
    _write_ecosystem(
        tmp_path,
        "eco",
        {"enabled": True, "disabled": False},
        {
            "enabled": _app("com.example.enabled", ["1.0.0"]),
            "disabled": _app("com.example.disabled", ["2.0.0"]),
        },
    )
    out_dir = tmp_path / "out"
    args = VAULT.parse_arguments([
        "--ecosystems",
        "eco",
        "--apps",
        "",
        "--dry-run",
        "--repo-root",
        str(tmp_path),
        "--output-dir",
        str(out_dir),
    ])

    assert VAULT.run(args) == 0
    summary = json.loads(
        (out_dir / "vault-sync-summary.json").read_text(encoding="utf-8")
    )
    assert summary["requested"]["apps"] == ["all"]
    assert [result["app"] for result in summary["results"]] == ["enabled"]


def test_build_plans_app_filters_and_repo_override(tmp_path):
    """App filters can be plain keys or ecosystem-qualified names."""
    _write_ecosystem(
        tmp_path,
        "one",
        {"first": True, "second": True},
        {
            "first": _app("com.example.first", ["1.0.0"]),
            "second": _app("com.example.second", ["2.0.0"]),
        },
    )

    plans, errors, matched = VAULT._build_plans(
        tmp_path, ["one"], ["one/second"], "stable", {}, ""
    )
    assert not errors
    assert [plan.app_key for plan in plans] == ["second"]
    assert matched == {"one/second"}

    plans, errors, _ = VAULT._build_plans(
        tmp_path,
        ["one"],
        ["first"],
        "stable",
        {},
        "owner/vault-test",
    )
    assert not errors
    assert plans[0].repo == "owner/vault-test"


def test_build_plans_allows_same_filename_in_different_repos(tmp_path):
    """A filename only has to be unique inside its own dataset."""
    _write_ecosystem(
        tmp_path,
        "one",
        {"shared": True},
        {"shared": _app("com.example.shared", ["5.0.0"], hf_repo="owner/one")},
    )
    _write_ecosystem(
        tmp_path,
        "two",
        {"shared": True},
        {"shared": _app("com.example.shared", ["5.0.0"], hf_repo="owner/two")},
    )

    plans, errors, _ = VAULT._build_plans(
        tmp_path, ["one", "two"], ["all"], "stable", {}, ""
    )
    assert not errors
    assert sorted(plan.repo for plan in plans) == ["owner/one", "owner/two"]


def test_parse_custom_versions_supports_global_value():
    """A pin without '=' applies globally, as in the patcher CLI."""
    assert VAULT._parse_custom_versions("7.7.7") == {"_global": "7.7.7"}
    parsed = VAULT._parse_custom_versions("app=1.2.3, other=4.5.6")
    assert parsed["app"] == "1.2.3"
    assert parsed["other"] == "4.5.6"


def test_sanitize_source_url_drops_signed_query():
    """Provenance URLs keep identifiers but never signed download keys."""
    url = "https://www.apkmirror.com/download/?key=secret&id=123"
    assert VAULT._sanitize_source_url(url) == (
        "https://www.apkmirror.com/download/?id=123"
    )
    play = "https://play.google.com/store/apps/details?id=com.example.app&hl=en"
    assert VAULT._sanitize_source_url(play) == (
        "https://play.google.com/store/apps/details?id=com.example.app&hl=en"
    )


def _plan(package="com.example.app", version="1.0.0"):
    """Builds a minimal artifact plan for staging tests."""
    return VAULT.ArtifactPlan(
        repo="owner/vault",
        package=package,
        version_raw=version,
        version_name=version,
        filename_stem=f"{package}_{version}",
        app_key="app",
        ecosystem="eco",
    )


def test_stage_artifact_preserves_container_and_renames(tmp_path):
    """A verified original container is moved, not unwrapped or repacked."""
    source = tmp_path / "downloaded.xapk"
    payload = _make_zip({"com.example.app.apk": _make_apk("1.0.0")})
    source.write_bytes(payload)

    staged = Path(VAULT._stage_artifact(_plan(), str(source), tmp_path))
    assert staged.name == "com.example.app_1.0.0.xapk"
    assert staged.read_bytes() == payload
    assert not source.exists()


def test_delivery_extension_maps_split_zip_to_apks(tmp_path):
    """apkeep ZIP output gets the extension Tier 0 knows how to find."""
    archive = tmp_path / "apkeep-output.zip"
    archive.write_bytes(_make_zip({"base.apk": _make_apk("1.0.0")}))
    assert VAULT._delivery_extension(str(archive)) == ".apks"


def test_delivery_extension_rejects_zip_without_apks(tmp_path):
    """An unrelated ZIP is rejected instead of being relabeled as a bundle."""
    archive = tmp_path / "not-an-apk.zip"
    archive.write_bytes(_make_zip({"readme.txt": b"no apk here"}))
    with pytest.raises(VAULT.ConfigError, match="no APK files"):
        VAULT._delivery_extension(str(archive))


class _FakeCommitOperation:
    """Minimal stand-in for huggingface_hub.CommitOperationAdd."""

    def __init__(self, path_in_repo, path_or_fileobj):
        self.path_in_repo = path_in_repo
        self.path_or_fileobj = path_or_fileobj


class _FakeApi:
    """Records HuggingFace calls without touching the network."""

    def __init__(self, exists=True, files=(), fail_commit=False):
        self.exists = exists
        self.files = set(files)
        self.fail_commit = fail_commit
        self.created = []
        self.commits = []
        self.uploads = []

    def repo_exists(self, repo_id, repo_type=None):  # pylint: disable=unused-argument
        """Reports whether the fake repository exists."""
        return self.exists

    def create_repo(self, repo_id, repo_type=None, private=False):  # pylint: disable=unused-argument
        """Records repository creation."""
        self.created.append(repo_id)
        self.exists = True

    def list_repo_files(self, repo_id, repo_type=None):  # pylint: disable=unused-argument
        """Returns the fake inventory."""
        return sorted(self.files)

    def create_commit(
        self, repo_id, operations, commit_message, repo_type=None
    ):  # pylint: disable=unused-argument,too-many-arguments
        """Applies a fake batch commit."""
        if self.fail_commit:
            raise RuntimeError("simulated batch failure")
        names = [operation.path_in_repo for operation in operations]
        self.commits.append(names)
        self.files.update(names)

    def upload_file(
        self,
        path_or_fileobj,
        path_in_repo,
        repo_id,
        repo_type=None,
        commit_message="",
    ):  # pylint: disable=unused-argument,too-many-arguments
        """Applies a fake single-file upload."""
        self.uploads.append(path_in_repo)
        self.files.add(path_in_repo)


def _fake_publisher(monkeypatch, api, tmp_path):
    """Builds a VaultPublisher backed by a fake API."""
    monkeypatch.setattr(VAULT, "HF_AVAILABLE", True)
    monkeypatch.setattr(VAULT, "_HfApi", lambda token: api)
    monkeypatch.setattr(VAULT, "_CommitOperationAdd", _FakeCommitOperation)
    return VAULT.VaultPublisher(
        token="test-token", enabled=True, keep_files=True, out_dir=tmp_path
    )


def _record(tmp_path, name, metadata=None):
    """Creates a pending upload for a fake local artifact."""
    path = tmp_path / name
    path.write_bytes(b"artifact")
    return VAULT.PendingUpload(
        path=str(path),
        filename=name,
        repo="owner/vault",
        metadata=metadata or {"sha256": "abc"},
        result_index=0,
    )


def test_publisher_skips_existing_and_uploads_manifest(tmp_path, monkeypatch):
    """Existing vault files are found before any download is attempted."""
    api = _FakeApi(exists=True, files={"com.example.app_1.0.0.apk"})
    publisher = _fake_publisher(monkeypatch, api, tmp_path)

    assert publisher.prepare("owner/vault")
    assert publisher.existing(
        "owner/vault", "com.example.app_1.0.0"
    ) == "com.example.app_1.0.0.apk"
    assert not publisher.existing("owner/vault", "com.example.app_2.0.0")

    record = _record(tmp_path, "com.example.app_2.0.0.apk")
    assert publisher.publish("owner/vault", [record]) == {
        record.filename
    }
    publisher.flush_manifests()

    assert record.filename in api.files
    assert "vault_manifest.json" in api.files
    manifest_path = tmp_path / "manifests" / "owner__vault.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifacts"][record.filename] == {"sha256": "abc"}


def test_publisher_merges_existing_manifest(tmp_path, monkeypatch):
    """A new run must never drop metadata for artifacts already in the vault."""
    remote_manifest = tmp_path / "remote-manifest.json"
    remote_manifest.write_text(
        json.dumps({
            "schema": 1,
            "artifacts": {"old.apk": {"sha256": "old"}},
        }),
        encoding="utf-8",
    )
    api = _FakeApi(exists=True, files={"old.apk", "vault_manifest.json"})
    publisher = _fake_publisher(monkeypatch, api, tmp_path)
    monkeypatch.setattr(
        VAULT, "_hf_hub_download", lambda **kwargs: str(remote_manifest)
    )

    assert publisher.prepare("owner/vault")
    record = _record(tmp_path, "new.apk")
    publisher.publish("owner/vault", [record])
    publisher.flush_manifests()

    manifest_path = tmp_path / "manifests" / "owner__vault.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["artifacts"]) == {"old.apk", "new.apk"}


def test_publisher_falls_back_to_single_file_commits(tmp_path, monkeypatch):
    """One bad batch commit must not lose the other verified artifacts."""
    api = _FakeApi(exists=True, fail_commit=True)
    publisher = _fake_publisher(monkeypatch, api, tmp_path)
    assert publisher.prepare("owner/vault")

    first = _record(tmp_path, "one.apk")
    second = _record(tmp_path, "two.apk")
    uploaded = publisher.publish("owner/vault", [first, second])

    assert uploaded == {"one.apk", "two.apk"}
    assert api.uploads == ["one.apk", "two.apk"]
    assert {"one.apk", "two.apk"} <= api.files


def test_publisher_does_not_overwrite_unreadable_manifest(tmp_path, monkeypatch):
    """If the remote manifest cannot be parsed, it is left untouched."""
    broken_manifest = tmp_path / "broken-manifest.json"
    broken_manifest.write_text("{not json", encoding="utf-8")
    api = _FakeApi(exists=True, files={"vault_manifest.json"})
    publisher = _fake_publisher(monkeypatch, api, tmp_path)
    monkeypatch.setattr(
        VAULT, "_hf_hub_download", lambda **kwargs: str(broken_manifest)
    )

    assert publisher.prepare("owner/vault")
    record = _record(tmp_path, "new.apk")
    publisher.publish("owner/vault", [record])
    publisher.flush_manifests()

    assert "vault_manifest.json" in api.files
    assert not api.uploads
    assert not (tmp_path / "manifests" / "owner__vault.json").exists()
