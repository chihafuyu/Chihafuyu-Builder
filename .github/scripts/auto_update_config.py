"""
Auto-update ecosystem configs from patches-list.json or bundle metadata.
Run by GitHub Actions to create a Pull Request.
"""

import hashlib
import json
import os
import re
import sys

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

# Matches TOML entries with double quotes, single quotes, or no quotes
APPS_TOML_ENTRY_RE = re.compile(
    r'^(?:"([^"]+)"|\'([^\']+)\'|([a-zA-Z0-9_-]+))\s*=\s*(true|false)'
)

SLUG_PATTERN = re.compile(r"[^a-z0-9]+")

# GitHub caps workflow_dispatch at 25 inputs; 15 leaves room for standard inputs
MAX_YAML_INPUTS = 15


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


def _parse_repo_info(name, info, repo_root):
    """Parse repo info into a config path, URL, and target metadata."""
    config_path = info.get("config_path", f"ecosystem/{name}.json")
    abs_config_path = os.path.normpath(os.path.join(repo_root, config_path))
    release_patches = info.get("release_patches", False)
    target_package = info.get("target_package")
    target_name = info.get("target_name")
    ignore_packages = info.get("ignore_packages", [])

    if "aggregate_repos" in info:
        urls = [
            f"https://raw.githubusercontent.com/{repo}/main/patches-list.json"
            for repo in info["aggregate_repos"]
        ]
        return (
            abs_config_path, urls, release_patches, target_package, target_name, ignore_packages
        )

    repo_url = info.get("repo_url")
    if not repo_url:
        return None, None, False, None, None, []

    branch = info.get("branch", "main")
    file_path = info.get("file_path", "patches-list.json")
    url = f"https://raw.githubusercontent.com/{repo_url}/{branch}/{file_path}"
    return abs_config_path, url, release_patches, target_package, target_name, ignore_packages


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
                abs_path, url, is_rel, tgt_pkg, tgt_name, ignore_pkgs = parsed
                if abs_path and url:
                    ecosystems[abs_path] = {
                        "url": url,
                        "release_patches": is_rel,
                        "target_package": tgt_pkg,
                        "target_name": tgt_name,
                        "ignore_packages": ignore_pkgs,
                    }
        except (OSError, json.JSONDecodeError) as err:
            print(f"Failed to load repo_map.json: {err}")
    else:
        print("repo_map.json not found in any expected location.")

    if not ecosystems:
        print("Using fallback ecosystems (morphe & kveld9).")
        ecosystems = {
            os.path.join(repo_root, "ecosystem/morphe.json"): {
                "url": (
                    "https://raw.githubusercontent.com/MorpheApp/"
                    "morphe-patches/main/patches-list.json"
                ),
                "release_patches": False,
                "target_package": None,
                "target_name": None,
                "ignore_packages": [],
            },
            os.path.join(repo_root, "ecosystem/kveld9.json"): {
                "url": (
                    "https://raw.githubusercontent.com/kveld9/"
                    "kveld-morphe-patches/main/patches-list.json"
                ),
                "release_patches": False,
                "target_package": None,
                "target_name": None,
                "ignore_packages": [],
            },
        }
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
                    "versionCodes": None,
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
    """Normalize versionCodes and convert underscores to hyphens."""
    if isinstance(raw_vcodes, dict):
        return {
            str(k).lower().replace("_", "-"): str(v)
            for k, v in raw_vcodes.items() if v is not None
        }
    if isinstance(raw_vcodes, list):
        valid = [v for v in raw_vcodes if v is not None]
        if valid:
            highest = max(valid, key=lambda v: int(v) if str(v).isdigit() else -1)
            return {"universal": str(highest)}
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
        return (base_nums, 2, 0, (), ver_str)

    stage = PRE_STAGE_RANKS.get(pre_match.group(1).lower(), 5) if pre_match else 5
    is_release = 1 if stage == 5 else 0
    suffix_nums = tuple(int(n) for n in re.findall(r"\d+", suffix))
    return (base_nums, is_release, stage, suffix_nums, ver_str)


def _get_target_sort_key(target):
    """Return sort key prioritizing semantic version then version code."""
    vcodes = target.get("versionCodes")
    if isinstance(vcodes, dict):
        raw_values = vcodes.values()
    elif isinstance(vcodes, list):
        raw_values = vcodes
    else:
        raw_values = ()
    codes = [int(v) for v in raw_values if str(v).isdigit()]
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


def _process_package(pkg_data, pkg, patch_name, default_enabled):
    """Process a single compatible package entry."""
    pkg_name = pkg.get("packageName")
    if not pkg_name:
        return
    if pkg_name not in pkg_data:
        pkg_data[pkg_name] = {
            "package": pkg_name,
            "name": pkg.get("name") or pkg_name,
            "all_targets": [],
            "patches": {},
            "apk_file_type": pkg.get("apkFileType"),
        }
    if patch_name:
        pkg_data[pkg_name]["patches"][patch_name] = default_enabled

    valid_targets = []
    for t in pkg.get("targets") or []:
        if not isinstance(t, dict):
            continue
        is_exp = t.get("isExperimental", t.get("experimental", False))
        if not is_exp:
            valid_targets.append(t)
    for target in valid_targets:
        if target not in pkg_data[pkg_name]["all_targets"]:
            pkg_data[pkg_name]["all_targets"].append(target)


def _extract_pkg_data(patches_data):
    """Extract package data and compatible patches from patches list."""
    pkg_data = {}
    if not patches_data:
        return pkg_data
    for patch in patches_data.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        patch_name = patch.get("name")
        default_enabled = patch.get("default", patch.get("use", False))
        packages = _parse_patch_packages(patch)
        for pkg in packages:
            if isinstance(pkg, dict):
                _process_package(pkg_data, pkg, patch_name, default_enabled)
    for data in pkg_data.values():
        targets = data.pop("all_targets", [])
        if not targets:
            data["latest_target"] = None
            continue
        targets.sort(key=_get_target_sort_key, reverse=True)
        data["latest_target"] = targets[0]
    for pkg_name, data in pkg_data.items():
        latest = data.get("latest_target")
        ver = latest.get("version") if latest else "None"
        print(f"    -> Found upstream package: {pkg_name} (latest: {ver})")
    return pkg_data


def _sync_app_patches(app_cfg, compatible_patches, app_key):
    """Sync patches in options_override to match upstream 1:1."""
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
        elif compatible_patches[patch_name] is True:
            local_opt = options[patch_name]
            if isinstance(local_opt, dict) and local_opt.get("enabled") is False:
                continue
            print(f"    -> Removing redundant patch '{patch_name}' "
                  f"(now default: true upstream) from '{app_key}'.")
            del options[patch_name]
            changed = True

    for patch_name, default_val in sorted(compatible_patches.items()):
        if patch_name not in options and default_val is False:
            print(f"    -> Adding new patch '{patch_name}' "
                  f"to '{app_key}' (enabled: false).")
            options[patch_name] = {"enabled": False}
            changed = True
    return changed


def _update_app_version(app_key, app_cfg, pkg_info):
    """Update version data for an existing app config."""
    changed = False
    latest = pkg_info.get("latest_target")
    if not latest:
        return changed
    new_version = latest.get("version")
    if not new_version:
        return changed
    new_vcodes = _normalize_vcodes(latest.get("versionCodes"))
    target_list = [str(new_version)]

    old_stable = app_cfg.get("stable", [])
    if old_stable and isinstance(old_stable, list) and len(old_stable) > 0:
        old_version = str(old_stable[0])
        if _parse_version_key(old_version) > _parse_version_key(new_version):
            print(f"    -> Skipping downgrade for '{app_key}': "
                  f"{old_version} -> {new_version}.")
            return changed

    if app_cfg.get("stable") != target_list:
        print(f"    -> Updating stable version of '{app_key}' "
              f"from {app_cfg.get('stable')} to {target_list}.")
        app_cfg["stable"] = target_list
        changed = True

    if new_vcodes and app_cfg.get("version_codes") != new_vcodes:
        print(f"    -> Updating version codes for '{app_key}'.")
        app_cfg["version_codes"] = new_vcodes
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
    base = _slugify_app_name(
        pkg_info.get("name") or "", pkg_info.get("package") or ""
    )
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
    options = {
        patch_name: {"enabled": False}
        for patch_name, default_val in sorted(pkg_info["patches"].items())
        if default_val is False
    }
    config = {
        "package": pkg_info["package"],
        "search_term": pkg_info.get("name") or pkg_info["package"],
        "stable": [version] if version else ["Any"],
        "beta": [],
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

    existing_count = sum(
        1 for cfg in app_configs.values()
        if isinstance(cfg, dict) and cfg.get("package")
    )
    if existing_count >= 5:
        missing_count = sum(
            1 for cfg in app_configs.values()
            if isinstance(cfg, dict)
            and cfg.get("package")
            and cfg["package"] not in stable_pkgs
        )
        if missing_count > existing_count // 2:
            print(f"    -> WARNING: {missing_count}/{existing_count} apps "
                  "missing from upstream (>50% threshold); skipping ecosystem "
                  "to prevent accidental mass deletion.")
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
        if not skip_patches:
            if _sync_app_patches(app_cfg, pkg_info["patches"], app_key):
                changed = True

    added_apps = {}
    for pkg_name, pkg_info in stable_pkgs.items():
        if pkg_name in existing_packages:
            continue
        latest = pkg_info.get("latest_target")
        if not latest:
            print(f"    -> Skipping new package {pkg_name}: "
                  "no non-experimental version targets.")
            continue
        new_key = _unique_app_key(pkg_info, set(app_configs.keys()))
        if not new_key:
            print(f"    -> Skipping new package {pkg_name}: "
                  "could not derive a unique app key.")
            continue
        concrete_version = bool(str(latest.get("version") or "").strip())
        print(f"    -> Adding new app '{new_key}' for {pkg_name} "
              f"(enabled default: {concrete_version}).")
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


def _remove_app_from_yaml(ecosystem_name, app_key):
    """Precisely remove an app's input, env, and if-block from the workflow YAML."""
    yaml_path = os.path.join(
        _get_repo_root(), ".github", "workflows", f"{ecosystem_name}-patcher.yml"
    )
    if not os.path.exists(yaml_path):
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
        with open(yaml_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(content)
        print(f"    -> Removed app '{app_key}' from {ecosystem_name}-patcher.yml")
        return True
    return False


def _migrate_to_text_input(ecosystem_name, yaml_path):
    """Convert a checkbox-style workflow to a text-input workflow."""
    with open(yaml_path, "r", encoding="utf-8") as file_obj:
        content = file_obj.read()

    name_match = re.search(r"^name:\s*(.*)$", content, re.MULTILINE)
    workflow_name = name_match.group(1) if name_match else f"{ecosystem_name} Patcher"

    call_patcher_match = re.search(r"^  call-patcher:", content, re.MULTILINE)
    if not call_patcher_match:
        return False
    call_patcher_block = content[call_patcher_match.start():]

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
        required: false
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

          SELECTED=$(CONFIG_PATH="$CONFIG" TOML_PATH="$TOML_CONFIG" ECO_NAME="$ECO" APPS_RAW="$APPS_RAW" python3 -c "
          import os, json, sys

          try:
              import tomllib
          except ImportError:
              print('::error::Python 3.11+ required for tomllib', file=sys.stderr)
              sys.exit(1)

          eco = os.environ['ECO_NAME']
          config_path = os.environ['CONFIG_PATH']
          toml_path = os.environ['TOML_PATH']
          raw_input = os.environ.get('APPS_RAW', '').strip()

          with open(config_path) as f:
              valid_apps = json.load(f)[eco]['apps']

          selected = []

          if raw_input.lower() == 'all':
              print('[INFO] Mode: Reading from config/apps.toml', file=sys.stderr)
              if not os.path.exists(toml_path):
                  print(f'::error::TOML file not found: {{toml_path}}', file=sys.stderr)
                  sys.exit(1)

              with open(toml_path, 'rb') as f:
                  toml_data = tomllib.load(f)

              apps_dict = toml_data.get('apps', {{}})
              selected = [k for k, v in apps_dict.items() if v is True]

              if not selected:
                  print('::error::No apps enabled in apps.toml', file=sys.stderr)
                  sys.exit(1)

          else:
              print('[INFO] Mode: Manual Input', file=sys.stderr)
              selected = [a.strip() for a in raw_input.split(',') if a.strip()]

          invalid = [app for app in selected if app not in valid_apps]
          if invalid:
              print(f'::error::The following apps were not found in {{config_path}}: {{invalid}}', file=sys.stderr)
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

    with open(yaml_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(new_yaml)
    print(f"    -> Migrated {ecosystem_name} to text-input YAML (exceeded 15 app checkboxes)")
    return True


def _add_app_to_yaml(ecosystem_name, app_key):
    """Insert a per-app input/env/if-block into a checkbox-style workflow."""
    yaml_path = os.path.join(
        _get_repo_root(), ".github", "workflows", f"{ecosystem_name}-patcher.yml"
    )
    if not os.path.exists(yaml_path):
        print(f"    -> WARNING: {yaml_path} not found. If this ecosystem uses a matrix workflow, update manually.")
        return False
    try:
        with open(yaml_path, "r", encoding="utf-8") as file_obj:
            content = file_obj.read()
    except OSError as err:
        print(f"    -> Failed to read {yaml_path}: {err}")
        return False

    if "apps_to_patch:" in content:
        print(f"    -> {ecosystem_name} uses text input; "
              "no per-app YAML entry needed.")
        return False

    existing_inputs = len(re.findall(r"^ {6}app_", content, re.MULTILINE))
    if existing_inputs >= MAX_YAML_INPUTS:
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

    arch_pattern = re.compile(r"^ {6}architecture:\r?\n", re.MULTILINE)
    match = arch_pattern.search(content)
    if not match:
        print("    -> WARNING: Failed to locate 6-space 'architecture:' input in YAML; "
              "aborting to prevent invalid insertion.")
        return False

    input_block = (
        f"      {input_name}:{newline}"
        f"        description: 'Patch {app_key}'{newline}"
        f"        required: true{newline}"
        f"        type: boolean{newline}"
        f"        default: true{newline}"
    )
    content = content[:match.start()] + input_block + content[match.start():]

    env_pattern = re.compile(
        r"(^[ \t]+prepare-apps:\r?\n(?:.*\r?\n)*?"
        r"^[ \t]+env:\r?\n)"
        r"((?:^[ \t]+[A-Z0-9_]+: \$\{\{ inputs\.[^}]+\}\}\r?\n)+)",
        re.MULTILINE,
    )
    env_match = env_pattern.search(content)
    if not env_match:
        print("    -> WARNING: Failed to locate env block in YAML; "
              "aborting to prevent partial updates.")
        return False

    insert_pos = env_match.end(2)
    env_line = f"      {env_var}: ${{{{ inputs.{input_name} }}}}{newline}"
    content = content[:insert_pos] + env_line + content[insert_pos:]

    list_pattern = re.compile(r'^[ \t]+LIST="\$\{LIST#,}"\r?\n', re.MULTILINE)
    list_match = list_pattern.search(content)
    if not list_match:
        print("    -> WARNING: Failed to locate LIST append block in YAML; "
              "aborting to prevent partial updates.")
        return False

    if_block = (
        f'          if [[ "${env_var}" == "true" ]]; then{newline}'
        f'            LIST="$LIST,{app_key}"{newline}'
        f"          fi{newline}"
    )
    content = content[:list_match.start()] + if_block + content[list_match.start():]

    if content == original_content:
        return False

    try:
        with open(yaml_path, "w", encoding="utf-8") as file_obj:
            file_obj.write(content)
        print(f"    -> Added '{app_key}' to {ecosystem_name}-patcher.yml")
        return True
    except OSError as err:
        print(f"    -> Failed to write {yaml_path}: {err}")
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


def _update_apps_toml(toml_path, added_apps, removed_apps):
    """Add or remove apps in config/apps-[eco].toml while preserving structure."""
    if not os.path.exists(toml_path):
        try:
            os.makedirs(os.path.dirname(toml_path), exist_ok=True)
            with open(toml_path, "w", encoding="utf-8") as file_obj:
                file_obj.write(
                    "# Application Configuration\n"
                    "# Change to `true` to patch, `false` to skip.\n\n"
                    "[apps]\n"
                )
        except OSError as err:
            print(f"    -> Failed to create {toml_path}: {err}")
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
        print(f"    -> Added '{app_key}' to {os.path.basename(toml_path)} (enabled: {flag})")
        changed = True
        header_idx, entries = _rebuild_toml_index(lines)

    if not changed:
        return False

    try:
        with open(toml_path, "w", encoding="utf-8") as file_obj:
            file_obj.writelines(lines)
    except OSError as err:
        print(f"    -> Failed to write {toml_path}: {err}")
        return False
    return True


def update_config(config_path, stable_data, ignore_packages=None):
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

    stable_pkgs = _extract_pkg_data(stable_data)
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


def _process_ecosystem(config_path, ecosystem_info):
    """Process a single ecosystem and return the fetched data."""
    if not os.path.exists(config_path):
        print(f"Skipping '{config_path}': Config file not found in the repository.")
        return None

    stable_data = fetch_patches_list(
        ecosystem_info["url"],
        release_patches=ecosystem_info["release_patches"],
        target_package=ecosystem_info.get("target_package"),
        target_name=ecosystem_info.get("target_name"),
    )
    if stable_data is None:
        print(f"Skipping '{config_path}': Failed to fetch or parse upstream "
              "data (see error above).")
        return None
    return stable_data


def main():
    """Run the auto-update process for all configured ecosystems."""
    ecosystems = load_ecosystems()
    fetched_ecosystems = []
    for config_path, info in ecosystems.items():
        stable_data = _process_ecosystem(config_path, info)
        if stable_data is not None:
            fetched_ecosystems.append((config_path, stable_data, info))

    any_change = False
    for config_path, stable_data, info in fetched_ecosystems:
        ignore_pkgs = info.get("ignore_packages", [])
        changed, removed_apps, added_apps = update_config(
            config_path, stable_data, ignore_pkgs
        )
        if not changed:
            continue
        any_change = True
        eco_name = os.path.splitext(os.path.basename(config_path))[0]

        for app_key in removed_apps:
            if _remove_app_from_yaml(eco_name, app_key):
                any_change = True
        for app_key in added_apps:
            if _add_app_to_yaml(eco_name, app_key):
                any_change = True

        toml_path = os.path.join(_get_repo_root(), "config", f"apps-{eco_name}.toml")
        if added_apps or removed_apps:
            if _update_apps_toml(toml_path, added_apps, removed_apps):
                any_change = True

    if not any_change:
        print("No updates found across all ecosystems.")
        sys.exit(0)


if __name__ == "__main__":
    main()
