"""Unit tests for the builder's pure parsing and matching helpers."""

# Test modules legitimately exercise private helpers; the production code
# keeps the protected-access check enabled.
# pylint: disable=protected-access

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup
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


MAIN = _load_module("builder_main", "main.py")
CORE_UTILS = _load_module("builder_core_utils", "core/utils.py")
TIER1 = _load_module("scrapers.tier1_apkmirror", "scrapers/tier1_apkmirror.py")
TIER3 = _load_module("scrapers.tier3_archive", "scrapers/tier3_archive.py")
AUTO_CONFIG = _load_module(
    "builder_auto_update_config", ".github/scripts/auto_update_config.py"
)
CONVERT_TO_TEXT = _load_module(
    "builder_convert_to_text", ".github/scripts/convert_to_text.py"
)


class _FakeArgs:
    """Minimal args stand-in for resolver tests."""

    def __init__(self, selection):
        self.version_selection = selection


def test_parse_version_key_normalizes_v_prefix():
    """Versions differing only by a leading v/V must compare equal."""
    parse = AUTO_CONFIG._parse_version_key
    assert parse("1.2.3") == parse("v1.2.3")
    assert parse("1.2.3") == parse("V1.2.3")
    assert parse("12.41.0-prod.01") == parse("v12.41.0-prod.01")


def test_parse_version_key_orders_stable_above_pre_release():
    """Stable releases sort above pre-release tags sharing the same base."""
    parse = AUTO_CONFIG._parse_version_key
    assert parse("12.41.0-prod.01") > parse("12.40.0-alpha.01")
    assert parse("2.0.0") > parse("2.0.0-beta.1")
    assert parse("2.0.0-beta.2") > parse("2.0.0-beta.1")


def test_get_target_sort_key_prefers_higher_version():
    """Newer versions with higher version codes sort above older ones."""
    old = {"version": "12.40.0", "versionCodes": {"arm64-v8a": "312400001"}}
    new = {"version": "12.41.0", "versionCodes": {"arm64-v8a": "312401001"}}
    key_fn = AUTO_CONFIG._get_target_sort_key
    assert key_fn(new) > key_fn(old)


def test_get_target_sort_key_tolerates_non_decimal_codes():
    """Non-decimal version codes are ignored during sort-key building."""
    target = {"version": "1.0", "versionCodes": {"arm64-v8a": "²"}}
    assert AUTO_CONFIG._get_target_sort_key(target)[1] == -1


def test_normalize_vcodes_list_uses_highest_digit():
    """A list of version codes collapses to its highest numeric value."""
    assert AUTO_CONFIG._normalize_vcodes(["312401001", 31100, None]) == {
        "universal": "312401001"
    }


def test_normalize_vcodes_list_without_digits_returns_empty():
    """Lists without numeric codes produce no garbage universal entry."""
    assert AUTO_CONFIG._normalize_vcodes(["latest", None]) == {}


def test_normalize_vcodes_dict_normalizes_keys():
    """Mixed-separator version code keys map to canonical arch names."""
    assert AUTO_CONFIG._normalize_vcodes(
        {"ARM64_V8A": "1", "X86-64": "2"}
    ) == {"arm64-v8a": "1", "x86_64": "2"}


def test_normalize_vcodes_ignores_non_decimal_digits():
    """Unicode digit lookalikes are skipped instead of crashing int()."""
    assert AUTO_CONFIG._normalize_vcodes(["312401001", "²"]) == {
        "universal": "312401001"
    }


def test_repo_raw_url_expands_repository_hosts():
    """Repository URLs expand to raw file URLs for each forge layout."""
    expand = AUTO_CONFIG._repo_raw_url
    assert expand("owner/repo", "main", "patches-list.json") == (
        "https://raw.githubusercontent.com/owner/repo/main/patches-list.json"
    )
    assert expand("https://github.com/owner/repo", "dev", "list.json") == (
        "https://raw.githubusercontent.com/owner/repo/dev/list.json"
    )
    assert expand(
        "https://git.kvkdns.org/kveld9/kveld-morphe-patches",
        "main",
        "patches-list.json",
    ) == (
        "https://git.kvkdns.org/kveld9/kveld-morphe-patches/"
        "raw/branch/main/patches-list.json"
    )
    assert expand(
        "https://gitlab.com/owner/repo", "main", "patches-list.json"
    ) == "https://gitlab.com/owner/repo/-/raw/main/patches-list.json"


def test_repo_raw_url_keeps_explicit_raw_urls():
    """Full raw URLs and .git repository suffixes stay valid."""
    raw = "https://example.com/owner/repo/raw/branch/main/patches-list.json"
    assert AUTO_CONFIG._repo_raw_url(raw, "main", "patches-list.json") == raw
    assert AUTO_CONFIG._repo_raw_url(
        "https://git.kvkdns.org/kveld9/repo.git",
        "main",
        "patches-list.json",
    ) == "https://git.kvkdns.org/kveld9/repo/raw/branch/main/patches-list.json"


def test_parse_fallback_urls_expands_repo_bases():
    """Mirror entries accept repo bases while invalid entries are dropped."""
    parsed = AUTO_CONFIG._parse_fallback_urls({
        "fallback_urls": [
            " https://git.kvkdns.org/kveld9/kveld-morphe-patches ",
            "https://example.com/owner/repo/raw/branch/main/patches-list.json",
            "ftp://bad",
            "",
            5,
        ]
    })
    assert parsed == [
        "https://git.kvkdns.org/kveld9/kveld-morphe-patches/"
        "raw/branch/main/patches-list.json",
        "https://example.com/owner/repo/raw/branch/main/patches-list.json",
    ]
    assert AUTO_CONFIG._parse_fallback_urls(
        {"fallback_urls": "https://git.kvkdns.org/owner/repo"}
    ) == [
        "https://git.kvkdns.org/owner/repo/raw/branch/main/patches-list.json"
    ]
    assert AUTO_CONFIG._parse_fallback_urls({}) == []


def test_process_ecosystem_falls_back_to_mirror(monkeypatch, tmp_path):
    """A failed GitHub raw fetch retries the declared mirror."""
    config_path = tmp_path / "kveld9.json"
    config_path.write_text("{}", encoding="utf-8")
    primary = (
        "https://raw.githubusercontent.com/kveld9/"
        "kveld-morphe-patches/main/patches-list.json"
    )
    fallback = (
        "https://git.kvkdns.org/kveld9/"
        "kveld-morphe-patches/raw/branch/main/patches-list.json"
    )
    calls = []

    def fake_fetch(url, **_kwargs):
        calls.append(url)
        if url == primary:
            return None
        return {"patches": []}

    monkeypatch.setattr(AUTO_CONFIG, "fetch_patches_list", fake_fetch)
    info = {
        "url": primary,
        "fallback_urls": [fallback],
        "release_patches": False,
    }
    assert AUTO_CONFIG._process_ecosystem(str(config_path), info) == {
        "patches": []
    }
    assert calls == [primary, fallback]


def test_process_ecosystem_keeps_primary_when_it_works(monkeypatch, tmp_path):
    """A healthy primary source must not trigger a fallback request."""
    config_path = tmp_path / "kveld9.json"
    config_path.write_text("{}", encoding="utf-8")
    calls = []

    def fake_fetch(url, **_kwargs):
        calls.append(url)
        return {"patches": []}

    monkeypatch.setattr(AUTO_CONFIG, "fetch_patches_list", fake_fetch)
    info = {
        "url": "https://raw.githubusercontent.com/owner/repo/main/patches-list.json",
        "fallback_urls": [
            "https://git.kvkdns.org/owner/repo/raw/branch/main/patches-list.json"
        ],
        "release_patches": False,
    }
    assert AUTO_CONFIG._process_ecosystem(str(config_path), info) is not None
    assert len(calls) == 1


def test_process_ecosystem_falls_back_on_invalid_payload(monkeypatch, tmp_path):
    """A JSON error object from GitHub must not block the mirror retry."""
    config_path = tmp_path / "kveld9.json"
    config_path.write_text("{}", encoding="utf-8")
    calls = []

    def fake_fetch(url, **_kwargs):
        calls.append(url)
        if len(calls) == 1:
            return {"message": "Not Found"}
        return {"patches": [{"name": "Example"}]}

    monkeypatch.setattr(AUTO_CONFIG, "fetch_patches_list", fake_fetch)
    info = {
        "url": "https://raw.githubusercontent.com/owner/repo/main/patches-list.json",
        "fallback_urls": [
            "https://git.kvkdns.org/owner/repo/raw/branch/main/patches-list.json"
        ],
        "release_patches": False,
    }
    result = AUTO_CONFIG._process_ecosystem(str(config_path), info)
    assert result == {"patches": [{"name": "Example"}]}
    assert len(calls) == 2


def test_load_ecosystems_reads_fallbacks_from_repo_map():
    """repo_map.json mirrors are expanded for the updater."""
    ecosystems = AUTO_CONFIG.load_ecosystems()
    by_name = {Path(path).stem: info for path, info in ecosystems.items()}
    assert by_name["kveld9"]["fallback_urls"] == [
        "https://git.kvkdns.org/kveld9/kveld-morphe-patches/"
        "raw/branch/main/patches-list.json"
    ]
    assert by_name["kveld9-extra"]["fallback_urls"] == [
        "https://git.kvkdns.org/kveld9/kveld-extra-morphe-patches/"
        "raw/branch/main/patches-list.json"
    ]
    assert by_name["hoo-dles"]["fallback_urls"] == [
        "https://gitgud.io/xiondigital/MorphePatches/"
        "-/raw/main/hoodles/patches-list.json"
    ]


def test_load_ecosystems_without_repo_map_has_no_hidden_fallback(monkeypatch):
    """A missing repo_map must not resurrect the old hardcoded sources."""
    monkeypatch.setattr(AUTO_CONFIG, "_find_repo_map", lambda *_args: None)
    assert AUTO_CONFIG.load_ecosystems() == {}


def test_main_aborts_without_ecosystems(monkeypatch):
    """The updater must fail visibly instead of silently doing nothing."""
    monkeypatch.setattr(AUTO_CONFIG, "load_ecosystems", lambda: {})
    with pytest.raises(SystemExit) as exc_info:
        AUTO_CONFIG.main()
    assert exc_info.value.code == 1


def test_update_app_version_syncs_codes_one_to_one():
    """Upstream code sets replace local ones, dropping removed arches."""
    app = {
        "stable": ["454.0.0.54.77"],
        "version_codes": {
            "arm64-v8a": "386014758",
            "armeabi-v7a": "386014757",
            "x86": "386014759",
            "x86_64": "386014760",
        },
    }
    pkg = {"latest_target": {
        "version": "456.0.0.53.78",
        "versionCodes": {
            "ARM64_V8A": 386214574,
            "ARMEABI_V7A": 386214562,
            "X86": 386214575,
        },
    }}
    assert AUTO_CONFIG._update_app_version("instagram", app, pkg) is True
    assert app["stable"] == ["456.0.0.53.78"]
    assert app["version_codes"] == {
        "arm64-v8a": "386214574",
        "armeabi-v7a": "386214562",
        "x86": "386214575",
    }


def test_update_app_version_keeps_codes_when_upstream_silent():
    """Upstream without a versionCodes field leaves local codes untouched."""
    app = {"stable": ["1.0.0"], "version_codes": {"arm64-v8a": "100"}}
    pkg = {"latest_target": {"version": "1.1.0"}}
    assert AUTO_CONFIG._update_app_version("app", app, pkg) is True
    assert app["version_codes"] == {"arm64-v8a": "100"}


def test_update_app_version_clears_codes_on_explicit_null():
    """An upstream null versionCodes clears stale local codes."""
    app = {"stable": ["1.0.0"], "version_codes": {"arm64-v8a": "100"}}
    pkg = {"latest_target": {"version": "1.1.0", "versionCodes": None}}
    assert AUTO_CONFIG._update_app_version("app", app, pkg) is True
    assert "version_codes" not in app


def test_update_app_version_preserves_flagged_codes():
    """preserve_version_codes keeps local codes regardless of upstream."""
    app = {
        "stable": ["1.0.0"],
        "preserve_version_codes": True,
        "version_codes": {"arm64-v8a": "100"},
    }
    pkg = {"latest_target": {
        "version": "1.1.0",
        "versionCodes": {"ARM64_V8A": "200"},
    }}
    assert AUTO_CONFIG._update_app_version("app", app, pkg) is True
    assert app["version_codes"] == {"arm64-v8a": "100"}


def test_update_app_version_syncs_universal_from_list():
    """List-style upstream codes collapse to the universal key 1:1."""
    app = {"stable": ["1.0.0"], "version_codes": {"arm64-v8a": "100"}}
    pkg = {"latest_target": {
        "version": "1.1.0",
        "versionCodes": ["300", "200"],
    }}
    assert AUTO_CONFIG._update_app_version("app", app, pkg) is True
    assert app["version_codes"] == {"universal": "300"}


def test_collect_patch_options_picks_boolean_false_defaults():
    """Only options whose default is literally false are recorded."""
    patch = {
        "options": [
            {"key": "hideShortsWidget", "default": False},
            {"key": "enabledOpt", "default": True},
            {"key": "strOpt", "default": "in-app"},
            {"key": "nullOpt", "default": None},
            {"key": "", "default": False},
            {"default": False},
            "junk",
        ]
    }
    assert AUTO_CONFIG._collect_patch_options(patch) == {"hideShortsWidget": False}


def test_extract_pkg_data_flags_experimental_and_options():
    """Experimental targets and false option defaults surface per package."""
    data = {"patches": [{
        "name": "Hide Shorts components",
        "default": True,
        "compatiblePackages": [{
            "packageName": "com.google.android.youtube",
            "targets": [
                {"version": "21.45.82", "isExperimental": True},
                {"version": "21.30.216", "isExperimental": False},
            ],
        }],
        "options": [
            {"key": "hideShortsWidget", "default": False},
            {"key": "other", "default": True},
        ],
    }]}
    pkg_data = AUTO_CONFIG._extract_pkg_data(data)
    pkg = pkg_data["com.google.android.youtube"]
    assert pkg["latest_target"]["version"] == "21.30.216"
    assert pkg["latest_experimental"]["version"] == "21.45.82"
    assert pkg["patch_options"] == {
        "Hide Shorts components": {"hideShortsWidget": False}
    }
    assert pkg["patches"] == {"Hide Shorts components": True}


def test_update_app_version_mirrors_highest_experimental_beta():
    """Beta mirrors the highest upstream experimental version."""
    app = {"stable": ["21.30.216"], "beta": ["21.46.520"]}
    pkg = {
        "latest_target": {"version": "21.30.216"},
        "latest_experimental": {"version": "21.45.82"},
    }
    assert AUTO_CONFIG._update_app_version("youtube", app, pkg) is True
    assert app["beta"] == ["21.45.82"]


def test_update_app_version_leaves_beta_without_experimental():
    """Ecosystems without experimental targets keep their beta list intact."""
    app = {"stable": ["1.0.0"], "beta": []}
    pkg = {"latest_target": {"version": "1.1.0"}}
    assert AUTO_CONFIG._update_app_version("app", app, pkg) is True
    assert not app["beta"]


def test_sync_app_patches_records_false_option_defaults():
    """Default-true patches with false options gain recorded entries."""
    app = {"options_override": {}}
    patches = {"Hide Shorts components": True, "Network proxy": False}
    patch_options = {"Hide Shorts components": {"hideShortsWidget": False}}
    changed = AUTO_CONFIG._sync_app_patches(app, patches, patch_options, "youtube")
    assert changed is True
    assert app["options_override"]["Hide Shorts components"] == {
        "enabled": True,
        "options": {"hideShortsWidget": False},
    }
    assert app["options_override"]["Network proxy"] == {"enabled": False}


def test_sync_app_patches_preserves_local_option_values():
    """Existing option values are kept; only missing keys are filled."""
    app = {"options_override": {
        "Hide Shorts components": {
            "enabled": True,
            "options": {"hideShortsWidget": True},
        },
    }}
    patches = {"Hide Shorts components": True}
    patch_options = {
        "Hide Shorts components": {
            "hideShortsWidget": False,
            "hideShortsAppShortcut": False,
        },
    }
    changed = AUTO_CONFIG._sync_app_patches(app, patches, patch_options, "youtube")
    assert changed is True
    assert app["options_override"]["Hide Shorts components"]["options"] == {
        "hideShortsWidget": True,
        "hideShortsAppShortcut": False,
    }


def test_sync_app_patches_prunes_stale_option_keys():
    """Option keys dropped upstream are removed; empty entries follow."""
    app = {"options_override": {
        "Widget patch": {"enabled": True, "options": {"gone": False}},
    }}
    patches = {"Widget patch": True}
    changed = AUTO_CONFIG._sync_app_patches(app, patches, {}, "app")
    assert changed is True
    assert "Widget patch" not in app["options_override"]


def test_sync_app_patches_keeps_explicit_disable():
    """A locally disabled default-true patch is never pruned as redundant."""
    app = {"options_override": {"Widget patch": {"enabled": False}}}
    patches = {"Widget patch": True}
    changed = AUTO_CONFIG._sync_app_patches(app, patches, {}, "app")
    assert changed is False
    assert app["options_override"]["Widget patch"] == {"enabled": False}


def test_sync_app_patches_removes_obsolete_and_merges_options():
    """Obsolete patches vanish; default-false patches gain upstream options."""
    app = {"options_override": {
        "Old patch": {"enabled": False},
        "Install next to Instagram": {"enabled": False},
    }}
    patches = {"Install next to Instagram": False}
    patch_options = {"Install next to Instagram": {"markIcon": False}}
    changed = AUTO_CONFIG._sync_app_patches(app, patches, patch_options, "instagram")
    assert changed is True
    assert "Old patch" not in app["options_override"]
    assert app["options_override"]["Install next to Instagram"] == {
        "enabled": False,
        "options": {"markIcon": False},
    }


def test_build_new_app_config_prefills_beta_and_options():
    """New app configs prefill beta and record false option defaults."""
    pkg = {
        "package": "com.example.app",
        "name": "Example",
        "latest_target": {"version": "1.0.0"},
        "latest_experimental": {"version": "2.0.0-beta.1"},
        "patches": {"Premium unlock": False, "Widget toggle": True},
        "patch_options": {"Widget toggle": {"hideWidget": False}},
        "apk_file_type": "APK",
    }
    config = AUTO_CONFIG._build_new_app_config(pkg)
    assert config["stable"] == ["1.0.0"]
    assert config["beta"] == ["2.0.0-beta.1"]
    assert config["options_override"] == {
        "Premium unlock": {"enabled": False},
        "Widget toggle": {"enabled": True, "options": {"hideWidget": False}},
    }


def test_sync_workflow_and_toml_repairs_missing_apps(tmp_path, monkeypatch):
    """Existing TOMLs must gain config apps that predate their creation."""
    eco_name = "aidan"
    config_path = tmp_path / f"{eco_name}.json"
    config_path.write_text(
        json.dumps(
            {
                eco_name: {
                    "apps": {
                        "365-pay": {"stable": ["10.2605.0"]},
                        "adobe-scan": {"stable": ["26.09.25"]},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    toml_path = tmp_path / "config" / f"apps-{eco_name}.toml"
    toml_path.parent.mkdir()
    toml_path.write_text(
        AUTO_CONFIG.TOML_HEADER
        + '"365-pay" = false\n'
        + '"removed-app" = true\n',
        encoding="utf-8",
    )
    yaml_path = tmp_path / f"{eco_name}-patcher.yml"
    yaml_path.write_text("apps_to_patch:\n", encoding="utf-8")

    monkeypatch.setattr(AUTO_CONFIG, "_get_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        AUTO_CONFIG, "_find_workflow_path", lambda _name: str(yaml_path)
    )

    changed = AUTO_CONFIG._sync_workflow_and_toml(
        eco_name,
        str(config_path),
        14,
        added_apps={},
        removed_apps=[],
        changed=False,
    )

    assert changed is True
    content = toml_path.read_text(encoding="utf-8")
    assert '"365-pay" = false' in content
    assert '"adobe-scan" = true' in content
    assert '"removed-app"' not in content


def test_sync_workflow_and_toml_creates_toml_for_text_input(
    tmp_path, monkeypatch
):
    """Text-input workflows need a TOML even when the app count is low."""
    eco_name = "small"
    config_path = tmp_path / f"{eco_name}.json"
    config_path.write_text(
        json.dumps(
            {
                eco_name: {
                    "apps": {
                        "alpha": {"stable": ["1.0.0"]},
                        "beta": {"stable": ["Any"]},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    toml_path = tmp_path / "config" / f"apps-{eco_name}.toml"
    yaml_path = tmp_path / f"{eco_name}-patcher.yml"
    yaml_path.write_text("apps_to_patch:\n", encoding="utf-8")

    monkeypatch.setattr(AUTO_CONFIG, "_get_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        AUTO_CONFIG, "_find_workflow_path", lambda _name: str(yaml_path)
    )

    changed = AUTO_CONFIG._sync_workflow_and_toml(
        eco_name,
        str(config_path),
        2,
        added_apps={},
        removed_apps=[],
        changed=False,
    )

    assert changed is True
    content = toml_path.read_text(encoding="utf-8")
    assert '"alpha" = true' in content
    assert '"beta" = false' in content


def test_convert_workflow_repairs_toml_for_text_input(tmp_path, monkeypatch):
    """Already-migrated workflows still reconcile apps missing from TOML."""
    eco_name = "aidan"
    config_dir = tmp_path / "ecosystem"
    config_dir.mkdir()
    (config_dir / f"{eco_name}.json").write_text(
        json.dumps(
            {
                eco_name: {
                    "apps": {
                        "365-pay": {"stable": ["10.2605.0"]},
                        "adobe-scan": {"stable": ["26.09.25"]},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    toml_path = tmp_path / "config" / f"apps-{eco_name}.toml"
    toml_path.parent.mkdir()
    toml_path.write_text(
        AUTO_CONFIG.TOML_HEADER + '"365-pay" = false\n',
        encoding="utf-8",
    )
    yaml_path = tmp_path / f"{eco_name}-patcher.yml"
    yaml_path.write_text(
        '"on":\n'
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      apps_to_patch:\n"
        "        type: string\n"
        "\n"
        "jobs:\n"
        "  call-patcher:\n"
        "    with:\n"
        f'      ecosystem: "{eco_name}"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(CONVERT_TO_TEXT, "_get_repo_root", lambda: str(tmp_path))

    assert CONVERT_TO_TEXT.convert_workflow(str(yaml_path)) is True
    content = toml_path.read_text(encoding="utf-8")
    assert '"365-pay" = false' in content
    assert '"adobe-scan" = true' in content


@pytest.mark.parametrize(
    "text,target,expected",
    [
        ("x86_64 nodpi apk", "x86", False),
        ("x86_64 nodpi apk", "x86_64", True),
        ("x86 nodpi apk", "x86", True),
        ("x86-64 nodpi", "x86", False),
        ("arm64-v8a nodpi", "arm64-v8a", True),
        ("armeabi-v7a nodpi", "arm64-v8a", False),
        ("universal", "universal", True),
        ("noarch", "arm64-v8a", True),
        ("some app 1.2.3 nodpi", "arm64-v8a", True),
    ],
)
def test_is_arch_match(text, target, expected):
    """Architecture tokens must match as standalone words only."""
    assert TIER1.ApkmirrorScraper._is_arch_match(text, target, 1) is expected


def test_is_arch_match_multi_arm_bundle():
    """Multi-arm bundle rows match both ARM targets and universal."""
    text = "app 1.0 arm64-v8a armeabi-v7a bundle"
    assert TIER1.ApkmirrorScraper._is_arch_match(text, "arm64-v8a", 3) is True
    assert TIER1.ApkmirrorScraper._is_arch_match(text, "armeabi-v7a", 3) is True
    assert TIER1.ApkmirrorScraper._is_arch_match(text, "universal", 3) is True
    assert TIER1.ApkmirrorScraper._is_arch_match(text, "x86", 3) is False


def test_is_arch_token_case_insensitive():
    """Arch token matching tolerates mixed-case text and tokens."""
    assert TIER1._is_arch_token("X86 NODPI APK", "x86") is True
    assert TIER1._is_arch_token("X86_64 NODPI APK", "x86") is False


REDDIT_VARIANTS = [
    {"arch": "universal", "android": "12L+"},
    {"arch": "arm64-v8a", "android": "10+"},
]


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "2026.48.0 universal Android 12L+ 120-480dpi BUNDLE",
            0,
        ),
        (
            "2026.48.0 arm64-v8a Android 10+ 480-640dpi BUNDLE",
            1,
        ),
        (
            "2026.48.0 universal Android 10+ 120-480dpi BUNDLE",
            None,
        ),
        (
            "2026.48.0 x86 Android 12L+ 120-480dpi BUNDLE",
            None,
        ),
        (
            "2026.48.0 arm64-v8a Android 12L+ 480-640dpi BUNDLE",
            None,
        ),
    ],
)
def test_variant_preference_index_requires_all_fields(text, expected):
    """A variant rule only matches when arch, Android, and DPI agree."""
    assert TIER1.ApkmirrorScraper._variant_preference_index(
        text, REDDIT_VARIANTS
    ) == expected


def _reddit_variant_row(arch, android, dpi, slug):
    """Builds a Reddit release-table row with a bundle badge."""
    return BeautifulSoup(
        (
            '<div class="table-row headerFont">'
            '<div class="table-cell">'
            '<a class="accent_color" href="/apk/redditinc/reddit/'
            f'reddit-2026-48-0-release/{slug}/">2026.48.0</a>'
            '<span class="apkm-badge success">BUNDLE</span>'
            '<span class="colorLightBlack">2648001</span>'
            "</div>"
            f'<div class="table-cell">{arch}</div>'
            f'<div class="table-cell">{android}</div>'
            f'<div class="table-cell">{dpi}</div>'
            "</div>"
        ),
        "html.parser",
    ).div


def _reddit_rows():
    """Returns Reddit's two 2026.48.0 bundles in ARM-first site order."""
    return [
        _reddit_variant_row(
            "arm64-v8a",
            "Android 10+",
            "480-640dpi",
            "reddit-2026-48-0-android-apk-download",
        ),
        _reddit_variant_row(
            "universal",
            "Android 12L+",
            "120-480dpi",
            "reddit-2026-48-0-2-android-apk-download",
        ),
    ]


def test_find_variant_prefers_universal_12l_over_site_order(monkeypatch):
    """Reddit's universal/12L+ bundle wins even when ARM is listed first."""
    scraper = object.__new__(TIER1.ApkmirrorScraper)
    ctx = SimpleNamespace(
        pkg="com.reddit.frontpage",
        app_data={
            "force_bundle": True,
            "apkm_variants": REDDIT_VARIANTS,
        },
        arch="arm64-v8a",
        target_ver="2026.48.0",
    )
    calls = []

    def fake_process(_ctx, url, is_bundle):
        calls.append((url, is_bundle))
        return "/tmp/reddit.apkm"

    monkeypatch.setattr(scraper, "_process_variant_page", fake_process)

    out = scraper._find_variant_in_rows(ctx, _reddit_rows(), "", True)

    assert out == "/tmp/reddit.apkm"
    assert len(calls) == 1
    assert "-2-android-apk-download" in calls[0][0]
    assert calls[0][1] is True


def test_find_variant_falls_back_to_arm64_when_universal_fails(
    monkeypatch,
):
    """A failed universal download falls through to arm64/Android 10+."""
    scraper = object.__new__(TIER1.ApkmirrorScraper)
    ctx = SimpleNamespace(
        pkg="com.reddit.frontpage",
        app_data={
            "force_bundle": True,
            "apkm_variants": REDDIT_VARIANTS,
        },
        arch="arm64-v8a",
        target_ver="2026.48.0",
    )
    calls = []

    def fake_process(_ctx, url, is_bundle):
        calls.append((url, is_bundle))
        if "-2-android-apk-download" in url:
            return None
        return "/tmp/reddit-arm64.apkm"

    monkeypatch.setattr(scraper, "_process_variant_page", fake_process)

    out = scraper._find_variant_in_rows(ctx, _reddit_rows(), "", True)

    assert out == "/tmp/reddit-arm64.apkm"
    assert len(calls) == 2
    assert "-2-android-apk-download" in calls[0][0]
    assert "-2-android-apk-download" not in calls[1][0]


def _release_link(title, href):
    """Builds a minimal APKMirror search-result anchor."""
    return BeautifulSoup(
        f'<a class="fontBlack" href="{href}">{title}</a>', "html.parser"
    ).a


@pytest.mark.parametrize(
    "title,href,expected",
    [
        (
            "YouTube 21.45.82",
            "/apk/google-inc/youtube/youtube-21-45-82-release/",
            True,
        ),
        (
            "YouTube 21.45.82-BETA",
            "/apk/google-inc/youtube/youtube-21-45-82-beta-release/",
            True,
        ),
        (
            "YouTube 21.45.82-EXTENDED",
            "/apk/google-inc/youtube/youtube-21-45-82-extended-release/",
            False,
        ),
        (
            "YouTube 21.45.82-SECONDARY",
            "/apk/google-inc/youtube/youtube-21-45-82-secondary-release/",
            False,
        ),
    ],
)
def test_is_valid_release_link_filters_upload_tags(title, href, expected):
    """Extended and secondary uploads are skipped; beta uploads stay eligible."""
    exc_kws, inc_kws = TIER1.ApkmirrorScraper._get_filter_kws(
        SimpleNamespace(app_data={})
    )
    result = TIER1.ApkmirrorScraper._is_valid_release_link(
        _release_link(title, href), "21.45.82", exc_kws, inc_kws
    )
    assert (result is not None) is expected


def test_get_filter_kws_merges_default_and_custom_exclusions():
    """Per-app exclusions are appended to the default release tags."""
    exc_kws, _ = TIER1.ApkmirrorScraper._get_filter_kws(
        SimpleNamespace(app_data={
            "apkm_exclude": ["Fire TV", "tablet"],
        })
    )
    assert exc_kws == ["extended", "secondary", "fire tv", "tablet"]


def test_find_release_skips_newer_extended_upload(monkeypatch):
    """A newer extended upload must not shadow the exact version release."""
    html = (
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-extended-release/">YouTube 21.45.82-EXTENDED</a>'
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-release/">YouTube 21.45.82</a>'
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-secondary-release/">YouTube 21.45.82-SECONDARY</a>'
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-beta-release/">YouTube 21.45.82-BETA</a>'
    )
    scraper = object.__new__(TIER1.ApkmirrorScraper)
    ctx = SimpleNamespace(
        pkg="com.google.android.youtube",
        app_data={"search_term": "YouTube"},
        target_ver="21.45.82",
    )

    def fake_get(_ctx, url):
        return SimpleNamespace(url=url, text=html)

    monkeypatch.setattr(scraper, "_safe_get", fake_get)
    url = scraper._find_release(ctx)
    assert url == (
        "https://www.apkmirror.com/apk/google-inc/youtube/"
        "youtube-21-45-82-release/"
    )


def test_find_release_falls_back_to_beta_upload(monkeypatch):
    """Beta uploads remain valid when no plain release exists."""
    html = (
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-extended-release/">YouTube 21.45.82-EXTENDED</a>'
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-beta-release/">YouTube 21.45.82-BETA</a>'
    )
    scraper = object.__new__(TIER1.ApkmirrorScraper)
    ctx = SimpleNamespace(
        pkg="com.google.android.youtube",
        app_data={"search_term": "YouTube"},
        target_ver="21.45.82",
    )

    def fake_get(_ctx, url):
        return SimpleNamespace(url=url, text=html)

    monkeypatch.setattr(scraper, "_safe_get", fake_get)
    url = scraper._find_release(ctx)
    assert url == (
        "https://www.apkmirror.com/apk/google-inc/youtube/"
        "youtube-21-45-82-beta-release/"
    )


def test_release_priority_ranks_plain_before_beta():
    """Plain and stable uploads outrank beta, which outranks other tags."""
    assert TIER1._release_priority(
        "/apk/x/app-1-0-release/", "App 1.0", "1.0"
    ) == 0
    assert TIER1._release_priority(
        "/apk/x/app-1-0-stable-release/", "App 1.0 - Stable", "1.0"
    ) == 0
    assert TIER1._release_priority(
        "/apk/x/app-1-0-beta-release/", "App 1.0-BETA", "1.0"
    ) == 1
    assert TIER1._release_priority(
        "/apk/x/app-1-0-alpha-release/", "App 1.0-ALPHA", "1.0"
    ) == 2


def test_find_release_prefers_plain_over_beta_regardless_of_order(monkeypatch):
    """A beta listed first must not outrank an existing plain release."""
    html = (
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-beta-release/">YouTube 21.45.82-BETA</a>'
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-release/">YouTube 21.45.82</a>'
    )
    scraper = object.__new__(TIER1.ApkmirrorScraper)
    ctx = SimpleNamespace(
        pkg="com.google.android.youtube",
        app_data={"search_term": "YouTube"},
        target_ver="21.45.82",
    )

    def fake_get(_ctx, url):
        return SimpleNamespace(url=url, text=html)

    monkeypatch.setattr(scraper, "_safe_get", fake_get)
    url = scraper._find_release(ctx)
    assert url == (
        "https://www.apkmirror.com/apk/google-inc/youtube/"
        "youtube-21-45-82-release/"
    )


def test_find_release_keeps_searching_for_plain_after_beta(monkeypatch):
    """A beta-only result page must not stop the search for a plain build."""
    pages = [
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-beta-release/">YouTube 21.45.82-BETA</a>',
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-45-82-release/">YouTube 21.45.82</a>',
    ]
    responses = iter(pages)
    scraper = object.__new__(TIER1.ApkmirrorScraper)
    ctx = SimpleNamespace(
        pkg="com.google.android.youtube",
        app_data={"search_term": "YouTube"},
        target_ver="21.45.82",
    )

    def fake_get(_ctx, url):
        return SimpleNamespace(url=url, text=next(responses))

    monkeypatch.setattr(scraper, "_safe_get", fake_get)
    url = scraper._find_release(ctx)
    assert url == (
        "https://www.apkmirror.com/apk/google-inc/youtube/"
        "youtube-21-45-82-release/"
    )


def test_find_release_rejects_longer_version_prefix(monkeypatch):
    """A target version must not match a longer numeric version."""
    html = (
        '<a class="fontBlack" href="/apk/google-inc/youtube/'
        'youtube-21-46-524-release/">YouTube 21.46.524</a>'
    )
    scraper = object.__new__(TIER1.ApkmirrorScraper)
    ctx = SimpleNamespace(
        pkg="com.google.android.youtube",
        app_data={"search_term": "YouTube"},
        target_ver="21.46.52",
    )

    def fake_get(_ctx, url):
        return SimpleNamespace(url=url, text=html)

    monkeypatch.setattr(scraper, "_safe_get", fake_get)
    assert scraper._find_release(ctx) is None


def test_find_release_rejects_excluded_auto_redirect(monkeypatch):
    """An auto-redirect to a secondary upload must not be downloaded."""
    redirect = (
        "https://www.apkmirror.com/apk/google-inc/youtube/"
        "youtube-21-45-82-secondary-release/"
    )
    scraper = object.__new__(TIER1.ApkmirrorScraper)
    ctx = SimpleNamespace(
        pkg="com.google.android.youtube",
        app_data={"search_term": "YouTube"},
        target_ver="21.45.82",
    )

    def fake_get(_ctx, url):
        return SimpleNamespace(url=redirect, text="")

    monkeypatch.setattr(scraper, "_safe_get", fake_get)
    assert scraper._find_release(ctx) is None


def test_find_release_accepts_plain_auto_redirect(monkeypatch):
    """A redirect to the exact plain release remains valid."""
    redirect = (
        "https://www.apkmirror.com/apk/google-inc/youtube/"
        "youtube-21-45-82-release/"
    )
    scraper = object.__new__(TIER1.ApkmirrorScraper)
    ctx = SimpleNamespace(
        pkg="com.google.android.youtube",
        app_data={"search_term": "YouTube"},
        target_ver="21.45.82",
    )

    def fake_get(_ctx, url):
        return SimpleNamespace(url=redirect, text="")

    monkeypatch.setattr(scraper, "_safe_get", fake_get)
    assert scraper._find_release(ctx) == redirect


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://archive.org/download/ZeNiX/app.apk", ".apk"),
        ("https://archive.org/download/ZeNiX/app.apk?dl=1", ".apk"),
        ("https://archive.org/download/ZeNiX/app.xapk#frag", ".xapk"),
        ("https://archive.org/download/ZeNiX/app.zip", ".apk"),
        ("https://archive.org/download/ZeNiX/app", ".apk"),
    ],
)
def test_resolve_download_ext(url, expected):
    """Download URLs resolve their extension ignoring queries/fragments."""
    assert TIER3._resolve_download_ext(url) == expected


def test_parse_custom_versions_global():
    """A bare value without '=' becomes the global custom version."""
    assert MAIN._parse_custom_versions("1.2.3") == {"_global": "1.2.3"}


def test_parse_custom_versions_pairs():
    """Comma-separated key=value pairs map app keys to versions."""
    assert MAIN._parse_custom_versions("a=1.0,b=2.0") == {"a": "1.0", "b": "2.0"}


def test_parse_custom_versions_malformed_ignored():
    """Malformed entries are skipped instead of corrupting the mapping."""
    assert MAIN._parse_custom_versions("a=1.0,broken,x=2.0") == {
        "a": "1.0",
        "x": "2.0",
    }


def test_apply_custom_version_code_overrides_all_arch_variants():
    """An explicit code overrides every separator variant of the arch."""
    app = {"package": "p", "version_codes": {"arm64-v8a": 1, "universal": 9}}
    merged = MAIN._apply_custom_version_code(app, "312401001", "arm64-v8a")
    assert merged["version_codes"]["arm64-v8a"] == 312401001
    assert merged["version_codes"]["arm64_v8a"] == 312401001
    assert merged["version_codes"]["universal"] == 9
    assert app["version_codes"]["arm64-v8a"] == 1


def test_apply_custom_version_code_rejects_non_numeric():
    """Non-numeric codes fall back to the JSON value untouched."""
    app = {"package": "p"}
    assert MAIN._apply_custom_version_code(app, "abc", "arm64-v8a") is app


def test_apply_custom_version_code_rejects_non_decimal_digits():
    """Superscript digit lookalikes are rejected like non-numeric codes."""
    app = {"package": "p"}
    assert MAIN._apply_custom_version_code(app, "²", "arm64-v8a") is app


def test_apply_custom_version_code_empty_passthrough():
    """Empty custom codes leave the original app data unchanged."""
    app = {"package": "p"}
    assert MAIN._apply_custom_version_code(app, "", "arm64-v8a") is app


def test_build_patch_command_uses_supported_bytecode_mode(tmp_path):
    """Morphe 2.4.0 dropped FULL, so the CLI command must use a valid mode."""
    args = SimpleNamespace(
        cli=str(tmp_path / "morphe.jar"),
        patches=str(tmp_path / "patches.mpp"),
        version_selection="Latest",
        continue_on_error="false",
        keystore=None,
        ks_alias=None,
        ks_pass=None,
        signer=None,
    )
    cmd = MAIN.build_patch_command(
        args,
        {"strip": False},
        ("app.apk", "options.json", "out.apk"),
        "arm64-v8a",
    )
    assert cmd[cmd.index("--bytecode-mode") + 1] == "STRIP_SAFE"
    assert "FULL" not in cmd


def test_resolve_target_version_prefers_beta():
    """Beta selection picks the newest beta when one is configured."""
    app = {"stable": ["1.0.0"], "beta": ["2.0.0-beta.1"]}
    assert MAIN._resolve_target_version(app, _FakeArgs("Beta"), "") == (
        "2.0.0-beta.1"
    )


def test_resolve_target_version_sanitizes_spaces():
    """Spaces in resolved versions become underscores for filenames."""
    app = {"stable": ["351.11 - Stable"]}
    assert MAIN._resolve_target_version(
        app, _FakeArgs("Latest Recommended"), ""
    ) == "351.11_-_Stable"


def test_resolve_target_version_custom_overrides():
    """An explicit custom version beats the configured stable entry."""
    app = {"stable": ["1.0.0"]}
    assert MAIN._resolve_target_version(
        app, _FakeArgs("Latest Recommended"), "9.9.9"
    ) == "9.9.9"


def test_resolve_app_list_all_expands():
    """The reserved 'all' token selects every configured app in order."""
    eco = {"a": {}, "b": {}, "c": {}}
    assert MAIN._resolve_app_list("all", eco) == ["a", "b", "c"]


def test_resolve_app_list_dedupes_and_keeps_order():
    """App lists tolerate whitespace and deduplicate preserving order."""
    eco = {"a": {}, "b": {}}
    assert MAIN._resolve_app_list(" b,a,b ,a", eco) == ["b", "a"]


def test_resolve_app_list_empty():
    """Blank app selections resolve to an empty list."""
    assert MAIN._resolve_app_list(" ,", {"a": {}}) == []


def test_normalize_repo_url_owner_repo():
    """owner/repo identifiers expand into full GitHub URLs."""
    assert MAIN._normalize_repo_url("owner/repo/") == "https://github.com/owner/repo"


def test_normalize_repo_url_rejects_foreign_host():
    """Non-GitHub repository URLs are rejected instead of mangled."""
    assert MAIN._normalize_repo_url("https://gitlab.com/owner/repo") == ""


def test_sandboxed_env_strips_credentials(monkeypatch):
    """Subprocess sandboxes never leak tokens, IDs, or session strings."""
    secrets = (
        "BOT_TOKEN",
        "API_ID",
        "API_HASH",
        "CHAT_ID",
        "SESSION_STRING",
        "PLAY_AAS_TOKEN",
        "KEYSTORE_PASSWORD",
    )
    for secret in secrets:
        monkeypatch.setenv(secret, "x")
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    env = CORE_UTILS._sandboxed_env()
    for secret in secrets:
        assert secret not in env
    assert env["PYTHONHASHSEED"] == "0"
    assert env["PATH"]


def test_extract_download_name_unquotes_redirect_basename():
    """APKMirror redirect basenames decode into the original artifact name."""
    resp = SimpleNamespace(
        headers={},
        url=(
            "https://r2.example.com/downloadprod/com.zhiliaoapp.musically_47.7.3-"
            "2024707030_minAPI23%28arm64-v8a%2Carmeabi-v7a%29%28nodpi%29_"
            "apkmirror.com.apk?X-Amz-Signature=secret"
        ),
    )
    name = CORE_UTILS._extract_download_name(
        resp, "https://www.apkmirror.com/download.php?id=1"
    )
    assert name == (
        "com.zhiliaoapp.musically_47.7.3-2024707030_"
        "minAPI23(arm64-v8a,armeabi-v7a)(nodpi)_apkmirror.com.apk"
    )


def test_extract_download_name_prefers_content_disposition():
    """A server-provided filename wins over the URL basename."""
    resp = SimpleNamespace(
        headers={"Content-Disposition": 'attachment; filename="app-1.2.3.apk"'},
        url="https://example.com/download?id=1",
    )
    assert (
        CORE_UTILS._extract_download_name(resp, "https://example.com/download")
        == "app-1.2.3.apk"
    )


def test_extract_download_name_falls_back_to_original_url():
    """Opaque CDN keys still resolve to the original request's filename."""
    resp = SimpleNamespace(
        headers={},
        url="https://cdn.example.com/opaque/abc123?X-Amz-Signature=secret",
    )
    assert (
        CORE_UTILS._extract_download_name(
            resp, "https://github.com/o/r/releases/download/v1/app.apk"
        )
        == "app.apk"
    )


def test_extract_download_name_decodes_rfc5987_language_tag():
    """Charset and language prefixes decode into a plain filename."""
    resp = SimpleNamespace(
        headers={
            "Content-Disposition": (
                "attachment; filename*=iso-8859-1'it'caf%E9.apk"
            )
        },
        url="https://example.com/download",
    )
    assert (
        CORE_UTILS._extract_download_name(resp, resp.url)
        == "caf\u00e9.apk"
    )


def test_extract_download_name_skips_empty_candidates():
    """Unusable header filenames fall through to the URL basename."""
    resp = SimpleNamespace(
        headers={
            "Content-Disposition": (
                "attachment; filename*=UTF-8''; filename=\"..\""
            )
        },
        url="https://example.com/files/app-1.2.3.apk",
    )
    assert (
        CORE_UTILS._extract_download_name(resp, resp.url)
        == "app-1.2.3.apk"
    )


def test_finalize_download_clears_provenance_on_failure(monkeypatch):
    """A failed post-processing step cannot leave stale source metadata."""
    ctx = SimpleNamespace(
        original_name="artifact.apk",
        source_name="APKMirror",
        source_url="https://www.apkmirror.com/apk/x/y/download/",
    )
    monkeypatch.setattr(MAIN, "process_downloaded_file", lambda _: None)
    assert MAIN._finalize_download(ctx, "artifact.apk") is None
    assert ctx.original_name == ""
    assert ctx.source_name == ""
    assert ctx.source_url == ""


def test_build_log_block_reports_provenance(tmp_path):
    """Per-app headers expose the original filename and sanitized source."""
    entry = {
        "name": "youtube",
        "package": "com.google.android.youtube",
        "version": "21.45.82",
        "arch": "arm64-v8a",
        "status": "success",
        "original_name": "com.google.android.youtube_21.45.82-1_apkmirror.com.apk",
        "source_name": "APKMirror",
        "source_url": "https://www.apkmirror.com/apk/google-inc/youtube/?key=secret#frag",
    }
    block = MAIN._build_log_block(entry, {"staging_dir": str(tmp_path)})
    assert "App: youtube (com.google.android.youtube)" in block
    assert (
        "Original app name: "
        "com.google.android.youtube_21.45.82-1_apkmirror.com.apk"
    ) in block
    assert (
        "Downloaded from: APKMirror "
        "(https://www.apkmirror.com/apk/google-inc/youtube/)"
    ) in block
    assert "key=secret" not in block


def test_format_download_source_preserves_play_store_id():
    """Google Play links keep their package id while tokens are dropped."""
    entry = {
        "source_name": "Google Play",
        "source_url": (
            "https://play.google.com/store/apps/details"
            "?id=com.google.android.youtube&hl=en&token=secret"
        ),
    }
    assert MAIN._format_download_source(entry) == (
        "Google Play (https://play.google.com/store/apps/details"
        "?id=com.google.android.youtube&hl=en)"
    )


def test_format_download_source_drops_one_time_apkmirror_key():
    """APKMirror one-time keys and routing params never reach the log."""
    entry = {
        "source_name": "APKMirror",
        "source_url": (
            "https://www.apkmirror.com/apk/x/y/download/"
            "?key=secret&forcebaseapk=true"
        ),
    }
    assert MAIN._format_download_source(entry) == (
        "APKMirror (https://www.apkmirror.com/apk/x/y/download/)"
    )


def test_pr_reviewer_keeps_paid_neon_as_last_resort():
    """The paid-only Neon model is tried only after free-tier candidates."""
    reviewer = _load_module(
        "builder_pr_reviewer", ".github/scripts/pr_reviewer.py"
    )
    assert reviewer.GEMINI_MODEL_CANDIDATES[-1] == "gemini-4-neon"
    assert "gemini-4-hydrogen" in reviewer.GEMINI_MODEL_CANDIDATES
