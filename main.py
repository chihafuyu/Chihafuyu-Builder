"""
Automated APK Downloader and Patcher using the CLI.
Modular architecture: Main Execution Entrypoint.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from typing import Any

from scrapers import AVAILABLE_SCRAPERS
from core.context import Context, RateLimiter
from core.utils import (
    SUBPROCESS_TIMEOUT_SECONDS,
    _safe_filename,
    _sandboxed_env,
    get_scraper,
    process_downloaded_file,
    run_java_tool,
    update_options_json,
)

# 5.5h budget so the job stops before GitHub Actions 6h hard-kill.
MAX_RUNTIME_SECONDS = 19800
ECOSYSTEM_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def load_config(ecosystem_name: str) -> dict[str, Any]:
    """Loads ecosystem configuration from the specific JSON file."""
    if not ECOSYSTEM_NAME_RE.fullmatch(ecosystem_name):
        sys.exit(f"[FATAL] Invalid ecosystem name: {ecosystem_name!r}")
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "ecosystem",
        f"{ecosystem_name}.json",
    )
    if not os.path.isfile(config_path):
        sys.exit(f"[FATAL] '{config_path}' not found.")
    try:
        with open(config_path, "r", encoding="utf-8") as c_file:
            data = json.load(c_file)
    except (OSError, json.JSONDecodeError) as err:
        sys.exit(f"[FATAL] Failed to load config: {err}")
    if not isinstance(data, dict):
        sys.exit(f"[FATAL] {ecosystem_name}.json must contain a JSON object.")
    if ecosystem_name not in data:
        sys.exit(f"[FATAL] Root key '{ecosystem_name}' missing in {config_path}.")
    return data[ecosystem_name]


def download_apk(ctx: Context, args: Any) -> str | None:
    """Downloads target APK via fallback sources or primary scraper."""
    if not ctx.target_ver or ctx.target_ver.lower() == "any":
        print(f"[ERROR] No concrete version defined ({ctx.target_ver!r}). Skipping.")
        return None

    os.makedirs(ctx.pkg_dir, exist_ok=True)

    req_source = args.download_source.lower()
    if req_source in AVAILABLE_SCRAPERS:
        with AVAILABLE_SCRAPERS[req_source]() as scraper_instance:
            path = scraper_instance.scrape(ctx)
            if path:
                return process_downloaded_file(path)
        print(f"[INFO] Primary source '{req_source}' returned nothing; falling back.")

    fallback_order = [
        "direct", "github", "huggingface", "apkmirror",
        "archive", "apkpure", "google_play",
    ]

    for src_name in fallback_order:
        if src_name in AVAILABLE_SCRAPERS and src_name != req_source:
            with AVAILABLE_SCRAPERS[src_name]() as scraper_instance:
                path = scraper_instance.scrape(ctx)
                if path:
                    return process_downloaded_file(path)

    print(f"[FATAL] Exhausted sources or specific source failed for {ctx.pkg}.")
    return None


def _normalize_repo_url(repo: str) -> str:
    """Normalizes a repo identifier into a fully-qualified GitHub URL.

    Accepts any of:
      - "owner/repo"
      - "github.com/owner/repo"
      - "https://github.com/owner/repo"

    Trailing slashes are stripped. Returns an empty string when input is empty.
    """
    repo = (repo or "").strip().rstrip("/")
    if not repo:
        return ""
    if repo.startswith(("https://", "http://")):
        return repo
    if repo.startswith("github.com/"):
        return f"https://{repo}"
    return f"https://github.com/{repo}"


def write_changelog(
    args: Any, apps_patched: list, workspace: str, clean_ver: str
) -> None:
    """Writes the patched apps changelog to a markdown file."""
    log_path = os.path.join(workspace, "changelog.md")
    cli_version = args.cli_version if args.cli_version else "Unknown"
    repo_url = _normalize_repo_url(args.repo_url)

    with open(log_path, "w", encoding="utf-8") as f_obj:
        f_obj.write(f"## Automatically Patched Applications ({args.ecosystem})\n\n")
        if args.is_prerelease.lower() == "true":
            f_obj.write("> [!WARNING]\n")
            f_obj.write("> **Patched using pre-release tools. Use with caution.**\n\n")

        f_obj.write("Generated using:\n")
        if clean_ver and clean_ver != "unknown":
            f_obj.write(
                f"- Patches version **v{clean_ver}** from `{args.ecosystem}`. "
            )
        else:
            f_obj.write(f"- Patches from `{args.ecosystem}`. ")
        f_obj.write(f"Source: [Repository]({repo_url})\n")
        f_obj.write(
            f"- CLI version **v{cli_version.lstrip('v')}** from `morphe-desktop`. "
        )
        f_obj.write(
            "Source: [Repository](https://github.com/MorpheApp/morphe-desktop)\n\n"
        )

        f_obj.write("### Apps:\n")
        for app in apps_patched:
            b_str = f" (Build: {app['build']})" if app.get("build") else ""
            line = (
                f"- **{app['name']}** "
                f"(v{app['version']}{b_str} - `{app['arch']}`)\n"
            )
            f_obj.write(line)
        f_obj.write("\n---\n### ⚠️ microG Required\n")
        f_obj.write(
            f"For Google Apps, install [microG-RE]({args.microg_url}).\n"
        )


def _parse_custom_versions(ver_str: str) -> dict:
    """Parses custom version arguments into a dictionary."""
    if not ver_str:
        return {}
    if "=" not in ver_str:
        return {"_global": ver_str.strip()}
    versions = {}
    for pair in ver_str.split(","):
        key, sep, val = (part.strip() for part in pair.partition("="))
        if sep and key and val:
            versions[key] = val
        elif pair.strip():
            print(
                f"[WARN] Ignoring malformed custom version entry: {pair.strip()!r}"
            )
    return versions


def _get_apk_output_path(
    app_info: tuple[str, str, str],
    args: Any,
    state: dict,
) -> str:
    """Constructs the release path for the patched APK."""
    app, ver, arch = app_info
    stem = (
        f"{_safe_filename(app)}_{_safe_filename(args.ecosystem)}_"
        f"{_safe_filename(ver)}-{_safe_filename(arch)}_"
        f"patches_{_safe_filename(state['clean_ver'])}"
    )
    return os.path.join(state["out_dir"], f"{stem}_patched.apk")


def _get_staging_path(app_name: str, state: dict, ext: str) -> str:
    """Staging path for per-app log/options before ecosystem aggregation."""
    safe = _safe_filename(app_name)
    return os.path.join(state["staging_dir"], f"{safe}.{ext}")


def build_patch_command(
    args: Any, app_data: dict, paths: tuple, target_arch: str
) -> list[str]:
    """Builds the CLI shell command including exclusive patch parameters.

    Note: keystore credentials are passed as CLI flags because the upstream
    CLI does not expose an environment-based alternative. This means they
    are briefly visible via /proc on shared runners. Do not log this list.
    """
    cmd = [
        "java", "-Xmx4G", "-jar", args.cli, "patch",
        "--patches", args.patches,
        "--options-file", paths[1],
        "--out", paths[2],
        "--bytecode-mode", "FULL",
    ]
    if args.is_prerelease.lower() == "true" or args.version_selection.lower() in (
        "beta", "pre-release", "latest", "experimental", "custom",
    ):
        cmd.append("--force")

    strip_flag = app_data.get("strip")
    if strip_flag and target_arch.lower() not in ("universal", "noarch"):
        cmd.extend(["--striplibs", target_arch])

    if args.continue_on_error.lower() == "true":
        cmd.append("--continue-on-error")

    if args.keystore and args.ks_alias and args.ks_pass:
        cmd.extend([
            "--keystore", args.keystore,
            "--keystore-entry-alias", args.ks_alias,
            "--keystore-password", args.ks_pass,
            "--keystore-entry-password", args.ks_pass,
        ])
        if args.signer:
            cmd.extend(["--signer", args.signer])

    exc_list = app_data.get("exclusive_patches", [])
    if exc_list:
        print("[INFO] Exclusive mode detected. Generating targeted patch command...")
        cmd.append("--exclusive")
        for patch_name in exc_list:
            cmd.extend(["-e", patch_name])

    cmd.append(paths[0])
    return cmd


def execute_patch_cli(
    patch_cmd: list[str], log_path: str | None = None
) -> tuple[int, bool]:
    """Executes the patch CLI under a credential-free env with a hard timeout.

    When log_path is provided, the combined stdout/stderr stream is written
    to that file so it can be aggregated and exported alongside the release.
    """
    zero_patches = False

    with ExitStack() as stack:
        log_handle = None
        if log_path:
            try:
                log_handle = stack.enter_context(
                    open(log_path, "w", encoding="utf-8")
                )
            except OSError as err:
                print(f"[WARN] Could not open patch log {log_path}: {err}")

        try:
            with subprocess.Popen(
                patch_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=_sandboxed_env(),
            ) as proc:

                def _stream_output() -> None:
                    nonlocal zero_patches
                    if proc.stdout is None:
                        return
                    for line in proc.stdout:
                        print(line, end="")
                        if log_handle is not None:
                            log_handle.write(line)
                        if "Applying 0 patches" in line:
                            zero_patches = True

                reader = threading.Thread(target=_stream_output, daemon=True)
                reader.start()

                try:
                    proc.wait(timeout=SUBPROCESS_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    print(
                        f"[ERROR] Patch CLI timed out after "
                        f"{SUBPROCESS_TIMEOUT_SECONDS}s. Killing."
                    )
                    proc.kill()
                    proc.wait()
                    reader.join(timeout=5)
                    return 124, zero_patches

                reader.join(timeout=5)
                return proc.returncode, zero_patches
        except OSError as err:
            print(f"[ERROR] Failed to execute patch CLI: {err}")
            return 127, zero_patches


def _generate_options_json(
    app_name: str, args: Any, app_data: dict, workspace: str
) -> str | None:
    """Generates options.json required for patching.

    Returns the file path on success, or None when options-create failed
    to produce a usable file. Callers must handle the None case.
    """
    json_file = os.path.join(workspace, f"{_safe_filename(app_name)}-options.json")
    cmd = [
        "java", "-jar", args.cli, "options-create",
        "--patches", args.patches,
        "--out", json_file,
        "--filter-package-name", app_data["package"],
    ]
    res = run_java_tool(cmd, tag=f"options-create ({app_name})")

    if res is not None and res.returncode != 0:
        err_out = (res.stderr or res.stdout or "").strip()[-500:]
        print(f"[WARN] CLI options-create failed (exit {res.returncode}): {err_out}")

    if not os.path.isfile(json_file):
        print(f"[ERROR] options.json was not produced at {json_file}.")
        return None

    exc_list = app_data.get("exclusive_patches", [])
    if exc_list or app_data.get("options_override"):
        if exc_list:
            print(f"[INFO] Forcing strict exclusive options mapping for {app_name}...")
        update_options_json(
            json_file,
            app_data.get("options_override", {}),
            exclusive_patches=exc_list,
        )
    return json_file


def _resolve_target_version(app_data: dict, args: Any, custom_ver: str) -> str:
    """Resolves target version safely avoiding index errors from empty fallbacks."""
    t_ver = custom_ver if custom_ver else (app_data.get("stable") or [""])[0]

    if args.version_selection.lower() in (
        "beta", "pre-release", "latest", "experimental",
    ):
        beta_list = app_data.get("beta") or []
        if beta_list:
            t_ver = beta_list[0]
        else:
            stable_list = app_data.get("stable") or [t_ver]
            t_ver = stable_list[0] if stable_list else t_ver

    return t_ver


def _export_options_snapshot(json_file: str, out_opts: str) -> bool:
    """Copies the generated options.json to the staging folder.

    Returns True on success. Failure is non-fatal but logged.
    """
    try:
        shutil.copy2(json_file, out_opts)
        print(f"[INFO] Staged options snapshot: {os.path.basename(out_opts)}")
        return True
    except OSError as err:
        print(f"[WARN] Could not stage options.json: {err}")
        return False


def _prepare_patch_inputs(
    app_info: tuple[str, str, str],
    args: Any,
    app_data: dict,
    state: dict,
) -> dict | None:
    """Downloads APK, generates options, and returns a bundle of paths.

    The returned dict has keys: apk, options, out_apk.
    Returns None if download or options generation failed.
    """
    app_name, t_ver, arch = app_info

    ctx = Context(
        get_scraper(),
        app_data,
        t_ver,
        arch,
        state["in_dir"],
        RateLimiter(delay=3.0),
    )
    apk_path = download_apk(ctx, args)
    if not apk_path:
        return None

    json_file = _generate_options_json(app_name, args, app_data, state["workspace"])
    if not json_file:
        return None

    return {
        "apk": apk_path,
        "options": json_file,
        "out_apk": _get_apk_output_path(app_info, args, state),
    }


def process_single_app(
    app_name: str, args: Any, app_data: dict, custom_ver: str, state: dict
) -> None:
    """Processes a single application for downloading and patching safely."""
    t_ver = _resolve_target_version(app_data, args, custom_ver)
    arch = app_data.get("force_arch", args.arch)
    print(f"\n--- {app_name} ({app_data['package']}) ---")

    app_info = (app_name, t_ver, arch)
    bundle = _prepare_patch_inputs(app_info, args, app_data, state)
    if bundle is None:
        return

    # Stage the exact options used for this patch, even if patching fails.
    _export_options_snapshot(
        bundle["options"], _get_staging_path(app_name, state, "options")
    )

    print("[INFO] Patching via CLI...")
    cmd = build_patch_command(
        args,
        app_data,
        (bundle["apk"], bundle["options"], bundle["out_apk"]),
        arch,
    )
    ret_code, zero_patches = execute_patch_cli(
        cmd, log_path=_get_staging_path(app_name, state, "log")
    )

    success = ret_code == 0 and not zero_patches
    status = (
        "success" if success
        else "DMCA Trap" if zero_patches
        else f"Exit code {ret_code}"
    )

    if success:
        print(f"\n[INFO] SUCCESS: {app_name}")
        state["success"].append({
            "name": app_name,
            "version": t_ver,
            "build": app_data.get("version_codes", {}).get(arch),
            "arch": arch,
        })
    else:
        print(f"\n[ERROR] FAILED: {app_name}. Reason: {status}")
        if os.path.exists(bundle["out_apk"]):
            os.remove(bundle["out_apk"])

    state["attempted"].append({
        "name": app_name,
        "package": app_data["package"],
        "version": t_ver,
        "arch": arch,
        "status": status,
    })
    time.sleep(5)


def _build_log_block(entry: dict, state: dict) -> str:
    """Builds a single delimited log block for one app."""
    tmp_log = _get_staging_path(entry["name"], state, "log")
    header = (
        "=" * 60 + "\n"
        f"App: {entry['name']} ({entry['package']})\n"
        f"Version: {entry['version']} | Arch: {entry['arch']}\n"
        f"Status: {entry['status']}\n"
        + "=" * 60 + "\n\n"
    )
    if os.path.isfile(tmp_log):
        with open(tmp_log, "r", encoding="utf-8") as f_obj:
            body = f_obj.read()
    else:
        body = "(no log captured)\n"
    return header + body


def _read_staged_options(app_name: str, state: dict) -> Any:
    """Reads a staged options.json for one app, or returns an error marker."""
    tmp_opts = _get_staging_path(app_name, state, "options")
    if not os.path.isfile(tmp_opts):
        return {"_error": "no options captured"}
    try:
        with open(tmp_opts, "r", encoding="utf-8") as f_obj:
            return json.load(f_obj)
    except (OSError, json.JSONDecodeError) as err:
        return {"_error": str(err)}


def _aggregate_exports(args: Any, state: dict) -> None:
    """Merges per-app logs and options into single ecosystem-wide files.

    Produces:
      {out_dir}/{ecosystem}_log.txt
      {out_dir}/{ecosystem}_options.json
    """
    if not state["attempted"]:
        return

    eco = _safe_filename(args.ecosystem)
    out_dir = state["out_dir"]

    log_blocks = [_build_log_block(e, state) for e in state["attempted"]]
    agg_log = os.path.join(out_dir, f"{eco}_log.txt")
    with open(agg_log, "w", encoding="utf-8") as f_obj:
        f_obj.write("\n".join(log_blocks))

    merged = {
        e["name"]: _read_staged_options(e["name"], state)
        for e in state["attempted"]
    }
    agg_opts = os.path.join(out_dir, f"{eco}_options.json")
    with open(agg_opts, "w", encoding="utf-8") as f_obj:
        json.dump(merged, f_obj, indent=2, ensure_ascii=False)

    print(f"[INFO] Aggregated exports: {os.path.basename(agg_log)}")
    print(f"[INFO] Aggregated exports: {os.path.basename(agg_opts)}")


def _process_app_loop(args: Any, state: dict) -> None:
    """Iterates over the app list, dispatching each to process_single_app."""
    for app_name in state["app_list"]:
        if time.monotonic() - state["start_time"] > MAX_RUNTIME_SECONDS:
            print("\n[WARN] ⏱️ Maximum runtime limit (5.5 hours) reached!")
            print("[WARN] Stopping gracefully to preserve generated artifacts...")
            break

        if app_name not in state["eco_apps"]:
            continue

        c_ver = (
            state["custom_vers"].get(app_name)
            or state["custom_vers"].get("_global", "")
        )
        process_single_app(app_name, args, state["eco_apps"][app_name], c_ver, state)


def run_patcher(args: Any) -> None:
    """Main execution function handling the patching loop with graceful timeout."""
    workspace = f"./{_safe_filename(args.ecosystem)}"
    in_dir = f"{workspace}/Input"
    out_dir = f"{workspace}/Output"
    staging_dir = f"{workspace}/Staging"
    clean_ver = (
        args.patches_version.lstrip("v") if args.patches_version else "unknown"
    )
    for d in (in_dir, out_dir, staging_dir):
        os.makedirs(d, exist_ok=True)

    print(f"=== INITIALIZING WORKSPACE: {args.ecosystem.upper()} ===")
    eco_config = load_config(args.ecosystem)
    eco_apps = eco_config.get("apps")
    if not isinstance(eco_apps, dict):
        sys.exit(f"[FATAL] '{args.ecosystem}' has no valid 'apps' config.")

    app_list = (
        list(eco_apps.keys())
        if args.apps.lower() == "all"
        else [a.strip() for a in args.apps.split(",") if a.strip()]
    )
    state = {
        "in_dir": in_dir,
        "out_dir": out_dir,
        "staging_dir": staging_dir,
        "workspace": workspace,
        "clean_ver": clean_ver,
        "success": [],
        "attempted": [],
        "eco_apps": eco_apps,
        "app_list": app_list,
        "custom_vers": _parse_custom_versions(args.custom_version),
        "start_time": time.monotonic(),
    }

    _process_app_loop(args, state)

    _aggregate_exports(args, state)

    if state["success"]:
        write_changelog(args, state["success"], workspace, clean_ver)


def parse_arguments() -> Any:
    """Parses command line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--ecosystem", required=True)
    parser.add_argument("--apps", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--version-selection", required=True)
    parser.add_argument("--custom-version", default="")
    parser.add_argument("--download-source", default="default")
    parser.add_argument(
        "--continue-on-error", choices=("true", "false"), default="false"
    )
    parser.add_argument("--hf-user", default="chihafuyu")
    parser.add_argument(
        "--microg-url",
        default="https://github.com/MorpheApp/MicroG-RE/releases/latest",
    )
    parser.add_argument("--cli", required=True)
    parser.add_argument("--cli-version", default="")
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
