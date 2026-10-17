"""
Auto-update ecosystem configs from patches-list.json.
Run by GitHub Actions to create a Pull Request.
"""

import json
import os
import sys
import requests

# List of ecosystems to automate.
# Format: { "config_filename": { "stable": "url_main", "dev": "url_dev" } }
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
    # Add other ecosystems here
}

# Path to the ecosystem folder in your repo
ECOSYSTEM_DIR = "ecosystem"


def fetch_patches_list(url):
    """Fetch patches-list.json from a URL."""
    print(f"Fetching {url}...")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
        print(f"  -> Failed to fetch data: {e}")
        return None


def _extract_pkg_data(patches_data):
    """Extract package data and compatible patches from patches list."""
    pkg_data = {}
    for patch in patches_data.get("patches", []):
        for pkg in patch.get("compatiblePackages", []):
            pkg_name = pkg["packageName"]
            if pkg_name not in pkg_data:
                pkg_data[pkg_name] = {
                    "name": pkg.get("name", pkg_name),
                    "latest_target": None,
                    "patches": set(),
                }

            pkg_data[pkg_name]["patches"].add(patch["name"])

            targets = pkg.get("targets", [])
            if targets and not pkg_data[pkg_name]["latest_target"]:
                pkg_data[pkg_name]["latest_target"] = targets[0]

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

    # Remove obsolete patches
    for opt_name in list(options.keys()):
        if opt_name not in compatible_patches:
            print(f"    -> Removing obsolete patch '{opt_name}' from '{app_key}'.")
            del options[opt_name]
            changed = True

    # Add new patches
    for patch_name in compatible_patches:
        if patch_name not in options:
            print(f"    -> Adding new patch '{patch_name}' to '{app_key}' (false).")
            options[patch_name] = {"enabled": False}
            changed = True

    return changed


def _update_app_version(app_key, app_cfg, pkg_info, track):
    """Update version data (stable or beta) for an existing app config."""
    changed = False
    latest = pkg_info.get("latest_target")
    if not latest:
        return changed

    new_version = latest["version"]
    new_vcodes = {
        k.lower(): str(v) for k, v in latest.get("versionCodes", {}).items()
    }
    current_versions = app_cfg.get(track, [])

    if new_version not in current_versions:
        print(
            f"    -> Updating {track} version of '{app_key}' "
            f"from {current_versions} to {new_version}."
        )
        app_cfg[track] = [new_version]
        # Only update version_codes if we're updating the stable track,
        # or if the dev track has a newer version code.
        if track == "stable" or not app_cfg.get("version_codes"):
            app_cfg["version_codes"] = new_vcodes
        changed = True

    return changed


def _create_new_app(pkg_name, pkg_info, track):
    """Create a new app config entry based on branch track."""
    latest = pkg_info.get("latest_target")
    new_version = latest["version"] if latest else ""
    new_vcodes = (
        {k.lower(): str(v) for k, v in latest.get("versionCodes", {}).items()}
        if latest
        else {}
    )

    app_cfg = {
        "package": pkg_name,
        "search_term": pkg_info["name"],
        "stable": [],
        "beta": [],
        "version_codes": new_vcodes,
        "strip": True,
        "archive_id": "ZeNiX",
        "options_override": {},
    }

    if new_version:
        app_cfg[track] = [new_version]

    return app_cfg


def _process_existing_apps(app_configs, stable_pkgs, dev_pkgs):
    """Update existing apps, or remove them if no longer supported."""
    changed = False
    all_pkgs = set(stable_pkgs.keys()).union(dev_pkgs.keys())

    for app_key in list(app_configs.keys()):
        pkg_name = app_configs[app_key]["package"]
        if pkg_name not in all_pkgs:
            print(f"    -> Removing app '{app_key}' (not in patches-list).")
            del app_configs[app_key]
            changed = True
            continue

        app_cfg = app_configs[app_key]
        combined_patches = _get_pkg_patches(pkg_name, stable_pkgs, dev_pkgs)

        if pkg_name in stable_pkgs:
            if _update_app_version(
                app_key, app_cfg, stable_pkgs[pkg_name], "stable"
            ):
                changed = True

        if pkg_name in dev_pkgs:
            if _update_app_version(app_key, app_cfg, dev_pkgs[pkg_name], "dev"):
                changed = True

        if _sync_app_patches(app_cfg, combined_patches, app_key):
            changed = True

    return changed


def _process_new_apps(app_configs, stable_pkgs, dev_pkgs):
    """Add new apps that are present in stable or dev branches."""
    changed = False
    all_pkgs = set(stable_pkgs.keys()).union(dev_pkgs.keys())

    for pkg_name in all_pkgs:
        app_key = next(
            (k for k, v in app_configs.items() if v["package"] == pkg_name),
            None,
        )
        if app_key:
            continue

        print(f"    -> Adding new app '{pkg_name}'.")
        app_key = pkg_name.split(".")[-1]
        combined_patches = _get_pkg_patches(pkg_name, stable_pkgs, dev_pkgs)

        if pkg_name in stable_pkgs:
            app_cfg = _create_new_app(pkg_name, stable_pkgs[pkg_name], "stable")
        else:
            app_cfg = _create_new_app(pkg_name, dev_pkgs[pkg_name], "dev")

        app_configs[app_key] = app_cfg
        _sync_app_patches(app_cfg, combined_patches, app_key)
        changed = True

    return changed


def update_config(config_path, stable_data, dev_data):
    """Update a single config file based on patches-list data."""
    print(f"  -> Processing {config_path}...")

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    root_key = os.path.basename(config_path).replace(".json", "")
    if root_key not in config:
        print(f"    -> Root key '{root_key}' not found. Skipping.")
        return False

    app_configs = config[root_key].setdefault("apps", {})
    stable_pkgs = _extract_pkg_data(stable_data) if stable_data else {}
    dev_pkgs = _extract_pkg_data(dev_data) if dev_data else {}

    changed = False
    if _process_existing_apps(app_configs, stable_pkgs, dev_pkgs):
        changed = True

    if _process_new_apps(app_configs, stable_pkgs, dev_pkgs):
        changed = True

    if changed:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4, ensure_ascii=False)
        print(f"  -> File {config_path} updated.")
    else:
        print(f"  -> No changes for {config_path}.")

    return changed


def main():
    """Run the auto-update process for all configured ecosystems."""
    any_change = False
    for filename, urls in ECOSYSTEMS.items():
        config_path = os.path.join(ECOSYSTEM_DIR, filename)
        if not os.path.exists(config_path):
            print(f"Config file not found: {config_path}. Skipping.")
            continue

        stable_data = fetch_patches_list(urls["stable"])
        dev_data = fetch_patches_list(urls["dev"])

        if update_config(config_path, stable_data, dev_data):
            any_change = True

    if not any_change:
        print("No updates found across all ecosystems.")
        sys.exit(0)


if __name__ == "__main__":
    main()
