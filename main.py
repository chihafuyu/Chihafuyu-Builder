"""
Automated APK Downloader and Patcher using the CLI.
Modular architecture: Main Execution Entrypoint.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Dict, Any, Optional

from scrapers import AVAILABLE_SCRAPERS

from core.context import Context, RateLimiter
from core.utils import (
    _safe_filename,
    get_scraper,
    process_downloaded_file,
    update_options_json
)


def load_config() -> Dict[str, Dict[str, Any]]:
    """Loads the ecosystem configuration from the JSON file."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecosystems.json")
    if not os.path.isfile(config_path):
        sys.exit(f"[FATAL] '{config_path}' not found.")
    try:
        with open(config_path, "r", encoding="utf-8") as c_file:
            data = json.load(c_file)
    except (OSError, json.JSONDecodeError) as err:
        sys.exit(f"[FATAL] Failed to load config: {err}")
    if not isinstance(data, dict):
        sys.exit("[FATAL] ecosystems.json must contain a JSON object.")
    return data


ECOSYSTEMS: Dict[str, Dict[str, Any]] = load_config()


def download_apk(ctx: Context, args: Any) -> Optional[str]:
    """Fallback mechanism or targeted download for APK through multiple dynamic sources."""
    if ctx.target_ver.lower() == "any":
        print("[ERROR] Version defined as 'Any'. Skipping.")
        return None

    os.makedirs(os.path.join(ctx.out_dir, ctx.pkg), exist_ok=True)

    # 1. Try downloading from a specific source if prompted via a CLI argument
    req_source = args.download_source.lower()
    if req_source in AVAILABLE_SCRAPERS:
        scraper_instance = AVAILABLE_SCRAPERS[req_source]()
        path = scraper_instance.scrape(ctx)
        if path:
            return process_downloaded_file(path)

    # 2. Sequential fallback if a specific source fails or use the "default"
    fallback_order = [
        "direct", "github", "huggingface", "apkmirror",
        "apkpure", "apkcombo", "aptoide", "uptodown", "archive"
    ]

    for src_name in fallback_order:
        # Skip this if the source was already tried in step 1
        if src_name in AVAILABLE_SCRAPERS and src_name != req_source:
            scraper_instance = AVAILABLE_SCRAPERS[src_name]()
            path = scraper_instance.scrape(ctx)
            if path:
                return process_downloaded_file(path)

    print(f"[FATAL] Exhausted sources or specific source failed for {ctx.pkg}.")
    return None


def write_changelog(args: Any, apps_patched: list, workspace: str, clean_ver: str) -> None:
    """Write the patched apps changelog to a markdown file."""
    log_path = os.path.join(workspace, "changelog.md")
    with open(log_path, "w", encoding="utf-8") as f_obj:
        f_obj.write(f"## Automatically Patched Applications ({args.ecosystem})\n\n")
        if args.is_prerelease.lower() == "true":
            f_obj.write("> [!WARNING]\n")
            f_obj.write("> **Patched using pre-release tools. Use with caution.**\n\n")
        f_obj.write(f"Generated using **v{clean_ver}** from `{args.ecosystem}`.\n")
        f_obj.write(f"**Source:** [Repository]({args.repo_url})\n\n### Apps:\n")
        for app in apps_patched:
            b_str = f" (Build: {app['build']})" if app.get('build') else ""
            line = f"- **{app['name']}** (v{app['version']}{b_str} - `{app['arch']}`)\n"
            f_obj.write(line)
        f_obj.write("\n---\n### ⚠️ microG Required\n")
        f_obj.write(f"For Google Apps, install [microG-RE]({args.microg_url}).\n")


def _parse_custom_versions(ver_str: str) -> dict:
    if not ver_str:
        return {}
    if '=' in ver_str:
        return {p.split('=', 1)[0].strip(): p.split('=', 1)[1].strip() for p in ver_str.split(',')}
    return {"_global": ver_str.strip()}


def _get_patched_apk_path(app: str, ver: str, arch: str, args: Any, state: dict) -> str:
    s_app = _safe_filename(app)
    s_eco = _safe_filename(args.ecosystem)
    s_ver = _safe_filename(ver)
    s_arc = _safe_filename(arch)
    s_cln = _safe_filename(state['clean_ver'])
    f_name = f"{s_app}_{s_eco}_patched_{s_ver}-{s_arc}_patches_{s_cln}.apk"
    return os.path.join(state["out_dir"], f_name)


def build_patch_command(args: Any, app_data: dict, paths: tuple, target_arch: str) -> list:
    """Builds the shell command for the CLI, including exclusive patch handling."""
    cmd = [
        "java", "-Xmx4G", "-jar", args.cli, "patch", "--patches", args.patches,
        "--options-file", paths[1], "--out", paths[2], "--bytecode-mode", "FULL"
    ]
    if args.is_prerelease.lower() == "true" or args.version_selection.lower() in (
            "beta", "pre-release", "latest", "experimental", "custom"):
        cmd.append("--force")
    if app_data.get("strip"):
        cmd.extend(["--striplibs", target_arch])
    if args.continue_on_error.lower() == "true":
        cmd.append("--continue-on-error")
    if args.keystore and args.ks_alias and args.ks_pass:
        cmd.extend(["--keystore", args.keystore, "--keystore-entry-alias", args.ks_alias,
                    "--keystore-password", args.ks_pass, "--keystore-entry-password",
                    args.ks_pass])
        if args.signer:
            cmd.extend(["--signer", args.signer])
    if exc_list := app_data.get("exclusive_patches", []):
        print("[INFO] Exclusive mode detected. Generating targeted patch command...")
        cmd.append("--exclusive")
        for patch_name in exc_list:
            cmd.extend(["-e", patch_name])
    cmd.append(paths[0])
    return cmd


def execute_patch_cli(patch_cmd: list) -> tuple:
    """Executes the patch command and streams output."""
    zero_patches = False
    try:
        with subprocess.Popen(patch_cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) as proc:
            if proc.stdout:
                for line in proc.stdout:
                    print(line, end='')
                    if "Applying 0 patches" in line:
                        zero_patches = True
            proc.wait()
            return proc.returncode, zero_patches
    except OSError as err:
        print(f"[ERROR] Failed to execute patch CLI: {err}")
        return 127, zero_patches


def _generate_options_json(app_name: str, args: Any, app_data: dict, workspace: str) -> str:
    json_file = os.path.join(workspace, f"{_safe_filename(app_name)}-options.json")
    cmd = ["java", "-jar", args.cli, "options-create", "--patches", args.patches,
           "--out", json_file, "--filter-package-name", app_data["package"]]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        err_out = (res.stderr or res.stdout or '').strip()[-500:]
        print(f"[WARN] CLI options-create failed (exit {res.returncode}): {err_out}")
    exc_list = app_data.get("exclusive_patches", [])
    if exc_list or app_data.get("options_override"):
        if exc_list:
            print(f"[INFO] Forcing strict exclusive options mapping for {app_name}...")
        update_options_json(
            json_file, app_data.get("options_override", {}), exclusive_patches=exc_list
        )
    return json_file


def process_single_app(
    app_name: str, args: Any, app_data: dict, custom_ver: str, state: dict
) -> None:
    """Processes a single app for downloading and patching."""
    t_ver = custom_ver if custom_ver else app_data.get("stable", [""])[0]
    if args.version_selection.lower() in ["beta", "pre-release", "latest", "experimental"]:
        t_ver = app_data.get("beta", [t_ver])[0]

    arch = app_data.get("force_arch", args.arch)
    print(f"\n--- {app_name} ({app_data['package']}) ---")

    ctx = Context(get_scraper(), app_data, t_ver, arch, state["in_dir"], RateLimiter(delay=3.0))
    if not (apk_path := download_apk(ctx, args)):
        return

    json_file = _generate_options_json(app_name, args, app_data, state["workspace"])
    out_apk = _get_patched_apk_path(app_name, t_ver, arch, args, state)

    print("[INFO] Patching via CLI...")
    cmd = build_patch_command(args, app_data, (apk_path, json_file, out_apk), arch)
    ret_code, zero_patches = execute_patch_cli(cmd)

    if ret_code == 0 and not zero_patches:
        print(f"\n[INFO] SUCCESS: {app_name}")
        state["success"].append({
            "name": app_name, "version": t_ver,
            "build": app_data.get("version_codes", {}).get(arch), "arch": arch
        })
    else:
        msg = 'DMCA Trap' if zero_patches else f'Exit code {ret_code}'
        print(f"\n[ERROR] FAILED: {app_name}. Reason: {msg}")
        if os.path.exists(out_apk):
            os.remove(out_apk)
    time.sleep(5)


def run_patcher(args: Any) -> None:
    """Main execution function to handle the patching loop."""
    if args.ecosystem not in ECOSYSTEMS:
        sys.exit(f"[FATAL] Ecosystem '{args.ecosystem}' not found in JSON.")

    workspace = f"./{_safe_filename(args.ecosystem)}"
    state = {
        "in_dir": f"{workspace}/Input", "out_dir": f"{workspace}/Output", "workspace": workspace,
        "clean_ver": args.patches_version.lstrip('v') if args.patches_version else "unknown",
        "success": []
    }
    os.makedirs(state["in_dir"], exist_ok=True)
    os.makedirs(state["out_dir"], exist_ok=True)

    print(f"=== INITIALIZING WORKSPACE: {args.ecosystem.upper()} ===")
    eco_apps = ECOSYSTEMS[args.ecosystem].get("apps")
    if not isinstance(eco_apps, dict):
        sys.exit(f"[FATAL] '{args.ecosystem}' has no valid 'apps' config.")

    app_list = list(eco_apps.keys()) if args.apps.lower() == "all" else args.apps.split(',')
    custom_vers = _parse_custom_versions(args.custom_version)

    for app_name in [a.strip() for a in app_list]:
        if app_name in eco_apps:
            c_ver = custom_vers.get(app_name) or custom_vers.get("_global", "")
            process_single_app(app_name, args, eco_apps[app_name], c_ver, state)

    if state["success"]:
        write_changelog(args, state["success"], workspace, state["clean_ver"])


def parse_arguments() -> Any:
    """Parses command line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--ecosystem", required=True)
    parser.add_argument("--apps", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--version-selection", required=True)
    parser.add_argument("--custom-version", default="")
    parser.add_argument("--download-source", default="default")
    parser.add_argument("--continue-on-error", choices=("true", "false"), default="false")
    parser.add_argument("--hf-user", default="chihafuyu")
    parser.add_argument("--microg-url",
                        default="https://github.com/MorpheApp/MicroG-RE/releases/latest")
    parser.add_argument("--cli", required=True)
    parser.add_argument("--patches", required=True)
    parser.add_argument("--patches-version", required=True)
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--keystore")
    parser.add_argument("--ks-alias")
    parser.add_argument("--ks-pass")
    parser.add_argument("--signer")
    parser.add_argument("--is-prerelease", default="false")
    return parser.parse_args()


if __name__ == "__main__":
    run_patcher(parse_arguments())
