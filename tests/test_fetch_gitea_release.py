"""Unit tests for the Gitea/Forgejo release fetcher."""

# Test modules legitimately exercise private helpers; the production code
# keeps the protected-access check enabled.
# pylint: disable=protected-access

import importlib.util
import sys
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_SPEC = importlib.util.spec_from_file_location(
    "builder_fetch_gitea_release",
    REPO_ROOT / ".github/scripts/fetch_gitea_release.py",
)
FETCH = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(FETCH)


def _mpp_asset(tag, size=4):
    ver = tag.lstrip("vV")
    return {
        "name": f"patches-{ver}.mpp",
        "browser_download_url": f"https://example.com/{tag}/patches-{ver}.mpp",
        "size": size,
    }


def _release(tag, prerelease, assets=None):
    return {
        "tag_name": tag,
        "prerelease": prerelease,
        "draft": False,
        "assets": [_mpp_asset(tag)] if assets is None else assets,
    }


def test_version_key_orders_experimental_and_stable_releases():
    """Stable releases sort above pre-release tags and numeric suffixes sort naturally."""
    key = FETCH._version_key
    assert key("v2.13.0") > key("v2.13.0-rc.1")
    assert key("v2.13.0-rc.1") > key("v2.13.0-experimental.10")
    assert key("v2.13.0-experimental.10") > key("v2.13.0-experimental.9")
    assert key("v2.13.0-experimental.6") > key("v2.12.1-experimental.2")
    assert key("v2.13.0-experimental.1") > key("v2.13.0-alpha.9")
    assert key("v2.13.0-alpha.1") > key("v2.13.0-dev.9")
    assert key("v2.13.0.1") > key("v2.13.0") > key("v2.13.0-experimental.1")
    assert key("v2.12.1") > key("v2.12.0")


def test_parse_repo_url_accepts_gitea_urls():
    """Host, owner, and repository are split from a full HTTPS URL."""
    assert FETCH._parse_repo_url(
        "https://git.kvkdns.org/kveld9/kveld-morphe-patches"
    ) == ("git.kvkdns.org", "kveld9", "kveld-morphe-patches")
    assert FETCH._parse_repo_url(
        "https://git.example.com:3000/owner/repo.git"
    ) == ("git.example.com:3000", "owner", "repo")


@pytest.mark.parametrize(
    "url",
    [
        "http://git.kvkdns.org/owner/repo",
        "https://user:pass@git.kvkdns.org/owner/repo",
        "https://git.kvkdns.org/owner/repo?tab=readme",
        "https://git.kvkdns.org/owner/repo/extra",
        "https://git.kvkdns.org/owner",
        "https://git.kvkdns.org:/owner/repo",
        "owner/repo",
    ],
)
def test_parse_repo_url_rejects_unsafe_urls(url):
    """Only plain HTTPS host/owner/repo URLs are accepted."""
    with pytest.raises(ValueError):
        FETCH._parse_repo_url(url)


def test_select_release_prefers_the_highest_channel_version():
    """Stable and experimental channels are filtered independently."""
    releases = [
        _release("v2.12.1", False),
        _release("v2.12.0", False),
        _release("v2.13.0-experimental.6", True),
        _release("v2.13.0-experimental.5", True),
    ]
    stable_rel, stable_track = FETCH._select_release(releases, "stable")
    exp_rel, exp_track = FETCH._select_release(releases, "experimental")
    assert stable_rel["tag_name"] == "v2.12.1"
    assert stable_track == "stable"
    assert exp_rel["tag_name"] == "v2.13.0-experimental.6"
    assert exp_track == "experimental"


def test_select_release_skips_releases_without_mpp_assets():
    """Drafts and assetless releases are skipped in favour of usable bundles."""
    releases = [
        {**_release("v2.12.2", False), "draft": True},
        _release("v2.12.1", False, assets=[{"name": "notes.txt", "browser_download_url": "https://x/n"}]),
        _release("v2.12.0", False),
        _release("v2.13.0-experimental.7", True, assets=[]),
        _release("v2.13.0-experimental.6", True),
    ]
    assert FETCH._select_release(releases, "stable")[0]["tag_name"] == "v2.12.0"
    assert FETCH._select_release(releases, "experimental")[0]["tag_name"] == "v2.13.0-experimental.6"


def test_select_release_experimental_falls_back_to_stable_with_warning(capsys):
    """Missing experimental releases fall back to the highest stable release."""
    releases = [
        _release("v1.34.1", False),
        _release("v1.34.2", False),
    ]
    release, resolved = FETCH._select_release(releases, "experimental")
    assert release["tag_name"] == "v1.34.2"
    assert resolved == "stable"
    assert "falling back to stable release v1.34.2" in capsys.readouterr().err


def test_select_release_raises_when_no_usable_release_exists():
    """Empty or assetless release lists still fail loudly."""
    with pytest.raises(RuntimeError):
        FETCH._select_release(
            [_release("v2.13.0-experimental.6", True)],
            "stable",
        )
    with pytest.raises(RuntimeError):
        FETCH._select_release(
            [_release("v1.34.2", False)],
            "experimental",
            allow_stable_fallback=False,
        )


def test_select_asset_picks_the_matching_mpp():
    """Non-bundle assets are ignored and exact tag stems win over prefixes."""
    release = _release(
        "v2.13.0",
        False,
        assets=[
            {"name": "notes.txt", "browser_download_url": "https://x/notes"},
            {
                "name": "patches-2.13.0-experimental.6.mpp",
                "browser_download_url": "https://x/exp",
                "size": 4,
            },
            {
                "name": "patches-2.13.0.mpp",
                "browser_download_url": "https://x/patches",
                "size": 4,
            },
        ],
    )
    assert FETCH._select_asset(release)["name"] == "patches-2.13.0.mpp"


def test_select_asset_rejects_releases_without_https_mpp():
    """A release without a valid HTTPS .mpp bundle cannot be used."""
    with pytest.raises(RuntimeError):
        FETCH._select_asset(
            _release(
                "v1.0.0",
                False,
                assets=[
                    {"name": "notes.txt", "browser_download_url": "https://x/notes"},
                    {"name": "patches-1.0.0.mpp", "browser_download_url": "http://x/p.mpp"},
                ],
            )
        )


class _FakeResponse:
    """Minimal streaming response used by the download tests."""

    def __init__(self, payload, url="https://example.com/patches.mpp"):
        self.payload = payload
        self.url = url
        self.closed = False

    def raise_for_status(self):
        """Pretend the request succeeded."""

    def iter_content(self, chunk_size):
        """Yield the whole payload in one chunk."""
        yield self.payload

    def close(self):
        """Record that the response was closed."""
        self.closed = True


class _FakeSession:
    """Session returning prepared responses or exceptions across attempts."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, **kwargs):
        """Return the next prepared response or raise a queued exception."""
        item = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def test_download_asset_writes_and_verifies_payload(tmp_path):
    """A valid ZIP payload is written atomically and the response closes."""
    payload = b"PK\x03\x04payload"
    response = _FakeResponse(payload)
    output = tmp_path / "patches.mpp"
    FETCH._download_asset(
        _FakeSession([response]),
        {
            "browser_download_url": "https://example/patches.mpp",
            "size": len(payload),
        },
        str(output),
    )
    assert output.read_bytes() == payload
    assert response.closed is True


def test_download_asset_retries_transient_stream_failures(monkeypatch, tmp_path):
    """Mid-stream network failures retry and succeed on a later attempt."""
    monkeypatch.setattr(FETCH.time, "sleep", lambda _s: None)
    payload = b"PK\x03\x04recovered"
    response = _FakeResponse(payload)
    session = _FakeSession([
        requests.ConnectionError("reset by peer"),
        response,
    ])
    output = tmp_path / "patches.mpp"
    FETCH._download_asset(
        session,
        {
            "browser_download_url": "https://example/patches.mpp",
            "size": len(payload),
        },
        str(output),
    )
    assert session.calls == 2
    assert output.read_bytes() == payload


def test_download_asset_rejects_http_downgrade_and_bad_payloads(monkeypatch, tmp_path):
    """HTTP redirects, truncated streams, and non-ZIP payloads leave no output."""
    monkeypatch.setattr(FETCH.time, "sleep", lambda _s: None)
    payload = b"PK\x03\x04payload"
    output = tmp_path / "patches.mpp"

    with pytest.raises(RuntimeError):
        FETCH._download_asset(
            _FakeSession([_FakeResponse(payload, url="http://insecure/p.mpp")]),
            {
                "browser_download_url": "https://example/patches.mpp",
                "size": len(payload),
            },
            str(output),
        )
    assert not output.exists()

    with pytest.raises(RuntimeError):
        FETCH._download_asset(
            _FakeSession([_FakeResponse(payload)]),
            {
                "browser_download_url": "https://example/patches.mpp",
                "size": len(payload) + 1,
            },
            str(output),
        )
    assert not output.exists()
    assert not list(tmp_path.iterdir())

    with pytest.raises(RuntimeError):
        FETCH._download_asset(
            _FakeSession([_FakeResponse(b"<html>error</html>")]),
            {
                "browser_download_url": "https://example/patches.mpp",
                "size": 18,
            },
            str(output),
        )
    assert not output.exists()


def test_main_writes_github_output_on_fallback(monkeypatch, tmp_path, capsys):
    """CLI emits resolved tag and effective track to GITHUB_OUTPUT."""
    gh_out = tmp_path / "github_output"
    out_mpp = tmp_path / "patches.mpp"

    def fake_resolve(repo, track, output, **_kwargs):
        Path(output).write_bytes(b"PK\x03\x04")
        return "v1.34.2", "stable"

    monkeypatch.setattr(FETCH, "resolve_and_fetch_release", fake_resolve)
    rc = FETCH.main([
        "--repo", "https://git.kvkdns.org/kveld9/kveld-extra-morphe-patches",
        "--track", "experimental",
        "--output", str(out_mpp),
        "--github-output", str(gh_out),
    ])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "v1.34.2"
    assert gh_out.read_text(encoding="utf-8").splitlines() == [
        "tag=v1.34.2",
        "resolved_track=stable",
    ]
