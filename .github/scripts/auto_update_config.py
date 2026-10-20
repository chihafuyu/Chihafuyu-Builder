"""
Auto-update ecosystem configs from patches-list.json or bundle metadata.
Run by GitHub Actions to create a Pull Request.
"""

import json
import os
import re
import sys

import requests

_REQUEST_SESSION = requests.Session()
_REQUEST_SESSION.headers.update(
    {"User-Agent": "Chihafuyu-Builder/1.0 (+https://github.com/chihafuyu)"}
)

PRE_STAGE_RANKS = {
    "dev": 0,
    "alpha": 1,
    "pre": 2,
    "preview": 2,
    "beta": 3,
    "rc": 4,
}
VERSION_SPLIT_RE = re.compile(r"^(\d+(?:\.\d+)*)(.*)$")
PRE_STAGE_RE = re.compile(
    r"(?<![a-z])(dev|alpha|preview|pre|beta|rc)(?![a-z])", re.IGNORECASE
)


def _find_repo_map(script_dir, repo_root):
    """Find repo_map.json in expected locations."""
    possible_paths = [
        os.path.join(repo_root, "repo_map.json"),
        os.path.join(script_dir, "repo_map.json"),
        os.path.abspath("repo_map.json"),
    ]
    return next(
        (path for path in possible_paths if os.path.exists(path)), None
    )


def _parse_repo_info(name, info, repo_root):
    """Parse repo info into a config path and URL."""
    config_path = info.get("config_path", f"ecosystem/{name}.json")
    abs_config_path = os.path.normpath(
        os.path.join(repo_root, config_path)
    )
    release_patches = info.get("release_patches", False)

    if "aggregate_repos" in info:
        urls = [
            f"https://raw.githubusercontent.com/{repo}/main/patches-list.json"
            for repo in info["aggregate_repos"]
        ]
        return abs_config_path, urls, release_patches

    repo_url = info.get("repo_url")
    if not repo_url:
        return None, None, False

    branch = info.get("branch", "main")
    file_path = info.get("file_path", "patches-list.json")
    url = (
        f"https://raw.githubusercontent.com/{repo_url}"
        f"/{branch}/{file_path}"
    )
    return abs_config_path, url, release_patches


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
                abs_config_path, url, is_release = _parse_repo_info(
                    name, info, repo_root
                )
                if abs_config_path and url:
                    ecosystems[abs_config_path] = {
                        "url": url,
                        "release_patches": is_release,
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
            },
            os.path.join(repo_root, "ecosystem/kveld9.json"): {
                "url": (
                    "https://raw.githubusercontent.com/kveld9/"
                    "kveld-morphe-patches/main/patches-list.json"
                ),
                "release_patches": False,
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


def _parse_bundle_json(url):
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

    return {
        "patches": [
            {
                "name": "Default Bundle Patch",
                "default": True,
                "compatiblePackages": [
                    {
                        "packageName": "com.twitter.android",
                        "name": "X",
                        "apkFileType": "APKM",
                        "targets": [
                            {
                                "version": app_version,
                                "isExperimental": False,
                                "versionCodes": None,
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _fetch_release_patches(url):
    """Fetch patches-list.json from a GitHub release asset."""
    print(f"Fetching release patches from {url}...")
    data = _fetch_json(url)

    if isinstance(data, dict):
        app_version = data.get("app_version")
        download_url = data.get("download_url")
        if app_version and download_url:
            # Extract base URL from download_url
            base_release_url = download_url.rsplit("/", 1)[0]
            patch_list_url = f"{base_release_url}/patches-list.json"
            print(f"  -> Found bundle data. Fetching from: {patch_list_url}")
            patch_data = _fetch_json(patch_list_url)
            if isinstance(patch_data, list):
                patch_names = [
                    item for item in patch_data if isinstance(item, str)
                ]
                return {
                    "patches": [
                        {
                            "name": p_name,
                            "default": True,
                            "compatiblePackages": [
                                {
                                    "packageName": "com.twitter.android",
                                    "name": "X",
                                    "apkFileType": "APKM",
                                    "targets": [
                                        {
                                            "version": app_version,
                                            "isExperimental": False,
                                            "versionCodes": None,
                                        }
                                    ],
                                }
                            ],
                        }
                        for p_name in patch_names
                    ]
                }
        print("  -> Invalid bundle metadata format.")
        return None

    if not isinstance(data, list):
        print("  -> Invalid JSON format: expected an array.")
        return None

    patch_names = [item for item in data if isinstance(item, str)]

    return {
        "patches": [
            {
                "name": p_name,
                "default": True,
                "compatiblePackages": [
                    {
                        "packageName": "com.twitter.android",
                        "name": "X",
                        "apkFileType": "APKM",
                        "targets": [],
                    }
                ],
            }
            for p_name in patch_names
        ]
    }


def fetch_patches_list(url, release_patches=False):
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
        return _fetch_release_patches(url)

    if url.endswith("patches-bundle.json"):
        return _parse_bundle_json(url)

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
            for k, v in raw_vcodes.items()
            if v is not None
        }
    if isinstance(raw_vcodes, list):
        valid = [v for v in raw_vcodes if v is not None]
        if valid:
            highest = max(
                valid, key=lambda v: int(v) if str(v).isdigit() else -1
            )
            return {"universal": str(highest)}
    return {}


def _parse_version_key(ver_str):
    """Parse version string sorting stable releases above pre-release tags."""
    cleaned = ver_str.strip().lstrip("vV")
    ver_match = VERSION_SPLIT_RE.match(cleaned)
    if ver_match:
        base_part = ver_match.group(1)
        suffix = ver_match.group(2).lower()
    else:
        parts = re.split(r"[-_]", cleaned, maxsplit=1)
        base_part = parts[0]
        suffix = parts[1].lower() if len(parts) > 1 else ""

    base_nums = tuple(int(n) for n in re.findall(r"\d+", base_part))
    pre_match = PRE_STAGE_RE.search(suffix) or PRE_STAGE_RE.search(
        base_part.lower()
    )
    if not pre_match and not suffix.strip("-_."):
        return (base_nums, 2, 0, (), ver_str)

    stage = (
        PRE_STAGE_RANKS.get(pre_match.group(1).lower(), 5)
        if pre_match
        else 5
    )
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
        return [
            {"packageName": k, "targets": []}
            for k in packages
            if isinstance(k, str)
        ]
    return []


def _process_package(pkg_data, pkg, patch_name, default_enabled):
    """Process a single compatible package entry."""
    pkg_name = pkg.get("packageName")
    if not pkg_name:
        return

    if pkg_name not in pkg_data:
        pkg_data[pkg_name] = {
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


def _sync_app_patches(app_cfg, compatible_patches, app_key, force_enable=False):
    """Sync patches in options_override to match upstream 1:1."""
    changed = False
    options = app_cfg.get("options_override")
    if not isinstance(options, dict):
        options = {}
        app_cfg["options_override"] = options

    for patch_name in list(options.keys()):
        if patch_name not in compatible_patches:
            print(
                f"    -> Removing obsolete patch '{patch_name}' "
                f"from '{app_key}'."
            )
            del options[patch_name]
            changed = True
        elif not force_enable and compatible_patches[patch_name] is True:
            local_opt = options[patch_name]
            if isinstance(local_opt, dict) and local_opt.get("enabled") is False:
                continue
            print(
                f"    -> Removing redundant patch '{patch_name}' "
                f"(now default: true upstream) from '{app_key}'."
            )
            del options[patch_name]
            changed = True

    for patch_name, default_val in sorted(compatible_patches.items()):
        if patch_name not in options:
            if force_enable:
                print(
                    f"    -> Adding new patch '{patch_name}' "
                    f"to '{app_key}' (enabled: true)."
                )
                options[patch_name] = {"enabled": True}
                changed = True
            elif default_val is False:
                print(
                    f"    -> Adding new patch '{patch_name}' "
                    f"to '{app_key}' (enabled: false)."
                )
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
            print(
                f"    -> Skipping downgrade for '{app_key}': "
                f"{old_version} -> {new_version}."
            )
            return changed

    if app_cfg.get("stable") != target_list:
        print(
            f"    -> Updating stable version of '{app_key}' "
            f"from {app_cfg.get('stable')} to {target_list}."
        )
        app_cfg["stable"] = target_list
        changed = True

    if new_vcodes and app_cfg.get("version_codes") != new_vcodes:
        print(f"    -> Updating version codes for '{app_key}'.")
        app_cfg["version_codes"] = new_vcodes
        changed = True

    apk_type = pkg_info.get("apk_file_type")
    if apk_type == "APKM" and not app_cfg.get("force_bundle"):
        print(f"    -> Setting force_bundle=True for '{app_key}'.")
        app_cfg["force_bundle"] = True
        changed = True

    return changed


def _process_existing_apps(app_configs, stable_pkgs, force_enable_patches):
    """Update existing apps while preserving apps missing from upstream."""
    changed = False

    for app_key, app_cfg in list(app_configs.items()):
        if not isinstance(app_cfg, dict):
            continue
        pkg_name = app_cfg.get("package")
        if not pkg_name:
            continue

        if pkg_name not in stable_pkgs:
            print(
                f"    -> WARNING: App '{app_key}' not found in patches-list. "
                "Keeping it to prevent data loss."
            )
            continue

        pkg_info = stable_pkgs[pkg_name]

        if _update_app_version(app_key, app_cfg, pkg_info):
            changed = True

        if _sync_app_patches(
            app_cfg,
            pkg_info["patches"],
            app_key,
            force_enable=force_enable_patches,
        ):
            changed = True

    return changed


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


def update_config(config_path, stable_data, force_enable_patches=False):
    """Update a single config file based on patches-list data."""
    if stable_data is None:
        print(f"  -> Skipping {config_path} due to missing data.")
        return False

    print(f"  -> Processing {config_path}...")

    try:
        with open(config_path, "r", encoding="utf-8") as file_obj:
            config = json.load(file_obj)
    except (OSError, json.JSONDecodeError) as err:
        print(f"    -> Failed to read {config_path}: {err}. Skipping.")
        return False

    root_key = os.path.splitext(os.path.basename(config_path))[0]
    if not isinstance(config, dict) or not isinstance(
        config.get(root_key), dict
    ):
        print(f"    -> Root key '{root_key}' not found or invalid. Skipping.")
        return False

    app_configs = config[root_key].get("apps")
    if not isinstance(app_configs, dict):
        app_configs = {}
        config[root_key]["apps"] = app_configs

    stable_pkgs = _extract_pkg_data(stable_data)

    changed = False
    if _process_existing_apps(
        app_configs, stable_pkgs, force_enable_patches
    ):
        changed = True

    if changed:
        if _write_config_atomic(config_path, config):
            print(f"  -> File {config_path} updated.")
        else:
            changed = False
    else:
        print(f"  -> No changes for {config_path}.")

    return changed


def _process_ecosystem(config_path, ecosystem_info):
    """Process a single ecosystem and return the fetched data."""
    if not os.path.exists(config_path):
        print(
            f"Skipping '{config_path}': "
            "Config file not found in the repository."
        )
        return None, False

    url = ecosystem_info["url"]
    release_patches = ecosystem_info["release_patches"]

    stable_data = fetch_patches_list(url, release_patches=release_patches)
    if stable_data is None:
        print(
            f"Skipping '{config_path}': "
            "Failed to fetch or parse upstream data (see error above)."
        )
        return None, False

    return stable_data, release_patches


def main():
    """Run the auto-update process for all configured ecosystems."""
    ecosystems = load_ecosystems()
    fetched_ecosystems = []

    for config_path, info in ecosystems.items():
        stable_data, force_enable = _process_ecosystem(config_path, info)
        if stable_data is not None:
            fetched_ecosystems.append(
                (config_path, stable_data, force_enable)
            )

    any_change = False
    for config_path, stable_data, force_enable in fetched_ecosystems:
        if update_config(
            config_path, stable_data, force_enable_patches=force_enable
        ):
            any_change = True

    if not any_change:
        print("No updates found across all ecosystems.")
        sys.exit(0)


if __name__ == "__main__":
    main()
