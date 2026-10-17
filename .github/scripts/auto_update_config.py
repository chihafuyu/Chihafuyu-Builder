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


def fetch_patches_list(url):
    """Fetch patches-list.json from a URL."""
    print(f"Fetching {url}...")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.exceptions.RequestException, json.JSONDecodeError) as err:
        print(f"  -> Failed to fetch data: {err}")
        return None


def _get_target_sort_key(target):
    """Return sort key prioritizing highest version code and numeric version."""
    vcodes = target.get("versionCodes") or {}
    codes = [int(v) for v in vcodes.values() if str(v).isdigit()]
    highest_code = max(codes) if codes else -1
    ver_str = str(target.get("version") or "")
    ver_nums = tuple(int(part) for part in re.findall(r"\d+", ver_str))
    return (highest_code, ver_nums, ver_str)


def _extract_pkg_data(patches_data):
    """Extract package data and compatible patches from patches list."""
    pkg_data = {}
    for patch in patches_data.get("patches", []):
        for pkg in patch.get("compatiblePackages", []):
            pkg_name = pkg.get("packageName")
            if not pkg_name:
                continue

            if pkg_name not in pkg_data:
                pkg_data[pkg_name] = {
                    "name": pkg.get("name", pkg_name),
                    "all_targets": [],
                    "patches": set(),
                }

            pkg_data[pkg_name]["patches"].add(patch["name"])

            for target in pkg.get("targets", []):
                pkg_data[pkg_name]["all_targets"].append(target)

    for data in pkg_data.values():
        targets = data["all_targets"]
        if not targets:
            data["latest_target"] = None
            continue

        targets.sort(key=_get_target_sort_key, reverse=True)
        data["latest_target"] = targets[0]
        del data["all_targets"]

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
    options = app_cfg.setdefault("options_override", {})

    for patch_name in compatible_patches:
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


def _update_app_version(app_key, app_cfg, pkg_info, track_name):
    """Update version data (stable or beta) for an existing app config."""
    changed = False
    latest = pkg_info.get("latest_target")
    if not latest:
        return changed

    new_version = latest["version"]
    new_vcodes = {
        k.lower(): str(v) for k, v in (latest.get("versionCodes") or {}).items()
    }
    current_versions = app_cfg.get(track_name, [])

    if new_version not in current_versions:
        print(
            f"    -> Updating {track_name} version of '{app_key}' "
            f"from {current_versions} to {new_version}."
        )
        app_cfg[track_name] = [new_version]
        changed = True

    if track_name == "stable" or not app_cfg.get("version_codes"):
        if app_cfg.get("version_codes") != new_vcodes:
            print(f"    -> Updating version codes for '{app_key}'.")
            app_cfg["version_codes"] = new_vcodes
            changed = True

    return changed


def _process_existing_apps(app_configs, stable_pkgs, dev_pkgs):
    """Update existing apps while preserving apps missing from upstream."""
    changed = False
    all_pkgs = set(stable_pkgs.keys()).union(dev_pkgs.keys())

    for app_key in list(app_configs.keys()):
        pkg_name = app_configs[app_key].get("package")
        if not pkg_name:
            continue

        if pkg_name not in all_pkgs:
            print(
                f"    -> WARNING: App '{app_key}' not found in patches-list. "
                "Keeping it to prevent data loss."
            )
            continue

        app_cfg = app_configs[app_key]
        combined_patches = _get_pkg_patches(pkg_name, stable_pkgs, dev_pkgs)

        if pkg_name in stable_pkgs:
            if _update_app_version(
                app_key, app_cfg, stable_pkgs[pkg_name], "stable"
            ):
                changed = True

        if pkg_name in dev_pkgs:
            if _update_app_version(
                app_key, app_cfg, dev_pkgs[pkg_name], "beta"
            ):
                changed = True

        if _sync_app_patches(app_cfg, combined_patches, app_key):
            changed = True

    return changed


def _process_new_apps(app_configs, stable_pkgs, dev_pkgs):
    """Add new apps that are present in stable or dev branches."""
    changed = False
    all_pkgs = set(stable_pkgs.keys()).union(dev_pkgs.keys())
    default_archive_id = next(
        (
            cfg.get("archive_id")
            for cfg in app_configs.values()
            if cfg.get("archive_id")
        ),
        "ZeNiX",
    )

    for pkg_name in all_pkgs:
        app_key = next(
            (k for k, v in app_configs.items() if v.get("package") == pkg_name),
            None,
        )
        if app_key:
            continue

        print(f"    -> Adding new app '{pkg_name}'.")

        base_key = pkg_name.split(".")[-1]
        app_key = base_key
        counter = 1
        while (
            app_key in app_configs
            and app_configs[app_key].get("package") != pkg_name
        ):
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
                app_key, app_cfg, stable_pkgs[pkg_name], "stable"
            )
        if pkg_name in dev_pkgs:
            _update_app_version(app_key, app_cfg, dev_pkgs[pkg_name], "beta")

        app_configs[app_key] = app_cfg
        _sync_app_patches(app_cfg, combined_patches, app_key)
        changed = True

    return changed


def update_config(config_path, stable_data, dev_data):
    """Update a single config file based on patches-list data."""
    if stable_data is None or dev_data is None:
        print(f"  -> Skipping {config_path} due to missing data from network.")
        return False

    print(f"  -> Processing {config_path}...")

    with open(config_path, "r", encoding="utf-8") as file_obj:
        config = json.load(file_obj)

    root_key = os.path.basename(config_path).replace(".json", "")
    if root_key not in config:
        print(f"    -> Root key '{root_key}' not found. Skipping.")
        return False

    app_configs = config[root_key].setdefault("apps", {})
    stable_pkgs = _extract_pkg_data(stable_data)
    dev_pkgs = _extract_pkg_data(dev_data)

    changed = False
    if _process_existing_apps(app_configs, stable_pkgs, dev_pkgs):
        changed = True

    if _process_new_apps(app_configs, stable_pkgs, dev_pkgs):
        changed = True

    if changed:
        with open(config_path, "w", encoding="utf-8") as file_obj:
            json.dump(config, file_obj, indent=4, ensure_ascii=False)
            file_obj.write("\n")
        print(f"  -> File {config_path} updated.")
    else:
        print(f"  -> No changes for {config_path}.")

    return changed


def main():
    """Run the auto-update process for all configured ecosystems."""
    any_change = False
    fetch_failed = False

    for filename, urls in ECOSYSTEMS.items():
        config_path = os.path.join(ECOSYSTEM_DIR, filename)
        if not os.path.exists(config_path):
            print(f"Config file not found: {config_path}. Skipping.")
            continue

        stable_data = fetch_patches_list(urls["stable"])
        dev_data = fetch_patches_list(urls["dev"])

        if stable_data is None or dev_data is None:
            fetch_failed = True
            continue

        if update_config(config_path, stable_data, dev_data):
            any_change = True

    if fetch_failed:
        print("One or more network fetches failed.")
        sys.exit(1)

    if not any_change:
        print("No updates found across all ecosystems.")
        sys.exit(0)


if __name__ == "__main__":
    main()
