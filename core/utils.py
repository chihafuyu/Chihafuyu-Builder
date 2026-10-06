"""
Core utility functions.
Handles network streaming, file extraction, WAF detection, hash checking, and option injections.
"""

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from typing import Any
from urllib.parse import urlparse

import requests

MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024
# Upper bound for external tooling (apkeep / options-create) so a stuck process can't hang the job.
SUBPROCESS_TIMEOUT_SECONDS = 900
# APKs are executable code: only accept transports that guarantee integrity in transit.
ALLOWED_DOWNLOAD_SCHEMES = frozenset({"https"})
# VirusTotal engines that must agree before rejecting an APK.
DEFAULT_VT_MALICIOUS_THRESHOLD = 3
HA_THREAT_THRESHOLD = 50
# MetaDefender "scan_all_result_i": 1 = Infected/Known, 8 = Skipped Infected.
MD_INFECTED_CODES = frozenset({1, 8})
# MetaDefender engines that must agree before rejecting an APK.
# Loose default (2) filters lone false positives on legit Google APKs.
DEFAULT_MD_INFECTED_THRESHOLD = 2


def get_scraper() -> requests.Session:
    """Initializes and returns a configured requests session instance."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        )
    })
    return session


def _safe_filename(name: str, fallback: str = "artifact") -> str:
    """Sanitizes string into a safe file path segment."""
    cleaned = os.path.basename(str(name)).strip()
    if not cleaned or cleaned in {".", ".."} or any(c in cleaned for c in ('/', '\\', '\x00')):
        return fallback
    return cleaned


def _env_int(name: str, default: int) -> int:
    """Reads an integer from the environment, falling back on missing or malformed values."""
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _validate_download_url(url: str) -> None:
    """Raises ValueError if the URL is not an absolute HTTPS URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_DOWNLOAD_SCHEMES or not parsed.hostname:
        raise ValueError(f"Unsupported download URL: {url!r}")


def _is_waf_blocked(status_code: int, text: str) -> bool:
    """Evaluates if the response was intercepted by a Web Application Firewall."""
    if status_code in (429, 503):
        return True
    challenges = (
        "just a moment", "cf-challenge", "challenge-platform", "attention required",
        "checking your browser", "ddos-guard", "aptcha.execute", "enable javascript and cookies"
    )
    return any(c in text.lower() for c in challenges)


def _check_virustotal(file_hash: str) -> bool | None:
    """Checks hash against VirusTotal.

    Returns True if clean, False if flagged as malicious, None if not analyzed.
    """
    vt_key = os.environ.get("VT_API_KEY", "")
    if not vt_key:
        return None
    url = f"https://www.virustotal.com/api/v3/files/{file_hash}"
    try:
        resp = requests.get(url, headers={"x-apikey": vt_key}, timeout=10)
        if resp.status_code == 200:
            stats = resp.json().get("data", {}).get(
                "attributes", {}
            ).get("last_analysis_stats", {})
            mal = int(stats.get("malicious") or 0)
            sus = int(stats.get("suspicious") or 0)
            threshold = max(1, _env_int("VT_MALICIOUS_THRESHOLD", DEFAULT_VT_MALICIOUS_THRESHOLD))
            if mal >= threshold:
                print(f"[ERROR] VT Flagged: Malicious={mal}, Suspicious={sus}")
                return False
            if mal or sus:
                print(f"[WARN] VT low-confidence hits: Malicious={mal}, Suspicious={sus}")
            else:
                print("[INFO] VirusTotal verification passed: File is clean.")
            return True
        print(f"[INFO] VT bypass (Code {resp.status_code}).")
    except (requests.exceptions.RequestException, ValueError, TypeError, AttributeError) as err:
        print(f"[WARN] VT request failed: {err}")
    return None


def _check_hybrid_analysis(file_hash: str) -> bool | None:
    """Checks hash against Hybrid Analysis.

    Returns True if clean, False if flagged as malicious, None if not analyzed.
    """
    ha_key = os.environ.get("HA_API_KEY", "")
    if not ha_key:
        return None
    url = "https://www.hybrid-analysis.com/api/v2/search/hash"
    headers = {"api-key": ha_key, "User-Agent": "Chihafuyu-Builder"}
    try:
        resp = requests.post(url, headers=headers, data={"hash": file_hash}, timeout=10)
        reports = resp.json() if resp.status_code == 200 else None
        if isinstance(reports, list) and reports and isinstance(reports[0], dict):
            threat_score = int(reports[0].get("threat_score") or 0)
            if threat_score > HA_THREAT_THRESHOLD:
                print(f"[ERROR] HA Flagged: Threat Score {threat_score}/100")
                return False
            print(f"[INFO] HA verification passed (Score {threat_score}/100).")
            return True
        print(f"[INFO] HA bypass (Code {resp.status_code}).")
    except (requests.exceptions.RequestException, ValueError, TypeError) as err:
        print(f"[WARN] HA request failed: {err}")
    return None


def _check_metadefender(file_hash: str) -> bool | None:
    """Checks hash against MetaDefender Cloud.

    Uses an engine-count threshold so a lone false positive on legitimate
    APKs (common for Google apps) does not trigger a rejection.

    Returns True if clean, False if flagged as malicious, None if not analyzed.
    """
    md_key = os.environ.get("MD_API_KEY", "")
    if not md_key:
        return None
    url = f"https://api.metadefender.com/v4/hash/{file_hash}"
    try:
        resp = requests.get(url, headers={"apikey": md_key}, timeout=10)
        if resp.status_code == 200:
            scan_res = resp.json().get("scan_results", {})
            verdict = int(scan_res.get("scan_all_result_i") or 0)

            details = scan_res.get("scan_details", {})
            infected_engines = sum(
                1 for engine in details.values()
                if isinstance(engine, dict) and engine.get("threat_found")
            )
            threshold = max(
                1, _env_int("MD_INFECTED_THRESHOLD", DEFAULT_MD_INFECTED_THRESHOLD)
            )

            if verdict in MD_INFECTED_CODES and infected_engines >= threshold:
                print(
                    f"[ERROR] MetaDefender Flagged: "
                    f"verdict={verdict}, engines={infected_engines} (>= {threshold})"
                )
                return False
            if verdict in MD_INFECTED_CODES:
                print(
                    f"[WARN] MetaDefender low-confidence: "
                    f"verdict={verdict}, engines={infected_engines} (< {threshold})"
                )
            elif verdict:
                print(
                    f"[WARN] MetaDefender verdict code {verdict} "
                    f"(not a confirmed infection)."
                )
            else:
                print("[INFO] MetaDefender verification passed: File is clean.")
            return True
        print(f"[INFO] MD bypass (Code {resp.status_code}).")
    except (requests.exceptions.RequestException, ValueError, TypeError, AttributeError) as err:
        print(f"[WARN] MD request failed: {err}")
    return None


def verify_file_hash(file_path: str) -> bool:
    """Computes SHA256 hash and cascades through VT, HA, and MD sequentially.

    Returns False only when a scanner positively flags the file as malicious.
    """
    try:
        with open(file_path, "rb") as f_obj:
            file_hash = hashlib.file_digest(f_obj, "sha256").hexdigest()
    except OSError as err:
        print(f"[WARN] Hash verification skipped due to error: {err}")
        return True

    print(f"[INFO] SHA-256: {file_hash}")
    for checker in (_check_virustotal, _check_hybrid_analysis, _check_metadefender):
        verdict = checker(file_hash)
        if verdict is not None:
            return verdict

    print("[INFO] All hash verifications bypassed or unconfigured.")
    return True


def _is_acceptable_response(resp: Any, check_dmca: bool) -> bool:
    """Validates status, DMCA trap headers, and advertised size before streaming."""
    if resp.status_code != 200:
        return False
    disp = resp.headers.get('Content-Disposition', '').lower()
    if check_dmca and 'uptodown-app-store' in disp:
        print("[ERROR] DMCA Trap detected (Store APK).")
        return False
    size = resp.headers.get("Content-Length")
    if size and size.isdigit() and int(size) > MAX_DOWNLOAD_BYTES:
        print("[ERROR] File exceeds size limit.")
        return False
    return True


def _stream_to_file(resp: Any, file_obj: Any) -> None:
    """Streams the response body into file_obj while enforcing the size limit."""
    total = 0
    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
        total += len(chunk)
        if total > MAX_DOWNLOAD_BYTES:
            raise ValueError("download exceeds limit")
        file_obj.write(chunk)


def download_file_stream(
    scraper: Any, url: str, out_path: str, referer: str = "", check_dmca: bool = False
) -> bool:
    """Downloads a file safely with streaming, size limits, and atomic replacement."""
    out_dir = os.path.dirname(os.path.abspath(out_path))
    try:
        _validate_download_url(url)
        headers = {"Referer": referer} if referer else None
        with scraper.get(
            url, stream=True, headers=headers, timeout=(10, 60), allow_redirects=True
        ) as resp:
            # Reject redirects that downgrade the transport (e.g. HTTPS -> HTTP).
            _validate_download_url(str(resp.url))
            if not _is_acceptable_response(resp, check_dmca):
                return False

            os.makedirs(out_dir, exist_ok=True)
            fd_num, t_path = tempfile.mkstemp(prefix=".dl-", dir=out_dir)
            try:
                with os.fdopen(fd_num, 'wb') as apk_file:
                    _stream_to_file(resp, apk_file)
                # Only publish the file once it has passed the malware cascade.
                if not verify_file_hash(t_path):
                    print("[ERROR] Download rejected by malware scanners.")
                    return False
                os.replace(t_path, out_path)
            finally:
                if os.path.exists(t_path):
                    os.remove(t_path)
            return True
    except (requests.exceptions.RequestException, OSError, ValueError) as err:
        print(f"[ERROR] Request failed: {err}")
    return False


def _extract_xapk(
    file_path: str, zip_obj: zipfile.ZipFile, namelist: list[str]
) -> str | None:
    """Extracts base APK from XAPK wrapper formats into a temporary sibling file.

    Returns the temporary path, or None if the archive is not a single-APK wrapper.
    """
    apk_files = [item for item in namelist if item.lower().endswith(".apk")]
    if len(apk_files) != 1 or file_path.lower().endswith(".apkm"):
        return None

    print("[INFO] XAPK Wrapper detected. Extracting APK...")
    member = zip_obj.getinfo(apk_files[0])
    if member.file_size > MAX_DOWNLOAD_BYTES:
        raise ValueError("embedded APK exceeds limit")

    # Never write over the archive that is still being read (same-name .apk wrappers).
    out_dir = os.path.dirname(os.path.abspath(file_path))
    fd_num, t_path = tempfile.mkstemp(prefix=".xapk-", suffix=".apk", dir=out_dir)
    try:
        # fdopen first so fd_num is always consumed even if zip_obj.open fails.
        with os.fdopen(fd_num, "wb") as target:
            with zip_obj.open(member) as source:
                shutil.copyfileobj(source, target, length=CHUNK_SIZE)
    except Exception:
        os.remove(t_path)
        raise
    return t_path


def process_downloaded_file(file_path: str) -> str | None:
    """Processes downloaded files, handling pure APKs and wrappers."""
    try:
        if not zipfile.is_zipfile(file_path):
            print("[ERROR] Invalid ZIP/APK container.")
            return None
        with zipfile.ZipFile(file_path, 'r') as zip_obj:
            namelist = zip_obj.namelist()
            is_plain_apk = 'AndroidManifest.xml' in namelist and 'classes.dex' in namelist
            extracted = None if is_plain_apk else _extract_xapk(file_path, zip_obj, namelist)

        if not is_plain_apk and not extracted:
            # Split bundles (.apkm/.apks) are consumed natively by the CLI.
            return file_path

        new_path = os.path.splitext(file_path)[0] + '.apk'
        os.replace(extracted or file_path, new_path)
        if extracted and new_path != file_path:
            os.remove(file_path)
        return new_path
    except (zipfile.BadZipFile, OSError, ValueError) as err:
        print(f"[WARN] Inspection failed: {err}")
    return None


def _update_patch_options(target_dict: dict, override_data: dict) -> None:
    if "enabled" in override_data:
        target_dict["enabled"] = override_data["enabled"]
    if "options" in override_data:
        if "options" not in target_dict or not isinstance(target_dict["options"], dict):
            target_dict["options"] = {}
        for key, val in override_data["options"].items():
            target_dict["options"][key] = val


def _search_and_update(obj: Any, patch_name: str, override_data: dict) -> bool:
    found = False
    if isinstance(obj, dict):
        if patch_name in obj and isinstance(obj[patch_name], dict):
            _update_patch_options(obj[patch_name], override_data)
            found = True
        else:
            for val in obj.values():
                if _search_and_update(val, patch_name, override_data):
                    found = True
                    break
    elif isinstance(obj, list):
        for item in obj:
            if _search_and_update(item, patch_name, override_data):
                found = True
                break
    return found


def update_options_json(
    filepath: str, overrides: dict, exclusive_patches: list | None = None
) -> None:
    """Injects custom options and handles exclusive patch restrictions into the JSON file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as opt_file:
            data = json.load(opt_file)

        if exclusive_patches:
            print("[INFO] Enforcing exclusive patch states inside options.json...")

            def _apply_exclusivity(node: Any) -> None:
                if isinstance(node, dict):
                    for k, v in node.items():
                        if isinstance(v, dict) and "enabled" in v:
                            v["enabled"] = k in exclusive_patches
                        _apply_exclusivity(v)
                elif isinstance(node, list):
                    for item in node:
                        _apply_exclusivity(item)

            _apply_exclusivity(data)

        for patch_name, override_data in overrides.items():
            if not _search_and_update(data, patch_name, override_data):
                print(f"[WARN] Patch '{patch_name}' not found in JSON!")

        temp_path = f"{filepath}.tmp"
        try:
            with open(temp_path, 'w', encoding='utf-8') as opt_file:
                json.dump(data, opt_file, indent=4)
                opt_file.write("\n")
            os.replace(temp_path, filepath)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        print("[INFO] Options injected successfully.")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as err:
        print(f"[WARN] Options injection failed: {err}")
