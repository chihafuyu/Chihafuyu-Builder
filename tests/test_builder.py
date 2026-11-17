"""Unit tests for the builder's pure parsing and matching helpers."""

# Test modules legitimately exercise private helpers; the production code
# keeps the protected-access check enabled.
# pylint: disable=protected-access

import importlib.util
import sys
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


MAIN = _load_module("builder_main", "main.py")
CORE_UTILS = _load_module("builder_core_utils", "core/utils.py")
TIER1 = _load_module("scrapers.tier1_apkmirror", "scrapers/tier1_apkmirror.py")
TIER3 = _load_module("scrapers.tier3_archive", "scrapers/tier3_archive.py")
AUTO_CONFIG = _load_module(
    "builder_auto_update_config", ".github/scripts/auto_update_config.py"
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
