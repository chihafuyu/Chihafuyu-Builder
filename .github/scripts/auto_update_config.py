"""
Auto-update ecosystem configs from patches-list.json.
Run by GitHub Actions to create a Pull Request.
"""

import json
import os
import re
import sys

import requests

ECOSYSTEMS = {
    "morphe.json": {
        "stable": (
            "https://raw.githubusercontent.com/MorpheApp/"
            "morphe-patches/main/patches-list.json"
        ),
        "dev": (
            "https://raw.githubusercontent.com/MorpheApp/"
            "morphe-patches/dev/patches-list.json"
        ),
    },
    "kveld9.json": {
        "stable": (
            "https://raw.githubusercontent.com/kveld9/"
            "kveld-morphe-patches/main/patches-list.json"
        ),
        "dev": (
            "https://raw.githubusercontent.com/kveld9/"
            "kveld-morphe-patches/dev/patches-list.json"
        ),
    },
    "legendsciber.json": {
        "stable": (
            "https://raw.githubusercontent.com/legendsciber/"
            "morphe-patches/main/patches-list.json"
        ),
        "dev": (
            "https://raw.githubusercontent.com/legendsciber/"
            "morphe-patches/dev/patches-list.json"
        ),
    },
    "jordanwoodson.json": {
        "stable": (
            "https://raw.githubusercontent.com/jordanwoodson/"
            "lurk-morphe-patches/main/patches-list.json"
        ),
        "dev": (
            "https://raw.githubusercontent.com/jordanwoodson/"
            "lurk-morphe-patches/dev/patches-list.json"
        ),
    },
}

ECOSYSTEM_DIR = "ecosystem"
REQUEST_HEADERS = {
    "User-Agent": "Chihafuyu-Builder/1.0 (+https://github.com/chihafuyu)"
}
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


def fetch_patches_list(url):
    """Fetch patches-list.json from a URL."""
    print(f"Fetching {url}...")
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            print("  -> Invalid JSON format: expected an object.")
            return None
        return data
    except (requests.exceptions.RequestException, json.JSONDecodeError) as err:
        print(f"  -> Failed to fetch data: {err}")
        return None


def _normalize_vcodes(raw_vcodes):
    """Normalize versionCodes from a dict or list into a string dictionary."""
    if isinstance(raw_vcodes, dict):
        return {
            str(k).lower(): str(v)
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


def _extract_pkg_data(patches_data):
    """Extract package data and compatible patches from patches list."""
    pkg_data = {}
    if not patches_data:
        return pkg_data

    for patch in patches_data.get("patches") or []:
        if not isinstance(patch, dict):
            continue
        patch_name = patch.get("name")
        for pkg in patch.get("compatiblePackages") or []:
            if not isinstance(pkg, dict):
                continue
            pkg_name = pkg.get("packageName")
            if not pkg_name:
                continue

            if pkg_name not in pkg_data:
                pkg_data[pkg_name] = {
                    "name": pkg.get("name") or pkg_name,
                    "all_targets": [],
                    "patches": set(),
                }

            if patch_name:
                pkg_data[pkg_name]["patches"].add(patch_name)

            for target in pkg.get("targets") or []:
                if (
                    isinstance(target, dict)
                    and target not in pkg_data[pkg_name]["all_targets"]
                ):
                    pkg_data[pkg_name]["all_targets"].append(target)

    for data in pkg_data.values():
        targets = data.pop("all_targets", [])
        if not targets:
            data["latest_target"] = None
            continue

        targets.sort(key=_get_target_sort_key, reverse=True)
        data["latest_target"] = targets[0]

    return pkg_data


def _get_pkg_patches(pkg_name, stable_pkgs, dev_pkgs):
    """Get all compatible patches from both stable and dev branches."""
    patches = set()
    if pkg_name in stable_pkgs:
        patches.update(stable_pkgs[pkg_name]["patches"])
    if pkg_name in dev_pkgs:
        patches.update(dev_pkgs[pkg_name]["patches"])
    return patches


def _sync_app_patches(app_cfg, compatible_patches, app_key):
    """Sync patches in options_override, adding missing ones as false."""
    changed = False
    options = app_cfg.get("options_override")
    if not isinstance(options, dict):
        options = {}
        app_cfg["options_override"] = options

    for patch_name in sorted(compatible_patches):
        if patch_name not in options:
            print(
                f"    -> Adding new patch '{patch_name}' "
                f"to '{app_key}' (false)."
            )
            options[patch_name] = {"enabled": False}
            changed = True

    for opt_name in options:
        if opt_name not in compatible_patches:
            print(
                f"    -> WARNING: Patch '{opt_name}' on '{app_key}' "
                "is not in upstream. Keeping it."
            )

    return changed


def _update_app_version(
    app_key, app_cfg, pkg_info, track_name, update_vcodes=False
):
    """Update single-item version list (stable or beta) for an app config."""
    changed = False
    latest = pkg_info.get("latest_target")
    if not latest:
        return changed

    new_version = latest.get("version")
    new_vcodes = _normalize_vcodes(latest.get("versionCodes"))
    current_val = app_cfg.get(track_name)
    target_list = [str(new_version)] if new_version else []

    if target_list and current_val != target_list:
        print(
            f"    -> Updating {track_name} version of '{app_key}' "
            f"from {current_val} to {target_list}."
        )
        app_cfg[track_name] = target_list
        changed = True

    if new_vcodes and (update_vcodes or not app_cfg.get("version_codes")):
        if app_cfg.get("version_codes") != new_vcodes:
            print(f"    -> Updating version codes for '{app_key}'.")
            app_cfg["version_codes"] = new_vcodes
            changed = True

    return changed


def _process_existing_apps(app_configs, stable_pkgs, dev_pkgs):
    """Update existing apps while preserving apps missing from upstream."""
    changed = False
    all_pkgs = set(stable_pkgs.keys()).union(dev_pkgs.keys())

    for app_key, app_cfg in list(app_configs.items()):
        if not isinstance(app_cfg, dict):
            continue
        pkg_name = app_cfg.get("package")
        if not pkg_name:
            continue

        if pkg_name not in all_pkgs:
            print(
                f"    -> WARNING: App '{app_key}' not found in patches-list. "
                "Keeping it to prevent data loss."
            )
            continue

        combined_patches = _get_pkg_patches(pkg_name, stable_pkgs, dev_pkgs)

        if pkg_name in stable_pkgs:
            if _update_app_version(
                app_key,
                app_cfg,
                stable_pkgs[pkg_name],
                "stable",
                update_vcodes=True,
            ):
                changed = True

        if pkg_name in dev_pkgs:
            if _update_app_version(
                app_key,
                app_cfg,
                dev_pkgs[pkg_name],
                "beta",
                update_vcodes=pkg_name not in stable_pkgs,
            ):
                changed = True

        if _sync_app_patches(app_cfg, combined_patches, app_key):
            changed = True

    return changed


def _process_new_apps(app_configs, stable_pkgs, dev_pkgs):
    """Add new apps that are present in stable or dev branches."""
    changed = False
    all_pkgs = sorted(set(stable_pkgs.keys()).union(dev_pkgs.keys()))
    default_archive_id = next(
        (
            cfg.get("archive_id")
            for cfg in app_configs.values()
            if isinstance(cfg, dict) and cfg.get("archive_id")
        ),
        "ZeNiX",
    )

    for pkg_name in all_pkgs:
        app_key = next(
            (
                k
                for k, v in app_configs.items()
                if isinstance(v, dict) and v.get("package") == pkg_name
            ),
            None,
        )
        if app_key:
            continue

        print(f"    -> Adding new app '{pkg_name}'.")

        base_key = pkg_name.split(".")[-1].lower()
        app_key = base_key
        counter = 1
        while app_key in app_configs:
            app_key = f"{base_key}_{counter}"
            counter += 1

        combined_patches = _get_pkg_patches(pkg_name, stable_pkgs, dev_pkgs)
        pkg_info = stable_pkgs.get(pkg_name) or dev_pkgs.get(pkg_name)

        app_cfg = {
            "package": pkg_name,
            "search_term": pkg_info["name"],
            "stable": [],
            "beta": [],
            "version_codes": {},
            "strip": True,
            "archive_id": default_archive_id,
            "options_override": {},
        }

        if pkg_name in stable_pkgs:
            _update_app_version(
                app_key,
                app_cfg,
                stable_pkgs[pkg_name],
                "stable",
                update_vcodes=True,
            )
        if pkg_name in dev_pkgs:
            _update_app_version(
                app_key,
                app_cfg,
                dev_pkgs[pkg_name],
                "beta",
                update_vcodes=pkg_name not in stable_pkgs,
            )

        _sync_app_patches(app_cfg, combined_patches, app_key)
        app_configs[app_key] = app_cfg
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


def update_config(config_path, stable_data, dev_data):
    """Update a single config file based on patches-list data."""
    if stable_data is None:
        print(f"  -> Skipping {config_path} due to missing stable data.")
        return False

    if dev_data is None:
        print(f"  -> No dev data for {config_path}, assuming empty.")
        dev_data = {}

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
    dev_pkgs = _extract_pkg_data(dev_data)

    changed = False
    if _process_existing_apps(app_configs, stable_pkgs, dev_pkgs):
        changed = True

    if _process_new_apps(app_configs, stable_pkgs, dev_pkgs):
        changed = True

    if changed:
        if _write_config_atomic(config_path, config):
            print(f"  -> File {config_path} updated.")
        else:
            changed = False
    else:
        print(f"  -> No changes for {config_path}.")

    return changed


def main():
    """Run the auto-update process for all configured ecosystems."""
    fetched_ecosystems = []

    for filename, urls in ECOSYSTEMS.items():
        config_path = os.path.join(ECOSYSTEM_DIR, filename)
        if not os.path.exists(config_path):
            print(f"Config file not found: {config_path}. Skipping.")
            continue

        # Fetch stable (mandatory)
        stable_data = fetch_patches_list(urls["stable"])
        if stable_data is None:
            print(
                f"Failed to fetch stable data for {filename}. "
                "Aborting to prevent data loss."
            )
            sys.exit(1)

        # Fetch dev (optional)
        dev_data = fetch_patches_list(urls["dev"])
        if dev_data is None:
            print(f"No dev branch found for {filename}. Proceeding without it.")

        fetched_ecosystems.append((config_path, stable_data, dev_data))

    any_change = False
    for config_path, stable_data, dev_data in fetched_ecosystems:
        if update_config(config_path, stable_data, dev_data):
            any_change = True

    if not any_change:
        print("No updates found across all ecosystems.")
        sys.exit(0)


if __name__ == "__main__":
    main()
