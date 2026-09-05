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
import cloudscraper

MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


def get_scraper() -> Any:
    """Initializes and returns a cloudscraper instance."""
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )
    scraper.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0"
    })
    return scraper


def _safe_filename(name: str, fallback: str = "artifact") -> str:
    """Sanitizes string into a safe file path segment."""
    cleaned = os.path.basename(str(name)).strip()
    if not cleaned or cleaned in {".", ".."} or any(c in cleaned for c in ('/', '\\', '\x00')):
        return fallback
    return cleaned


def _validate_http_url(url: str) -> None:
    """Raises ValueError if URL schema is unsupported."""
    if urlparse(url).scheme not in {"http", "https"}:
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


def _check_virustotal(file_hash: str) -> bool:
    """Checks hash against VirusTotal. Returns True if successfully analyzed."""
    vt_key = os.environ.get("VT_API_KEY", "")
    if not vt_key:
        return False
    url = f"https://www.virustotal.com/api/v3/files/{file_hash}"
    try:
        resp = requests.get(url, headers={"x-apikey": vt_key}, timeout=10)
        if resp.status_code == 200:
            stats = resp.json().get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
            mal = stats.get("malicious", 0)
            sus = stats.get("suspicious", 0)
            if mal > 0 or sus > 0:
                print(f"[WARN] VT Flagged: Malicious={mal}, Suspicious={sus}")
            else:
                print("[INFO] VirusTotal verification passed: File is clean.")
            return True
        print(f"[INFO] VT bypass (Code {resp.status_code}).")
    except requests.exceptions.RequestException as err:
        print(f"[WARN] VT request failed: {err}")
    return False


def _check_hybrid_analysis(file_hash: str) -> bool:
    """Checks hash against Hybrid Analysis. Returns True if successfully analyzed."""
    ha_key = os.environ.get("HA_API_KEY", "")
    if not ha_key:
        return False
    url = "https://www.hybrid-analysis.com/api/v2/search/hash"
    headers = {
        "api-key": ha_key,
        "User-Agent": "Chihafuyu-Builder",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    try:
        resp = requests.post(url, headers=headers, data=f"hash={file_hash}", timeout=10)
        if resp.status_code == 200 and resp.json():
            threat_score = resp.json()[0].get("threat_score", 0)
            if threat_score > 50:
                print(f"[WARN] HA Flagged: Threat Score {threat_score}/100")
            else:
                print(f"[INFO] HA verification passed (Score {threat_score}/100).")
            return True
        print(f"[INFO] HA bypass (Code {resp.status_code}).")
    except requests.exceptions.RequestException as err:
        print(f"[WARN] HA request failed: {err}")
    return False


def _check_metadefender(file_hash: str) -> bool:
    """Checks hash against MetaDefender Cloud. Returns True if successfully analyzed."""
    md_key = os.environ.get("MD_API_KEY", "")
    if not md_key:
        return False
    url = f"https://api.metadefender.com/v4/hash/{file_hash}"
    try:
        resp = requests.get(url, headers={"apikey": md_key}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            scan_res = data.get("scan_results", {})
            threats = scan_res.get("scan_all_result_i", 0)
            if threats > 0:
                print(f"[WARN] MetaDefender Flagged: {threats} threats found.")
            else:
                print("[INFO] MetaDefender verification passed: File is clean.")
            return True
        print(f"[INFO] MD bypass (Code {resp.status_code}).")
    except requests.exceptions.RequestException as err:
        print(f"[WARN] MD request failed: {err}")
    return False


def verify_file_hash(file_path: str) -> None:
    """Computes SHA256 hash and cascades through VT, HA, and MD sequentially."""
    try:
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f_obj:
            for byte_block in iter(lambda: f_obj.read(4194304), b""):
                sha256_hash.update(byte_block)
        file_hash = sha256_hash.hexdigest()

        if _check_virustotal(file_hash):
            return
        if _check_hybrid_analysis(file_hash):
            return
        if _check_metadefender(file_hash):
            return

        print("[INFO] All hash verifications bypassed or unconfigured.")
    except (OSError, ValueError, IndexError) as err:
        print(f"[WARN] Hash verification skipped due to error: {err}")


def download_file_stream(scraper: Any, url: str, out_path: str,
                         referer: str = "", check_dmca: bool = False) -> bool:
    """Downloads a file safely with streaming, size limits, and atomic replacement."""
    try:
        _validate_http_url(url)
        headers = {"Referer": referer} if referer else None
        with scraper.get(url, stream=True, headers=headers, timeout=(10, 60),
                         allow_redirects=True) as resp:
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

            os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
            fd_num, t_path = tempfile.mkstemp(prefix=".dl-", dir=os.path.dirname(out_path))
            os.close(fd_num)
            total = 0
            try:
                with open(t_path, 'wb') as apk_file:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError("download exceeds limit")
                        apk_file.write(chunk)
                os.replace(t_path, out_path)
                verify_file_hash(out_path)
            finally:
                if os.path.exists(t_path):
                    os.remove(t_path)
            return True
    except (requests.exceptions.RequestException, OSError, ValueError) as err:
        print(f"[ERROR] Request failed: {err}")
    return False


def _extract_xapk(file_path: str, zip_obj: zipfile.ZipFile, namelist: list) -> str:
    """Extracts base APK from XAPK wrapper formats."""
    apk_files = [item for item in namelist if item.lower().endswith('.apk')]
    if len(apk_files) == 1 and not file_path.lower().endswith('.apkm'):
        print("[INFO] XAPK Wrapper detected. Extracting APK...")
        member = zip_obj.getinfo(apk_files[0])
        if member.file_size > MAX_DOWNLOAD_BYTES:
            raise ValueError("embedded APK exceeds limit")
        new_path = file_path.rsplit('.', 1)[0] + '.apk'
        with zip_obj.open(member) as source, open(new_path, 'wb') as target:
            shutil.copyfileobj(source, target, length=CHUNK_SIZE)
        os.remove(file_path)
        return new_path
    return file_path


def process_downloaded_file(file_path: str) -> str | None:
    """Processes downloaded files, handling pure APKs and wrappers."""
    try:
        if not zipfile.is_zipfile(file_path):
            print("[ERROR] Invalid ZIP/APK container.")
            return None
        with zipfile.ZipFile(file_path, 'r') as zip_obj:
            namelist = zip_obj.namelist()
            if 'AndroidManifest.xml' in namelist and 'classes.dex' in namelist:
                if not file_path.endswith('.apk'):
                    new_path = file_path.rsplit('.', 1)[0] + '.apk'
                    os.replace(file_path, new_path)
                    return new_path
                return file_path
            return _extract_xapk(file_path, zip_obj, namelist)
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


def update_options_json(filepath: str, overrides: dict, exclusive_patches: list = None) -> None:
    """Injects custom options and handles exclusive patch restrictions into the JSON file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as opt_file:
            data = json.load(opt_file)

        if exclusive_patches:
            print("[INFO] Enforcing exclusive patch states inside options.json...")
            def _apply_exclusivity(node: Any):
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
