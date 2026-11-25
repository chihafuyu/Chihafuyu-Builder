"""
Auto-update ecosystem configs from patches-list.json or bundle metadata.
Run by GitHub Actions to create a Pull Request.
"""

import hashlib
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests

_REQUEST_SESSION = requests.Session()
_REQUEST_SESSION.headers.update(
    {"User-Agent": "Chihafuyu-Builder/1.0 (+https://github.com/chihafuyu)"}
)

PRE_STAGE_RANKS = {"dev": 0, "alpha": 1, "pre": 2, "preview": 2, "beta": 3, "rc": 4}
VERSION_SPLIT_RE = re.compile(r"^(\d+(?:\.\d+)*)(.*)$")
PRE_STAGE_RE = re.compile(
    r"(?<![a-z])(dev|alpha|preview|pre|beta|rc)(?![a-z])", re.IGNORECASE
)

FORCE_BUNDLE_APK_TYPES = frozenset({
    "APKM", "APKS", "XAPK", "APKS_REQUIRED", "XAPK_REQUIRED",
})

APPS_TOML_ENTRY_RE = re.compile(
    r'^(?:"([^"]+)"|\'([^\']+)\'|([a-zA-Z0-9_-]+))\s*=\s*(true|false)'
)

SLUG_PATTERN = re.compile(r"[^a-z0-9]+")

TOML_HEADER = (
    "# Application Configuration\n"
    "# Change to `true` to patch, `false` to skip.\n\n"
    "[apps]\n"
)

# GitHub caps workflow_dispatch at 25 inputs. Standard patcher inputs
# consume 12 slots, so 13 checkboxes is the effective limit. Reaching
# 14 apps triggers the checkbox-to-text migration and TOML generation.
MAX_YAML_INPUTS = 13

# Maps lowercased patches-list versionCodes keys to the canonical arch
# identifiers used by the workflow inputs and scraper lookups.
_ARCH_CANONICAL_FORM = {
    "arm64_v8a": "arm64-v8a",
    "arm64-v8a": "arm64-v8a",
    "armeabi_v7a": "armeabi-v7a",
    "armeabi-v7a": "armeabi-v7a",
    "x86_64": "x86_64",
    "x86-64": "x86_64",
    "x86": "x86",
    "universal": "universal",
}


def _get_repo_root() -> str:
    """Returns the absolute path to the repository root."""
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _find_repo_map(script_dir, repo_root):
    """Find repo_map.json in expected locations."""
    candidates = [
        os.path.join(repo_root, "repo_map.json"),
        os.path.join(script_dir, "repo_map.json"),
        os.path.abspath("repo_map.json"),
    ]
    return next((p for p in candidates if os.path.exists(p)), None)


def _repo_raw_url(repo_url, branch, file_path):
    """Expands a repository URL into a raw patches-list URL.

    Explicit raw-file URLs are kept untouched. Repository URLs are
    expanded for the forge in use, so a mirror only needs its normal
    repository address. Adding a new mirror therefore never requires
    editing this script, and a forge with a different layout can always
    supply a full raw URL.
    """
    repo_url = repo_url.strip().rstrip("/")
    if not repo_url:
        return ""
    if repo_url.endswith(".git"):
        repo_url = repo_url[:-4].rstrip("/")

    parsed = urlparse(repo_url)
    path_lower = parsed.path.lower()
    if path_lower.endswith((".json", ".txt")):
        return repo_url
    if "/raw/" in path_lower or "/-/raw/" in path_lower:
        return repo_url

    branch = str(branch or "main").strip("/")
    file_path = str(file_path or "patches-list.json").strip("/")

    if not parsed.scheme:
        # Bare GitHub shorthand such as "owner/repo".
        return f"https://raw.githubusercontent.com/{repo_url}/{branch}/{file_path}"

    host = (parsed.hostname or "").lower()
    repo_path = parsed.path.strip("/")
    if host in ("github.com", "www.github.com"):
        return f"https://raw.githubusercontent.com/{repo_path}/{branch}/{file_path}"
    if "gitlab" in host:
        return f"{repo_url}/-/raw/{branch}/{file_path}"
    # Forgejo/Gitea raw layout.
    return f"{repo_url}/raw/branch/{branch}/{file_path}"


def _parse_fallback_urls(info):
    """Returns validated, expanded mirror URLs for an ecosystem source."""
    raw_urls = info.get("fallback_urls") or []
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    if not isinstance(raw_urls, list):
        return []
    branch = info.get("branch", "main")
    file_path = info.get("file_path", "patches-list.json")
    return [
        _repo_raw_url(url, branch, file_path)
        for url in raw_urls
        if isinstance(url, str)
        and url.strip().startswith(("https://", "http://"))
    ]


def _parse_repo_info(name, info, repo_root):
    """Parse repo info into a config path, URL, and target metadata."""
    config_path = info.get("config_path", f"ecosystem/{name}.json")
    abs_config_path = os.path.normpath(os.path.join(repo_root, config_path))
    release_patches = info.get("release_patches", False)
    target_package = info.get("target_package")
    target_name = info.get("target_name")
    ignore_packages = info.get("ignore_packages", [])
    ignore_patches = info.get("ignore_patches", [])

    if "aggregate_repos" in info:
        urls = [
            _repo_raw_url(repo, "main", "patches-list.json")
            for repo in info["aggregate_repos"]
        ]
        return (
            abs_config_path, urls, release_patches, target_package, target_name,
            ignore_packages, ignore_patches
        )

    repo_url = info.get("repo_url")
    if not repo_url:
        return None, None, False, None, None, [], []

    branch = info.get("branch", "main")
    file_path = info.get("file_path", "patches-list.json")
    url = _repo_raw_url(repo_url, branch, file_path)
    return (
        abs_config_path, url, release_patches, target_package, target_name,
        ignore_packages, ignore_patches
    )


def load_ecosystems():
    """Load ecosystems from repo_map.json dynamically."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(script_dir, "..", ".."))
    repo_map_path = _find_repo_map(script_dir, repo_root)
    ecosystems = {}

    if repo_map_path:
        print(f"Found repo_map.json at: {repo_map_path}")
        try:
            with open(repo_map_path, "r", encoding="utf-8") as file_obj:
                repo_map = json.load(file_obj)
            for name, info in repo_map.items():
                parsed = _parse_repo_info(name, info, repo_root)
                abs_path, url, is_rel, tgt_pkg, tgt_name, ignore_pkgs, ignore_pts = parsed
                if abs_path and url:
                    ecosystems[abs_path] = {
                        "url": url,
                        "fallback_urls": _parse_fallback_urls(info),
                        "release_patches": is_rel,
                        "target_package": tgt_pkg,
                        "target_name": tgt_name,
                        "ignore_packages": ignore_pkgs,
                        "ignore_patches": ignore_pts,
                    }
        except (OSError, json.JSONDecodeError) as err:
            print(f"Failed to load repo_map.json: {err}")
    else:
        print("repo_map.json not found in any expected location.")

    return ecosystems


def _fetch_json(url):
    """Fetch and parse JSON from a URL with error handling."""
    try:
        resp = _REQUEST_SESSION.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as err:
        print(f"  -> HTTP Error: {err}")
    except requests.exceptions.RequestException as err:
        print(f"  -> Network Error: {err}")
    except json.JSONDecodeError as err:
        print(f"  -> JSON Decode Error: {err}")
    return None


def _build_bundle_patches(pkg_name, app_name, app_version):
    """Returns the standard patches dict for a bundle metadata response."""
    return {
        "patches": [{
            "name": "Default Bundle Patch",
            "default": True,
            "compatiblePackages": [{
                "packageName": pkg_name,
                "name": app_name,
                "apkFileType": "APKM",
                "targets": [{
                    "version": app_version,
                    "isExperimental": False,
                }],
            }],
        }]
    }


def _parse_bundle_json(url, target_package=None, target_name=None):
    """Parse patches-bundle.json to extract the stable app version."""
    print(f"Fetching bundle metadata {url}...")
    data = _fetch_json(url)
    if not isinstance(data, dict):
        print("  -> Invalid JSON format: expected an object.")
        return None
    app_version = data.get("app_version")
    if not app_version:
        print("  -> Missing 'app_version' in bundle metadata.")
        return None
    pkg_name = target_package or "com.twitter.android"
    app_name = target_name or "Twitter"
    return _build_bundle_patches(pkg_name, app_name, app_version)


def _fetch_release_patches(url, target_package=None, target_name=None):
    """Fetch patches-list.json from a GitHub release asset."""
    print(f"Fetching release patches from {url}...")
    data = _fetch_json(url)
    pkg_name = target_package or "com.twitter.android"
    app_name = target_name or "Twitter"

    if isinstance(data, dict):
        app_version = data.get("app_version")
        download_url = data.get("download_url")
        if app_version and download_url:
            base_url = download_url.rsplit("/", 1)[0]
            patch_list_url = f"{base_url}/patches-list.json"
            print(f"  -> Found bundle data. Fetching from: {patch_list_url}")
            patch_data = _fetch_json(patch_list_url)
            is_plain_list = isinstance(patch_data, list) and all(
                isinstance(item, str) for item in patch_data
            )
            if is_plain_list:
                print("  -> Detected plain list format. Skipping patch sync.")
            result = _build_bundle_patches(pkg_name, app_name, app_version)
            result["is_plain_list"] = is_plain_list
            return result
        print("  -> Invalid bundle metadata format.")
        return None

    if not isinstance(data, list):
        print("  -> Invalid JSON format: expected an array.")
        return None

    patch_names = [item for item in data if isinstance(item, str)]
    return {
        "patches": [{
            "name": p_name,
            "default": True,
            "compatiblePackages": [{
                "packageName": pkg_name,
                "name": app_name,
                "apkFileType": "APKM",
                "targets": [],
            }],
        } for p_name in patch_names]
    }


def fetch_patches_list(
    url, release_patches=False, target_package=None, target_name=None,
):
    """Fetch patches-list.json or parse bundle metadata from a URL."""
    if isinstance(url, list):
        print(f"Fetching aggregated patches from {len(url)} URLs...")
        all_patches = []
        for u in url:
            data = _fetch_json(u)
            if data and isinstance(data.get("patches"), list):
                all_patches.extend(data["patches"])
        return {"patches": all_patches}

    if release_patches:
        return _fetch_release_patches(url, target_package, target_name)

    if url.endswith("patches-bundle.json"):
        return _parse_bundle_json(url, target_package, target_name)

    print(f"Fetching {url}...")
    data = _fetch_json(url)
    if not isinstance(data, dict):
        print("  -> Invalid JSON format: expected an object.")
        return None
    return data


def _normalize_vcodes(raw_vcodes):
    """Normalize versionCodes keys to the scraper's canonical arch form.

    Patches-list sources mix separators (ARM64_V8A, X86_64) while the
    workflow inputs and scraper lookups use arm64-v8a and x86_64.
    """
    if isinstance(raw_vcodes, dict):
        result = {}
        for key, val in raw_vcodes.items():
            if val is None:
                continue
            normalized = str(key).lower()
            canonical = _ARCH_CANONICAL_FORM.get(normalized, normalized)
            result[canonical] = str(val)
        return result
    if isinstance(raw_vcodes, list):
        digits = [str(v) for v in raw_vcodes if str(v).isdecimal()]
        if digits:
            return {"universal": str(max(int(d) for d in digits))}
    return {}


def _parse_version_key(ver_str):
    """Parse version string sorting stable releases above pre-release tags."""
    cleaned = ver_str.strip().lstrip("vV")
    ver_match = VERSION_SPLIT_RE.match(cleaned)
    if ver_match:
        base_part, suffix = ver_match.group(1), ver_match.group(2).lower()
    else:
        parts = re.split(r"[-_]", cleaned, maxsplit=1)
        base_part = parts[0]
        suffix = parts[1].lower() if len(parts) > 1 else ""

    base_nums = tuple(int(n) for n in re.findall(r"\d+", base_part))
    pre_match = PRE_STAGE_RE.search(suffix) or PRE_STAGE_RE.search(base_part.lower())
    if not pre_match and not suffix.strip("-_."):
        return (base_nums, 2, 0, (), cleaned)

    stage = PRE_STAGE_RANKS.get(pre_match.group(1).lower(), 5) if pre_match else 5
    is_release = 1 if stage == 5 else 0
    suffix_nums = tuple(int(n) for n in re.findall(r"\d+", suffix))
    return (base_nums, is_release, stage, suffix_nums, cleaned)


def _get_target_sort_key(target):
    """Return sort key prioritizing semantic version then version code."""
    vcodes = target.get("versionCodes")
    if isinstance(vcodes, dict):
        raw_values = vcodes.values()
    elif isinstance(vcodes, list):
        raw_values = vcodes
    else:
        raw_values = ()
    codes = [int(v) for v in raw_values if str(v).isdecimal()]
    highest_code = max(codes) if codes else -1
    ver_str = str(target.get("version") or "")
    return (_parse_version_key(ver_str), highest_code)


def _parse_patch_packages(patch):
    """Extract the packages list from a patch, handling multiple formats."""
    packages = patch.get("compatibility")
    if isinstance(packages, list):
        return packages
    packages = patch.get("compatiblePackages")
    if isinstance(packages, list):
        return packages
    if isinstance(packages, dict):
        result = []
        for pkg_name, pkg_val in packages.items():
            if not isinstance(pkg_name, str):
                continue
            targets = []
            if isinstance(pkg_val, list):
                for ver in pkg_val:
                    if isinstance(ver, str):
                        targets.append({
                            "version": ver,
                            "isExperimental": False,
                            "versionCodes": None,
                        })
            result.append({"packageName": pkg_name, "targets": targets})
        return result
    return []


def _collect_patch_options(patch):
    """Collect boolean option defaults whose upstream value is false.

    Option entries share the {key, default} shape across ecosystems;
    non-boolean options (paths, enums) and true defaults are left out
    because the CLI already applies them verbatim from options-create.
    """
    result = {}
    for opt in patch.get("options") or []:
        if not isinstance(opt, dict):
            continue
        key = opt.get("key")
        if isinstance(key, str) and key and opt.get("default") is False:
            result[key] = False
    return result


def _process_package(pkg_data, pkg, patch_name, default_enabled, patch_options):
    """Process a single compatible package entry."""
    pkg_name = pkg.get("packageName")
    if not pkg_name:
        return
    entry = pkg_data.setdefault(pkg_name, {
        "package": pkg_name,
        "name": pkg.get("name") or pkg_name,
        "all_targets": [],
        "all_targets_experimental": [],
        "patches": {},
        "patch_options": {},
        "apk_file_type": pkg.get("apkFileType"),
    })
    if patch_name:
        entry["patches"][patch_name] = default_enabled
        if patch_options:
            entry["patch_options"].setdefault(patch_name, {}).update(patch_options)

    for target in pkg.get("targets") or []:
        if not isinstance(target, dict):
            continue
        is_exp = target.get("isExperimental", target.get("experimental", False))
        bucket = "all_targets_experimental" if is_exp else "all_targets"
        if target not in entry[bucket]:
            entry[bucket].append(target)


def _extract_pkg_data(patches_data, ignore_patches=None):
    """Extract package data and compatible patches from patches list."""
    pkg_data = {}
    if not patches_data:
        return pkg_data

    ignore_set = set(ignore_patches or [])

    for patch in patches_data.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        patch_name = patch.get("name")

        if patch_name in ignore_set:
            print(f"    -> Ignoring excluded patch: {patch_name}")
            continue

        default_enabled = patch.get("default", patch.get("use", False))
        bool_options = _collect_patch_options(patch)
        packages = _parse_patch_packages(patch)
        for pkg in packages:
            if isinstance(pkg, dict):
                _process_package(
                    pkg_data, pkg, patch_name, default_enabled, bool_options
                )
    for data in pkg_data.values():
        targets = data.pop("all_targets", [])
        experimental = data.pop("all_targets_experimental", [])
        targets.sort(key=_get_target_sort_key, reverse=True)
        data["latest_target"] = targets[0] if targets else None
        experimental.sort(key=_get_target_sort_key, reverse=True)
        data["latest_experimental"] = experimental[0] if experimental else None
    for pkg_name, data in pkg_data.items():
        latest = data.get("latest_target")
        ver = latest.get("version") if latest else "None"
        print(f"    -> Found upstream package: {pkg_name} (latest: {ver})")
    return pkg_data


def _prune_local_options(entry, upstream_opts, patch_name, app_key):
    """Drops option keys the upstream patch no longer declares.

    Returns True when the entry changed; an options map left empty is
    removed so redundant default-true entries can be cleaned up after.
    """
    local_opts = entry.get("options")
    if not isinstance(local_opts, dict):
        return False
    stale = [key for key in local_opts if key not in upstream_opts]
    for key in stale:
        del local_opts[key]
    if not local_opts:
        entry.pop("options", None)
    if not stale:
        return False
    print(
        f"    -> Removing obsolete option(s) {', '.join(sorted(stale))}"
        f" from '{patch_name}' in '{app_key}'."
    )
    return True


def _sync_app_patches(app_cfg, compatible_patches, patch_options, app_key):
    """Sync options_override entries to match the upstream patches list.

    Entries carry the upstream enabled state plus every option whose
    default is false. Locally customized values are preserved and only
    missing keys are filled in from upstream; stale keys and patches the
    upstream no longer declares are pruned.
    """
    changed = False
    options = app_cfg.get("options_override")
    if not isinstance(options, dict):
        options = {}
        app_cfg["options_override"] = options

    for patch_name in list(options.keys()):
        if patch_name not in compatible_patches:
            print(f"    -> Removing obsolete patch '{patch_name}' from '{app_key}'.")
            del options[patch_name]
            changed = True
            continue
        entry = options[patch_name]
        if not isinstance(entry, dict):
            if compatible_patches[patch_name] is True:
                print(f"    -> Removing malformed patch entry '{patch_name}' from '{app_key}'.")
                del options[patch_name]
                changed = True
            continue
        upstream_opts = patch_options.get(patch_name) or {}
        if _prune_local_options(entry, upstream_opts, patch_name, app_key):
            changed = True
        if (
            compatible_patches[patch_name] is True
            and entry.get("enabled") is not False
            and not entry.get("options")
        ):
            print(f"    -> Removing redundant patch '{patch_name}' (now default: true upstream) from '{app_key}'.")
            del options[patch_name]
            changed = True

    for patch_name, default_val in sorted(compatible_patches.items()):
        upstream_opts = patch_options.get(patch_name) or {}
        if patch_name not in options:
            if default_val is False or upstream_opts:
                entry = {"enabled": bool(default_val)}
                if upstream_opts:
                    entry["options"] = dict(upstream_opts)
                state = "true" if entry["enabled"] else "false"
                print(
                    f"    -> Adding new patch '{patch_name}' to '{app_key}'"
                    f" (enabled: {state})."
                )
                options[patch_name] = entry
                changed = True
            continue
        local_opts = options[patch_name].get("options")
        if not isinstance(local_opts, dict):
            local_opts = {}
        missing = {key: val for key, val in upstream_opts.items() if key not in local_opts}
        if missing:
            local_opts.update(missing)
            options[patch_name]["options"] = local_opts
            names = ", ".join(sorted(missing))
            print(
                f"    -> Recording option default(s) for '{patch_name}'"
                f" in '{app_key}': {names}."
            )
            changed = True
    return changed


def _update_app_version(app_key, app_cfg, pkg_info):
    """Update version data for an existing app config.

    Stable mirrors the highest non-experimental upstream target and beta
    mirrors the highest experimental one. A stale local stable skips the
    whole update, beta included, so degraded upstream lists cannot
    regress the config. Version codes mirror the upstream target 1:1:
    architectures the upstream no longer publishes are dropped instead
    of lingering with stale codes merged in from older releases.
    """
    changed = False
    latest = pkg_info.get("latest_target")
    if not latest:
        return changed
    new_version = latest.get("version")
    if not new_version:
        return changed

    latest_codes = latest.get("versionCodes")
    new_vcodes = _normalize_vcodes(latest_codes)
    target_list = [str(new_version)]

    old_stable = app_cfg.get("stable", [])
    if old_stable and isinstance(old_stable, list) and len(old_stable) > 0:
        old_version = str(old_stable[0])
        if _parse_version_key(old_version) > _parse_version_key(new_version):
            print(f"    -> Skipping downgrade for '{app_key}': {old_version} -> {new_version}.")
            return changed

    if app_cfg.get("stable") != target_list:
        print(f"    -> Updating stable version of '{app_key}' from {app_cfg.get('stable')} to {target_list}.")
        app_cfg["stable"] = target_list
        changed = True

    latest_experimental = pkg_info.get("latest_experimental") or {}
    experimental_version = str(latest_experimental.get("version") or "").strip()
    if experimental_version and app_cfg.get("beta") != [experimental_version]:
        print(
            f"    -> Updating beta version of '{app_key}' to"
            f" {experimental_version} (highest upstream experimental)."
        )
        app_cfg["beta"] = [experimental_version]
        changed = True

    if app_cfg.get("preserve_version_codes"):
        print(f"    -> Preserving local version_codes for '{app_key}' (flag is active).")
    else:
        has_explicit_null = "versionCodes" in latest and latest_codes is None

        if has_explicit_null and app_cfg.get("version_codes"):
            print(f"    -> Clearing stale version_codes for '{app_key}' (upstream: null).")
            app_cfg.pop("version_codes", None)
            changed = True

        elif new_vcodes:
            # Mirror the upstream code set 1:1 so architectures the
            # upstream dropped (e.g. a removed x86_64) do not linger
            # with stale codes from older releases.
            if app_cfg.get("version_codes") != new_vcodes:
                print(
                    f"    -> Syncing version codes of '{app_key}'"
                    " 1:1 with upstream."
                )
                app_cfg["version_codes"] = dict(new_vcodes)
                changed = True

    apk_type = (pkg_info.get("apk_file_type") or "").upper()
    if apk_type in FORCE_BUNDLE_APK_TYPES and not app_cfg.get("force_bundle"):
        print(f"    -> Setting force_bundle=True for '{app_key}'.")
        app_cfg["force_bundle"] = True
        changed = True
    return changed


def _slugify_app_name(name, package):
    """Slugifies a display name (or package) into an app key."""
    base = (name or "").strip()
    if not base:
        base = (package or "").rsplit(".", 1)[-1]
    slug = SLUG_PATTERN.sub("-", base.lower()).strip("-")
    if not slug and package:
        slug = SLUG_PATTERN.sub("-", package.lower()).strip("-")
    return slug or "unknown-app"


def _unique_app_key(pkg_info, taken_keys):
    """Returns a unique app key, appending a numeric suffix on collision."""
    base = _slugify_app_name(pkg_info.get("name") or "", pkg_info.get("package") or "")
    if base not in taken_keys:
        return base
    for i in range(2, 100):
        candidate = f"{base}-{i}"
        if candidate not in taken_keys:
            return candidate

    pkg = str(pkg_info.get("package") or base)
    pkg_hash = hashlib.md5(pkg.encode()).hexdigest()[:6]
    return f"{base}-{pkg_hash}"


def _build_new_app_config(pkg_info):
    """Builds a fresh app config entry from upstream pkg metadata."""
    latest = pkg_info.get("latest_target") or {}
    version = str(latest.get("version") or "").strip()
    vcodes = _normalize_vcodes(latest.get("versionCodes"))
    experimental = pkg_info.get("latest_experimental") or {}
    experimental_version = str(experimental.get("version") or "").strip()
    patch_options = pkg_info.get("patch_options") or {}
    options = {}
    for name, val in sorted(pkg_info["patches"].items()):
        upstream_opts = patch_options.get(name) or {}
        if val is not False and not upstream_opts:
            continue
        entry = {"enabled": bool(val)}
        if upstream_opts:
            entry["options"] = dict(upstream_opts)
        options[name] = entry
    config = {
        "package": pkg_info["package"],
        "search_term": pkg_info.get("name") or pkg_info["package"],
        "stable": [version] if version else ["Any"],
        "beta": [experimental_version] if experimental_version else [],
        "strip": True,
        "archive_id": "ZeNiX",
        "options_override": options,
    }
    if vcodes:
        config["version_codes"] = vcodes
    apk_type = (pkg_info.get("apk_file_type") or "").upper()
    if apk_type in FORCE_BUNDLE_APK_TYPES:
        config["force_bundle"] = True
    return config


def _process_existing_apps(
    app_configs, stable_pkgs, skip_patches=False, ignore_packages=None
):
    """Update existing apps, remove missing ones, add new packages.

    Returns (changed, removed_app_keys, added_apps) where added_apps maps
    app_key -> enabled_default (True when the upstream version is concrete,
    False when upstream only exposes an "Any"/null version).
    """
    if not stable_pkgs:
        print("    -> WARNING: upstream returned no packages; skipping.")
        return False, [], {}

    if ignore_packages:
        for pkg in ignore_packages:
            stable_pkgs.pop(pkg, None)

    existing_count = 0
    missing_count = 0
    for cfg in app_configs.values():
        if isinstance(cfg, dict) and cfg.get("package"):
            existing_count += 1
            if cfg["package"] not in stable_pkgs:
                missing_count += 1

    if existing_count >= 5 and missing_count > existing_count // 2:
        print(f"    -> WARNING: {missing_count}/{existing_count} apps missing from upstream "
              "(>50% threshold); skipping ecosystem to prevent accidental mass deletion.")
        return False, [], {}

    changed = False
    removed_apps = []
    existing_packages = set()

    for app_key, app_cfg in list(app_configs.items()):
        if not isinstance(app_cfg, dict):
            continue
        pkg_name = app_cfg.get("package")
        if not pkg_name:
            continue
        existing_packages.add(pkg_name)
        if pkg_name not in stable_pkgs:
            print(f"    -> Removing app '{app_key}' from ecosystem config "
                  "(no longer present in upstream patches-list).")
            del app_configs[app_key]
            removed_apps.append(app_key)
            changed = True
            continue
        pkg_info = stable_pkgs[pkg_name]
        if _update_app_version(app_key, app_cfg, pkg_info):
            changed = True
        if not skip_patches and _sync_app_patches(
            app_cfg,
            pkg_info["patches"],
            pkg_info.get("patch_options") or {},
            app_key,
        ):
            changed = True

    added_apps = {}
    for pkg_name, pkg_info in stable_pkgs.items():
        if pkg_name in existing_packages:
            continue
        latest = pkg_info.get("latest_target")
        if not latest:
            print(f"    -> Skipping new package {pkg_name}: no non-experimental version targets.")
            continue
        new_key = _unique_app_key(pkg_info, set(app_configs.keys()))
        if not new_key:
            print(f"    -> Skipping new package {pkg_name}: could not derive a unique app key.")
            continue
        concrete_version = bool(str(latest.get("version") or "").strip())
        print(f"    -> Adding new app '{new_key}' for {pkg_name} (enabled default: {concrete_version}).")
        app_configs[new_key] = _build_new_app_config(pkg_info)
        added_apps[new_key] = concrete_version
        changed = True

    return changed, removed_apps, added_apps


def _write_config_atomic(config_path, config):
    """Write updated config to a temporary file and atomically replace."""
    tmp_path = f"{config_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as file_obj:
            json.dump(config, file_obj, indent=4, ensure_ascii=False)
            file_obj.write("\n")
        os.replace(tmp_path, config_path)
        return True
    except OSError as err:
        print(f"    -> Failed to write {config_path}: {err}.")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def _extract_apps_or_none(config_path, eco_name):
    """Load the apps dict from an ecosystem config, or (None, err) on failure.

    Returns (apps, None) on success, (None, exception) on read/parse error,
    and (None, None) when the config shape is unexpected.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as file_obj:
            config = json.load(file_obj)
    except (OSError, json.JSONDecodeError) as err:
        return None, err
    if not isinstance(config, dict):
        return None, None
    eco_data = config.get(eco_name)
    if not isinstance(eco_data, dict):
        return None, None
    apps = eco_data.get("apps")
    return (apps, None) if isinstance(apps, dict) else (None, None)


def _count_apps_in_config(config_path, eco_name):
    """Count app entries declared in an ecosystem config file."""
    apps, _ = _extract_apps_or_none(config_path, eco_name)
    return len(apps) if apps else 0


def _app_enabled_by_default(app_cfg):
    """Return whether a config entry has a concrete stable version to patch."""
    stable = app_cfg.get("stable") or []
    first = stable[0] if isinstance(stable, list) and stable else ""
    return bool(first and first != "Any")


def _create_full_toml(toml_path, config_path, eco_name):
    """Create a new apps.toml populated from the current config apps."""
    if os.path.exists(toml_path):
        return False
    apps, err = _extract_apps_or_none(config_path, eco_name)
    if err:
        print(f"    -> Failed to read config for TOML creation: {err}")
        return False
    if not isinstance(apps, dict) or not apps:
        return False

    tmp_path = f"{toml_path}.tmp"
    try:
        os.makedirs(os.path.dirname(toml_path), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(TOML_HEADER)
            for app_key, app_cfg in sorted(apps.items()):
                if not isinstance(app_cfg, dict):
                    continue
                flag = "true" if _app_enabled_by_default(app_cfg) else "false"
                file_obj.write(f'"{app_key}" = {flag}\n')
        os.replace(tmp_path, toml_path)
        print(f"    -> Created {os.path.basename(toml_path)} with {len(apps)} apps")
        return True
    except OSError as err:
        print(f"    -> Failed to create {toml_path}: {err}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def _find_workflow_path(ecosystem_name):
    """Finds the workflow YAML path for an ecosystem, accounting for name differences."""
    repo_root = _get_repo_root()
    workflows_dir = os.path.join(repo_root, ".github", "workflows")
    direct = os.path.join(workflows_dir, f"{ecosystem_name}-patcher.yml")
    if os.path.isfile(direct):
        return direct
    if not os.path.isdir(workflows_dir):
        return None
    for fname in os.listdir(workflows_dir):
        if not fname.endswith("-patcher.yml"):
            continue
        candidate = os.path.join(workflows_dir, fname)
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                content = f.read()
            if re.search(
                rf'^\s+ecosystem:\s*["\']?{re.escape(ecosystem_name)}["\']?',
                content,
                re.MULTILINE
            ):
                return candidate
        except OSError:
            pass
    return None


def _remove_app_from_yaml(ecosystem_name, app_key):
    """Precisely remove an app's input, env, and if-block from the workflow YAML."""
    yaml_path = _find_workflow_path(ecosystem_name)
    if not yaml_path or not os.path.exists(yaml_path):
        return False
    with open(yaml_path, "r", encoding="utf-8") as file_obj:
        content = file_obj.read()

    if "apps_to_patch:" in content:
        return False

    original_content = content
    if_block_re = re.compile(
        rf'^[ \t]+if \[\[ "\$\{{?([A-Z0-9_]+)\}}?" == "true" \]\]; then\r?\n'
        rf'[ \t]+LIST="\$LIST,{re.escape(app_key)}"\r?\n'
        rf'[ \t]+fi\r?\n',
        re.MULTILINE,
    )
    match = if_block_re.search(content)
    if not match:
        return False
    env_var = match.group(1)
    content = if_block_re.sub("", content)

    env_line_re = re.compile(
        rf'^[ \t]+{env_var}: \$\{{\{{ inputs\.[a-zA-Z0-9_]+ \}}\}}\r?\n',
        re.MULTILINE,
    )
    env_match = env_line_re.search(content)
    if env_match:
        input_name_match = re.search(r"inputs\.([a-zA-Z0-9_]+)", env_match.group(0))
        if input_name_match:
            input_name = input_name_match.group(1)
            content = env_line_re.sub("", content)
            input_block_re = re.compile(
                rf'^[ \t]+{input_name}:\r?\n'
                rf'[ \t]+description: .*\r?\n'
                rf'[ \t]+required: true\r?\n'
                rf'[ \t]+type: boolean\r?\n'
                rf'[ \t]+default: (true|false)\r?\n',
                re.MULTILINE,
            )
            content = input_block_re.sub("", content)

    if content != original_content:
        # Atomic write to prevent YAML corruption if runner dies mid-write
        tmp_path = f"{yaml_path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(content)
            os.replace(tmp_path, yaml_path)
            print(f"    -> Removed app '{app_key}' from {ecosystem_name}-patcher.yml")
            return True
        except OSError as err:
            print(f"    -> Failed to safely write {yaml_path}: {err}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    return False


def _migrate_to_text_input(ecosystem_name, yaml_path):
    """Convert a checkbox-style workflow to a text-input workflow.

    This is a destructive operation by design, triggered when apps exceed
    the GitHub Actions checkbox limit (MAX_YAML_INPUTS). The previous
    YAML content above 'call-patcher:' is completely replaced, while
    'call-patcher:' and everything below it is preserved.
    """
    with open(yaml_path, "r", encoding="utf-8") as file_obj:
        content = file_obj.read()

    name_match = re.search(r"^name:\s*(.*)$", content, re.MULTILINE)
    workflow_name = name_match.group(1) if name_match else f"{ecosystem_name} Patcher"

    call_patcher_match = re.search(r"^  call-patcher:", content, re.MULTILINE)
    if not call_patcher_match:
        return False
    call_patcher_block = content[call_patcher_match.start():]

    eco_match = re.search(
        r'^\s+ecosystem:\s*["\']([^"\'\r\n\s]+)["\']',
        call_patcher_block,
        re.MULTILINE
    )
    if eco_match and not eco_match.group(1).startswith("$"):
        ecosystem_name = eco_match.group(1)

    # The f-string formatting requires double braces (e.g., {{eco}}) for
    # literal curly braces that must survive into the generated YAML.
    new_yaml = f"""---
name: {workflow_name}

"on":
  workflow_dispatch:
    inputs:
      apps_to_patch:
        description: >-
          Type "all" to follow config/apps-{ecosystem_name}.toml,
          or type the name of a specific app (eg: 1weather,3way)
        required: true
        type: string
        default: 'all'
      architecture:
        description: 'Target Architecture'
        required: true
        type: choice
        default: 'arm64-v8a'
        options: [arm64-v8a, armeabi-v7a, x86_64, x86, universal]
      version_selection:
        description: 'App Version Selection'
        required: true
        type: choice
        default: 'Latest Recommended'
        options: [Latest Recommended, Beta, Custom]
      custom_version:
        description: 'Custom App Version'
        required: false
        type: string
        default: ''
      custom_version_code:
        description: 'Custom Version Code (blank for auto)'
        required: false
        type: string
        default: ''
      download_source:
        description: 'Download Source'
        required: true
        type: choice
        default: 'default'
        options:
          - default
          - google_play
          - huggingface
          - archive
          - apkmirror
          - apkpure
      continue_on_error:
        description: 'Debug: Continue on patch error'
        required: true
        type: boolean
        default: false
      cli_track:
        description: 'Morphe CLI Track'
        required: true
        type: choice
        default: 'Stable'
        options: [Stable, Pre-release, Custom]
      custom_cli_version:
        description: 'Custom CLI Version (e.g. v1.16.0)'
        required: false
        type: string
        default: ''
      patches_track:
        description: 'Ecosystem Patches Track'
        required: true
        type: choice
        default: 'Stable'
        options: [Stable, Pre-release, Custom Local]
      upload_github_release:
        description: 'Upload to GitHub Release'
        required: true
        type: boolean
        default: true
      upload_huggingface:
        description: 'Upload to HuggingFace Dataset'
        required: true
        type: boolean
        default: false
      send_to_telegram:
        description: 'Send to Telegram'
        required: true
        type: boolean
        default: false

permissions: {{}}

jobs:
  prepare-apps:
    runs-on: ubuntu-latest
    outputs:
      selected: ${{{{ steps.build-list.outputs.apps }}}}
    steps:
      - name: Checkout Repository
        uses: actions/checkout@v7

      - name: Set up Python
        uses: actions/setup-python@v7.1.0
        with:
          python-version: '3.14'

      - name: Validate and Normalize App List
        id: build-list
        env:
          APPS_RAW: ${{{{ inputs.apps_to_patch }}}}
          ECO_NAME: "{ecosystem_name}"
        run: |
          set -euo pipefail

          ECO="$ECO_NAME"
          CONFIG="ecosystem/${{ECO}}.json"
          TOML_CONFIG="config/apps-${{ECO}}.toml"

          if [ ! -f "$CONFIG" ]; then
            echo "::error::Config not found: $CONFIG"
            exit 1
          fi

          SELECTED=$(
            CONFIG_PATH="$CONFIG" \
            TOML_PATH="$TOML_CONFIG" \
            ECO_NAME="$ECO" \
            APPS_RAW="$APPS_RAW" \
            python3 -c "
          import os, json, sys

          try:
              import tomllib
          except ImportError:
              print(
                  '::error::Python 3.11+ required for tomllib',
                  file=sys.stderr
              )
              sys.exit(1)

          eco = os.environ['ECO_NAME']
          config_path = os.environ['CONFIG_PATH']
          toml_path = os.environ['TOML_PATH']
          raw_input = os.environ.get('APPS_RAW', '').strip()

          with open(config_path) as f:
              valid_apps = json.load(f)[eco]['apps']

          selected = []

          if raw_input.lower() == 'all':
              print(
                  f'[INFO] Mode: Reading from {{toml_path}}',
                  file=sys.stderr
              )
              if not os.path.exists(toml_path):
                  print(
                      f'::error::TOML file not found: {{toml_path}}',
                      file=sys.stderr
                  )
                  sys.exit(1)

              with open(toml_path, 'rb') as f:
                  toml_data = tomllib.load(f)

              apps_dict = toml_data.get('apps', {{}})
              selected = [k for k, v in apps_dict.items() if v is True]

              if not selected:
                  print(
                      f'::error::No apps enabled in {{toml_path}}',
                      file=sys.stderr
                  )
                  sys.exit(1)

          else:
              print('[INFO] Mode: Manual Input', file=sys.stderr)
              selected = [a.strip() for a in raw_input.split(',') if a.strip()]

          invalid = [app for app in selected if app not in valid_apps]
          if invalid:
              print(
                  f'::error::The following apps were not found in '
                  f'{{config_path}}: {{invalid}}',
                  file=sys.stderr
              )
              print('Valid apps:', file=sys.stderr)
              for v in sorted(valid_apps):
                  print(f'  - {{v}}', file=sys.stderr)
              sys.exit(1)

          print(','.join(selected))
          ")

          if [ -z "$SELECTED" ]; then
            echo "::error::No apps selected."
            exit 1
          fi

          echo "Apps to patch: $SELECTED"

          {{
            echo "apps<<EOF"
            echo "$SELECTED"
            echo "EOF"
          }} >> "$GITHUB_OUTPUT"

{call_patcher_block}"""

    # Atomic write to ensure we don't leave a half-written YAML file if the runner aborts
    tmp_path = f"{yaml_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(new_yaml)
        os.replace(tmp_path, yaml_path)
        print(
            f"    -> Migrated {ecosystem_name} to text-input YAML "
            f"(exceeded {MAX_YAML_INPUTS} app checkboxes)"
        )
        return True
    except OSError as err:
        print(f"    -> Failed to safely migrate YAML {yaml_path}: {err}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def _insert_yaml_block(
    content,
    regex,
    new_block,
    app_key,
    *,
    group_idx,
    fallback_pattern,
    fallback_at_end=False,
):
    """Helper for _add_app_to_yaml to keep branch count low and code clean."""
    existing_blocks = list(regex.finditer(content))
    normalized_key = app_key.replace("-", "_").replace(".", "_")

    if existing_blocks:
        insert_pos = None
        for match in existing_blocks:
            current_app = match.group(group_idx).lower()
            if normalized_key < current_app:
                insert_pos = match.start()
                break
        if insert_pos is None:
            insert_pos = existing_blocks[-1].end()
        return content[:insert_pos] + new_block + content[insert_pos:]

    match = fallback_pattern.search(content)
    if match:
        pos = match.end() if fallback_at_end else match.start()
        return content[:pos] + new_block + content[pos:]
    return None


def _add_app_to_yaml(ecosystem_name, app_key):
    """Insert a per-app input/env/if-block into a checkbox workflow in alphabetical order."""
    yaml_path = _find_workflow_path(ecosystem_name)
    if not yaml_path or not os.path.exists(yaml_path):
        print(f"    -> WARNING: Workflow for {ecosystem_name} not found.")
        return False
    try:
        with open(yaml_path, "r", encoding="utf-8") as file_obj:
            content = file_obj.read()
    except OSError as err:
        print(f"    -> Failed to read {yaml_path}: {err}")
        return False

    if "apps_to_patch:" in content:
        print(f"    -> {ecosystem_name} uses text input; no per-app YAML entry needed.")
        return False

    input_regex = re.compile(
        r"^( {6})app_([a-zA-Z0-9_]+):\r?\n"
        r"(?:\1  .*\r?\n)*",
        re.MULTILINE
    )
    existing_input_blocks = list(input_regex.finditer(content))

    if len(existing_input_blocks) >= MAX_YAML_INPUTS:
        if _migrate_to_text_input(ecosystem_name, yaml_path):
            return True
        print(f"    -> WARNING: Failed to migrate {ecosystem_name} to text-input YAML.")
        return False

    newline = "\r\n" if "\r\n" in content else "\n"
    input_name = "app_" + app_key.lower().replace("-", "_").replace(".", "_")
    env_var = "APP_" + app_key.upper().replace("-", "_").replace(".", "_")

    if re.search(rf"\n[ \t]+{input_name}:", content):
        return False

    original_content = content

    input_block = (
        f"      {input_name}:{newline}"
        f"        description: 'Patch {app_key}'{newline}"
        f"        required: true{newline}"
        f"        type: boolean{newline}"
        f"        default: true{newline}"
    )
    arch_pattern = re.compile(r"^ {6}architecture:\r?\n", re.MULTILINE)
    new_content = _insert_yaml_block(
        content,
        input_regex,
        input_block,
        app_key,
        group_idx=2,
        fallback_pattern=arch_pattern,
    )
    if new_content is None:
        print("    -> WARNING: Failed to locate input insertion point.")
        return False
    content = new_content

    env_regex = re.compile(
        r"^( {6})APP_([A-Z0-9_]+): \$\{\{ inputs\.app_[a-z0-9_]+ \}\}\r?\n",
        re.MULTILINE
    )
    env_line = f"      {env_var}: ${{{{ inputs.{input_name} }}}}{newline}"
    env_header_pattern = re.compile(r"^[ \t]+env:\r?\n", re.MULTILINE)
    new_content = _insert_yaml_block(
        content,
        env_regex,
        env_line,
        app_key,
        group_idx=2,
        fallback_pattern=env_header_pattern,
        fallback_at_end=True,
    )
    if new_content is None:
        print("    -> WARNING: Failed to locate env block in YAML.")
        return False
    content = new_content

    if_regex = re.compile(
        r"^[ \t]+if \[\[ \"\$APP_([A-Z0-9_]+)\" == \"true\" \]\]; then\r?\n"
        r"[ \t]+LIST=\"\$LIST,[^\"]+\"\r?\n"
        r"[ \t]+fi\r?\n",
        re.MULTILINE
    )
    if_block = (
        f'          if [[ "${env_var}" == "true" ]]; then{newline}'
        f'            LIST="$LIST,{app_key}"{newline}'
        f"          fi{newline}"
    )
    list_pattern = re.compile(r'^[ \t]+LIST="\$\{LIST#,}"\r?\n', re.MULTILINE)
    new_content = _insert_yaml_block(
        content,
        if_regex,
        if_block,
        app_key,
        group_idx=1,
        fallback_pattern=list_pattern,
    )
    if new_content is None:
        print("    -> WARNING: Failed to locate LIST append block in YAML.")
        return False
    content = new_content

    if content == original_content:
        return False

    tmp_path = f"{yaml_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(content)
        os.replace(tmp_path, yaml_path)
        print(f"    -> Added '{app_key}' (alphabetical insertion)")
        return True
    except OSError as err:
        print(f"    -> Failed to safely write {yaml_path}: {err}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def _rebuild_toml_index(lines):
    """Returns (header_idx, entries) for the [apps] section of apps.toml."""
    header_idx = None
    entries = {}
    in_apps_section = False

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        section_name = stripped.split("#", 1)[0].strip()
        if section_name.startswith("["):
            if section_name == "[apps]":
                header_idx = i
                in_apps_section = True
            else:
                in_apps_section = False
            continue

        if in_apps_section:
            match = APPS_TOML_ENTRY_RE.match(stripped)
            if match:
                key = match.group(1) or match.group(2) or match.group(3)
                entries[key] = i
    return header_idx, entries


def _insert_toml_entry(lines, entries, header_idx, app_key, flag):
    """Helper for _update_apps_toml to lower branch count."""
    new_line = f'"{app_key}" = {flag}\n'

    insert_idx = None
    for existing_key in sorted(entries.keys()):
        if app_key < existing_key:
            insert_idx = entries[existing_key]
            break
    if insert_idx is None:
        if entries:
            insert_idx = entries[max(entries, key=entries.get)] + 1
        else:
            insert_idx = header_idx + 1

    if insert_idx > 0 and not lines[insert_idx - 1].endswith("\n"):
        lines[insert_idx - 1] += "\n"

    lines.insert(insert_idx, new_line)
    return lines


def _collect_toml_delta(toml_path, config_path, eco_name):
    """Return (missing_apps, stale_apps) between config and an existing TOML.

    Existing TOML files can predate later config growth; incremental
    added/removed tracking alone cannot repair those gaps.
    """
    apps, err = _extract_apps_or_none(config_path, eco_name)
    if err:
        print(f"    -> Failed to read config for TOML sync: {err}")
        return {}, []
    if not isinstance(apps, dict) or not apps:
        return {}, []

    try:
        with open(toml_path, "r", encoding="utf-8") as file_obj:
            lines = file_obj.readlines()
    except OSError as err:
        print(f"    -> Failed to read {toml_path} for TOML sync: {err}")
        return {}, []

    header_idx, entries = _rebuild_toml_index(lines)
    if header_idx is None:
        return {}, []

    missing_apps = {}
    for app_key, app_cfg in apps.items():
        if app_key in entries or not isinstance(app_cfg, dict):
            continue
        missing_apps[app_key] = _app_enabled_by_default(app_cfg)
    stale_apps = sorted(set(entries) - set(apps))
    return missing_apps, stale_apps


def _update_apps_toml(toml_path, added_apps, removed_apps):
    """Add or remove apps in an existing config/apps-[eco].toml."""
    if not os.path.exists(toml_path):
        return False

    if not added_apps and not removed_apps:
        return False

    try:
        with open(toml_path, "r", encoding="utf-8") as file_obj:
            lines = file_obj.readlines()
    except OSError as err:
        print(f"    -> Failed to read {toml_path}: {err}")
        return False

    header_idx, entries = _rebuild_toml_index(lines)
    if header_idx is None:
        print(f"    -> {toml_path} missing [apps] section; skipping.")
        return False

    changed = False
    for app_key in removed_apps:
        if app_key not in entries:
            continue
        del lines[entries[app_key]]
        print(f"    -> Removed '{app_key}' from {os.path.basename(toml_path)}")
        changed = True
        header_idx, entries = _rebuild_toml_index(lines)

    for app_key in sorted(added_apps.keys()):
        if app_key in entries:
            continue
        flag = "true" if added_apps[app_key] else "false"
        lines = _insert_toml_entry(lines, entries, header_idx, app_key, flag)
        print(f"    -> Added '{app_key}' to {os.path.basename(toml_path)} (enabled: {flag})")
        changed = True
        header_idx, entries = _rebuild_toml_index(lines)

    if not changed:
        return False

    tmp_path = f"{toml_path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as file_obj:
            file_obj.writelines(lines)
        os.replace(tmp_path, toml_path)
    except OSError as err:
        print(f"    -> Failed to safely write {toml_path}: {err}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False
    return True


def update_config(config_path, stable_data, ignore_packages=None, ignore_patches=None):
    """Update a single config file based on patches-list data."""
    if stable_data is None:
        print(f"  -> Skipping {config_path} due to missing data.")
        return False, [], {}

    print(f"  -> Processing {config_path}...")
    try:
        with open(config_path, "r", encoding="utf-8") as file_obj:
            config = json.load(file_obj)
    except (OSError, json.JSONDecodeError) as err:
        print(f"    -> Failed to read {config_path}: {err}. Skipping.")
        return False, [], {}

    root_key = os.path.splitext(os.path.basename(config_path))[0]
    if not isinstance(config, dict) or not isinstance(config.get(root_key), dict):
        print(f"    -> Root key '{root_key}' not found or invalid. Skipping.")
        return False, [], {}

    app_configs = config[root_key].get("apps")
    if not isinstance(app_configs, dict):
        app_configs = {}
        config[root_key]["apps"] = app_configs

    stable_pkgs = _extract_pkg_data(stable_data, ignore_patches)
    skip_patches = stable_data.get("is_plain_list", False)

    changed, removed_apps, added_apps = _process_existing_apps(
        app_configs, stable_pkgs, skip_patches, ignore_packages
    )
    if changed and (added_apps or removed_apps):
        config[root_key]["apps"] = dict(sorted(app_configs.items()))

    if changed:
        if _write_config_atomic(config_path, config):
            print(f"  -> File {config_path} updated.")
        else:
            changed = False
            removed_apps = []
            added_apps = {}
    else:
        print(f"  -> No changes for {config_path}.")
    return changed, removed_apps, added_apps


def _is_patches_payload(data):
    """Returns True when fetched data has the expected patches-list shape."""
    return isinstance(data, dict) and isinstance(data.get("patches"), list)


def _process_ecosystem(config_path, ecosystem_info):
    """Process a single ecosystem and return the fetched data.

    GitHub accounts can be suspended without notice. Every source may
    declare ``fallback_urls`` mirrors on another forge; they are tried
    in order when the primary GitHub raw URL fails.
    """
    if not os.path.exists(config_path):
        print(f"Skipping '{config_path}': Config file not found in the repository.")
        return None

    fallback_urls = ecosystem_info.get("fallback_urls") or []
    urls = [ecosystem_info["url"], *fallback_urls]

    for index, url in enumerate(urls):
        if index:
            print(
                f"  -> Primary source unavailable; trying fallback "
                f"{index}/{len(fallback_urls)}: {url}"
            )
        stable_data = fetch_patches_list(
            url,
            release_patches=ecosystem_info["release_patches"],
            target_package=ecosystem_info.get("target_package"),
            target_name=ecosystem_info.get("target_name"),
        )
        if _is_patches_payload(stable_data):
            return stable_data
        if stable_data is not None:
            print(f"  -> Invalid upstream payload from {url}; ignoring.")

    print(
        f"Skipping '{config_path}': Failed to fetch or parse upstream "
        "data from all sources (see errors above)."
    )
    return None


def _sync_workflow_and_toml(
    eco_name,
    config_path,
    app_count,
    *,
    added_apps,
    removed_apps,
    changed,
):
    """Helper for main() to handle workflow and TOML synchronization."""
    any_change = False
    yaml_path = _find_workflow_path(eco_name)
    toml_path = os.path.join(_get_repo_root(), "config", f"apps-{eco_name}.toml")
    text_input = False

    # Proactively recover broken workflows: enforce YAML migration if the limit is exceeded,
    # even if no new apps were added in the current execution cycle.
    if yaml_path and os.path.exists(yaml_path):
        try:
            with open(yaml_path, "r", encoding="utf-8") as yf:
                yaml_content = yf.read()

            if "apps_to_patch:" in yaml_content:
                text_input = True
            else:
                inputs_sec = yaml_content.split("permissions:")[0] if "permissions:" in yaml_content else yaml_content
                tot_inputs = len(re.findall(r"^ {6}[a-zA-Z0-9_]+:\r?\n", inputs_sec, re.MULTILINE))
                if app_count > MAX_YAML_INPUTS or tot_inputs > 25:
                    print(
                        f"  -> {eco_name} exceeds input limit "
                        f"(apps: {app_count}, total inputs: {tot_inputs}). "
                        f"Forcing migration to text input."
                    )
                    if _migrate_to_text_input(eco_name, yaml_path):
                        text_input = True
                        any_change = True
        except OSError as err:
            print(f"  -> Failed to read {yaml_path} for migration check: {err}")

    # Text-input workflows need a TOML even below the checkbox limit. Existing
    # TOMLs are reconciled even without incremental changes so entries missed
    # when the file was first created are repaired.
    if app_count > MAX_YAML_INPUTS or text_input or os.path.exists(toml_path):
        if not os.path.exists(toml_path):
            if _create_full_toml(toml_path, config_path, eco_name):
                any_change = True
        else:
            missing_apps, stale_apps = _collect_toml_delta(
                toml_path, config_path, eco_name
            )
            sync_added = dict(missing_apps)
            sync_added.update(added_apps)
            sync_removed = sorted(set(removed_apps) | set(stale_apps))
            if sync_added or sync_removed:
                if _update_apps_toml(toml_path, sync_added, sync_removed):
                    any_change = True

    if changed:
        any_change = True
        for app_key in removed_apps:
            _remove_app_from_yaml(eco_name, app_key)
        for app_key in added_apps:
            _add_app_to_yaml(eco_name, app_key)

    return any_change


def main():
    """Run the auto-update process for all configured ecosystems."""
    ecosystems = load_ecosystems()
    if not ecosystems:
        print(
            "No ecosystems loaded. Check repo_map.json; refusing to run "
            "without an explicit source list."
        )
        sys.exit(1)
    fetched_ecosystems = []
    for config_path, info in ecosystems.items():
        stable_data = _process_ecosystem(config_path, info)
        if stable_data is not None:
            fetched_ecosystems.append((config_path, stable_data, info))

    any_change = False
    for config_path, stable_data, info in fetched_ecosystems:
        ignore_pkgs = info.get("ignore_packages", [])
        ignore_pts = info.get("ignore_patches", [])
        changed, removed_apps, added_apps = update_config(
            config_path, stable_data, ignore_pkgs, ignore_pts
        )

        eco_name = os.path.splitext(os.path.basename(config_path))[0]
        app_count = _count_apps_in_config(config_path, eco_name)

        if _sync_workflow_and_toml(
            eco_name,
            config_path,
            app_count,
            added_apps=added_apps,
            removed_apps=removed_apps,
            changed=changed,
        ):
            any_change = True

    if not any_change:
        print("No updates found across all ecosystems.")
        sys.exit(0)


if __name__ == "__main__":
    main()
