"""
Automated APK Downloader and Patcher using the CLI.
Handles multi-tier downloading, dynamic options.json injection,
exclusive patch execution, and WAF/Captcha evasions.
"""

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from typing import Dict, Any, Tuple
from urllib.parse import urljoin, urlparse, quote_plus

try:
    import requests
    import cloudscraper
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("[FATAL] Missing libs. Run: pip install cloudscraper beautifulsoup4 requests")

MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


class RateLimiter:
    """Ensures a minimum delay between requests to avoid rate limits."""

    def __init__(self, delay: float):
        self.delay = delay
        self.last_req = 0.0

    def wait(self) -> None:
        """Waits if the time since the last request is less than the delay."""
        now = time.monotonic()
        if now - self.last_req < self.delay:
            time.sleep(self.delay - (now - self.last_req))
        self.last_req = time.monotonic()

    def reset(self) -> None:
        """Resets the internal timer."""
        self.last_req = 0.0


def _safe_filename(name: str, fallback: str = "artifact") -> str:
    """Returns a filesystem-safe single path component."""
    cleaned = os.path.basename(str(name)).strip()
    if not cleaned or cleaned in {".", ".."} or any(c in cleaned for c in ('/', '\\', '\x00')):
        return fallback
    return cleaned


@dataclass
class Context:
    """Holds common variables for the scraping process."""
    scraper: Any
    app_data: dict
    target_ver: str
    arch: str
    out_dir: str
    limiter: RateLimiter

    @property
    def pkg(self) -> str:
        """Returns the package name."""
        return self.app_data["package"]

    def get_out_path(self, ext: str) -> str:
        """Returns the safe output path for the downloaded file."""
        pkg_str = _safe_filename(self.pkg)
        ver_str = _safe_filename(self.target_ver)
        return os.path.join(self.out_dir, f"{pkg_str}_{ver_str}{ext}")


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


def get_scraper() -> Any:
    """Initializes and returns a cloudscraper instance."""
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )
    scraper.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0.0.0"
        )
    })
    return scraper


def _validate_http_url(url: str) -> None:
    """Accept only HTTP(S) URLs before handing them to requests."""
    if urlparse(url).scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported download URL: {url!r}")


def _is_waf_blocked(status_code: int, text: str) -> bool:
    """Checks if the HTTP response is blocked by a WAF or Captcha."""
    if status_code in (429, 503):
        return True
    challenges = (
        "just a moment", "cf-challenge", "challenge-platform", "attention required",
        "checking your browser", "ddos-guard", "aptcha.execute",
        "enable javascript and cookies"
    )
    return any(c in text.lower() for c in challenges)


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
            finally:
                if os.path.exists(t_path):
                    os.remove(t_path)
            return True
    except (requests.exceptions.RequestException, OSError, ValueError) as err:
        print(f"[ERROR] Request failed: {err}")
    return False


def _extract_xapk(file_path: str, zip_obj: zipfile.ZipFile, namelist: list) -> str:
    """Extracts a single APK from an XAPK/APKM wrapper."""
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
    """Updates the enabled status and options for a specific patch."""
    if "enabled" in override_data:
        target_dict["enabled"] = override_data["enabled"]
    if "options" in override_data:
        if "options" not in target_dict or not isinstance(target_dict["options"], dict):
            target_dict["options"] = {}
        for key, val in override_data["options"].items():
            target_dict["options"][key] = val


def _search_and_update(obj: Any, patch_name: str, override_data: dict) -> bool:
    """Recursively updates the first matching patch and reports whether it was found."""
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


def update_options_json(filepath: str, overrides: dict) -> None:
    """Injects custom options into the JSON file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as opt_file:
            data = json.load(opt_file)
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


# --- TIER 0: HUGGINGFACE ---
def scrape_huggingface(ctx: Context, hf_user: str) -> str | None:
    """Scrape the APK directly from HuggingFace Datasets."""
    hf_repo = ctx.app_data.get("hf_repo", f"{hf_user}/{ctx.app_data.get('archive_id')}")
    if not ctx.app_data.get("archive_id") and not ctx.app_data.get("hf_repo"):
        return None
    print(f"[TIER 0] HuggingFace: v{ctx.target_ver}")
    ctx.limiter.wait()
    base_url = f"https://huggingface.co/datasets/{hf_repo}/resolve/main"

    for ext in ['.apk', '.xapk', '.apkm', '.apks']:
        dl_link = f"{base_url}/{ctx.pkg}_{ctx.target_ver}{ext}"
        out_path = ctx.get_out_path(ext)
        try:
            if ctx.scraper.head(dl_link, timeout=10, allow_redirects=True).status_code == 200:
                print("[INFO] Downloading from Vault...")
                if download_file_stream(ctx.scraper, dl_link, out_path):
                    return out_path
        except requests.exceptions.RequestException:
            continue
    print(f"[WARN] Not found in '{hf_repo}'.")
    return None


# --- TIER 1: APKMIRROR ---
def _find_apkmirror_release(ctx: Context) -> str | None:
    """Finds the release page URL on APKMirror."""
    raw_ver = ctx.target_ver
    base_ver = raw_ver.split('-')[0] if '-' in raw_ver and raw_ver[0].isdigit() else raw_ver
    query = quote_plus(f"{ctx.app_data.get('search_term', ctx.pkg)} {base_ver}")
    url = f"https://www.apkmirror.com/?post_type=app_release&s={query}"

    ctx.limiter.wait()
    resp = ctx.scraper.get(url, timeout=60)
    if _is_waf_blocked(resp.status_code, resp.text) or resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')
    exc_kws = ["secondary"] + [k.lower() for k in ctx.app_data.get("apkm_exclude", [])]
    inc_kws = [k.lower() for k in ctx.app_data.get("apkm_include", [])]

    for link in soup.find_all('a', class_='fontBlack'):
        text = link.text.lower()
        if base_ver.lower() not in text or any(kw in text for kw in exc_kws):
            continue
        if inc_kws and not all(kw in text for kw in inc_kws):
            continue
        return urljoin("https://www.apkmirror.com", link['href'])
    return None


def _process_apkmirror_variant_page(ctx: Context, var_url: str, is_bundle: bool) -> str | None:
    """Processes the specific variant download page and retrieves the file."""
    ctx.limiter.wait()
    v_resp = ctx.scraper.get(var_url, timeout=60)
    if _is_waf_blocked(v_resp.status_code, v_resp.text) or v_resp.status_code != 200:
        return None

    btn = BeautifulSoup(v_resp.text, 'html.parser').find('a', class_='downloadButton')
    if not btn:
        return None

    dl_page = urljoin("https://www.apkmirror.com", btn['href'])
    ctx.limiter.wait()
    d_resp = ctx.scraper.get(dl_page, timeout=60)
    if _is_waf_blocked(d_resp.status_code, d_resp.text) or d_resp.status_code != 200:
        return None

    dl_btn = BeautifulSoup(d_resp.text, 'html.parser').find("a", {"rel": "nofollow"})
    if dl_btn and 'href' in dl_btn.attrs:
        out_path = ctx.get_out_path('.apkm' if is_bundle else '.apk')
        dl_url = urljoin("https://www.apkmirror.com", dl_btn['href'])
        print("[INFO] Downloading from APKMirror...")
        if download_file_stream(ctx.scraper, dl_url, out_path, dl_page):
            return out_path
    return None


def _extract_apkm_row(ctx: Context, row: Any, is_bundle: bool, ver_code: str) -> str | None:
    """Checks if a row matches the required variant criteria."""
    text = row.text.lower()
    has_valid = any(a in text for a in (ctx.arch.lower(), "universal", "noarch"))
    has_any = any(a in text for a in ("arm64-v8a", "armeabi-v7a", "x86", "x86_64", "armeabi"))

    kind_match = ("bundle" in text) if is_bundle else ("bundle" not in text)
    if kind_match and (has_valid or not has_any):
        if not ver_code or str(ver_code) in text:
            if link := row.find('a', class_='accent_color'):
                rel_url = urljoin("https://www.apkmirror.com", link['href'])
                return _process_apkmirror_variant_page(ctx, rel_url, is_bundle)
    return None


def _download_apkmirror_variant(ctx: Context, rel_url: str,
                                ver_code: str, force_b: bool) -> str | None:
    """Finds and routes the specific variant from APKMirror."""
    ctx.limiter.wait()
    resp = ctx.scraper.get(rel_url, timeout=60)
    if _is_waf_blocked(resp.status_code, resp.text) or resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')
    if rows := soup.find_all('div', class_='table-row'):
        for is_bndl in ((False, True) if not force_b else (True,)):
            for row in rows:
                if out := _extract_apkm_row(ctx, row, is_bndl, ver_code):
                    return out
    elif btn := soup.find('a', class_='downloadButton'):
        is_bndl = "bundle" in btn.text.lower()
        if not force_b or is_bndl:
            return _process_apkmirror_variant_page(
                ctx, urljoin("https://www.apkmirror.com", btn['href']), is_bndl
            )
    return None


def scrape_apkmirror(ctx: Context, ver_code: str) -> str | None:
    """Scrape the APK from APKMirror."""
    print(f"[TIER 1] APKMirror: v{ctx.target_ver}")
    try:
        rel_url = _find_apkmirror_release(ctx)
        if not rel_url:
            print("[WARN] Release not found.")
            return None
        return _download_apkmirror_variant(ctx, rel_url, ver_code,
                                           ctx.app_data.get("force_bundle", False))
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 1 failed: {err}")
    return None


# --- TIER 2: APKPURE ---
def scrape_apkpure(ctx: Context) -> str | None:
    """Downloads APK from APKPure via apkeep."""
    print(f"[TIER 2] APKPure: v{ctx.target_ver}")
    dl_dir = os.path.join(ctx.out_dir, ctx.pkg)
    os.makedirs(dl_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="apkeep-") as tmp:
        try:
            cmd = ["apkeep", "-a", f"{ctx.pkg}@{ctx.target_ver}", "-d", "apk-pure", tmp]
            if subprocess.run(cmd, capture_output=True, check=False).returncode != 0:
                return None
        except OSError as err:
            print(f"[WARN] apkeep execution failed: {err}")
            return None

        files = []
        for ext in ("*.apk", "*.xapk", "*.apkm", "*.apks"):
            files.extend(glob.glob(os.path.join(tmp, ext)))
        if not files:
            return None
        dst = os.path.join(dl_dir, _safe_filename(os.path.basename(files[0])))
        shutil.copy2(files[0], dst)
        return dst


# --- TIER 3: APKCOMBO ---
def _find_apkcombo_page(ctx: Context) -> str | None:
    """Find the APKCombo download page."""
    raw_ver = ctx.target_ver
    base_ver = raw_ver.split('-')[0] if '-' in raw_ver and raw_ver[0].isdigit() else raw_ver
    app_url = f"https://apkcombo.com/a/{ctx.pkg}/"

    ctx.limiter.wait()
    resp = ctx.scraper.get(app_url, timeout=60)
    if _is_waf_blocked(resp.status_code, resp.text):
        return None
    if resp.status_code == 200:
        soup = BeautifulSoup(resp.text, 'html.parser')
        if (v_tag := soup.find('span', class_='version')) and base_ver in v_tag.text:
            if btn := soup.find('a', class_='button-download'):
                return btn.get('href')

    ctx.limiter.wait()
    old_resp = ctx.scraper.get(f"{app_url}old-versions/", timeout=60)
    if _is_waf_blocked(old_resp.status_code, old_resp.text) or old_resp.status_code != 200:
        return None
    for link in BeautifulSoup(old_resp.text, 'html.parser').find_all('a', href=True):
        href = link['href']
        if ctx.pkg in href and '/download/' in href:
            if (v_text := link.find(class_='vername')) and base_ver in v_text.text:
                return href
    return None


def _find_apkcombo_dl(ctx: Context, page_url: str) -> str | None:
    """Extracts the exact download link from APKCombo."""
    p_url = page_url if page_url.startswith('http') else f"https://apkcombo.com{page_url}"
    ctx.limiter.wait()
    resp = ctx.scraper.get(p_url, timeout=60)
    if _is_waf_blocked(resp.status_code, resp.text):
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')
    for link in soup.select('ul.list-download li a'):
        href = link.get('href', '')
        text = link.text.lower()
        if href.endswith(('.apk', '.apks')) or '&fp=' in href:
            if ctx.arch.lower() in text or 'universal' in text or 'armeabi' in text:
                return href

    if f_link := soup.select_one('ul.list-download li a'):
        return f_link.get('href')
    return None


def scrape_apkcombo(ctx: Context) -> str | None:
    """Scrape the APK from APKCombo."""
    print(f"[TIER 3] APKCombo: v{ctx.target_ver}")
    try:
        dl_page_url = _find_apkcombo_page(ctx)
        if not dl_page_url:
            print("[WARN] Version not found.")
            return None
        if final_dl := _find_apkcombo_dl(ctx, dl_page_url):
            out_path = ctx.get_out_path(".apks" if "apks" in final_dl else ".apk")
            print("[INFO] Downloading from APKCombo...")
            if download_file_stream(ctx.scraper, final_dl, out_path):
                return out_path
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 3 failed: {err}")
    return None


# --- TIER 4: APTOIDE ---
def scrape_aptoide(ctx: Context) -> str | None:
    """Scrape the APK from Aptoide."""
    print(f"[TIER 4] Aptoide API: v{ctx.target_ver}")
    raw_ver = ctx.target_ver
    base_ver = raw_ver.split('-')[0] if '-' in raw_ver and raw_ver[0].isdigit() else raw_ver
    try:
        ctx.limiter.wait()
        req_url = f"https://ws75.aptoide.com/api/7/apps/search/query={ctx.pkg}/limit=10"
        resp = ctx.scraper.get(req_url, timeout=60)
        if _is_waf_blocked(resp.status_code, resp.text) or resp.status_code != 200:
            return None

        dl_url = next((
            app.get("file", {}).get("path")
            for app in resp.json().get("datalist", {}).get("list", [])
            if app.get("package") == ctx.pkg and app.get("file", {}).get("vername") in (
                ctx.target_ver, base_ver)
        ), None)

        if dl_url:
            out_path = ctx.get_out_path(".apk")
            print("[INFO] Downloading from Aptoide...")
            if download_file_stream(ctx.scraper, dl_url, out_path):
                return out_path
        print("[WARN] Version not found.")
    except (requests.exceptions.RequestException, ValueError, OSError) as err:
        print(f"[ERROR] Tier 4 failed: {err}")
    return None


# --- TIER 5: UPTODOWN ---
def _resolve_bing_uptodown_fallback(ctx: Context) -> str | None:
    """Uses Bing search as a fallback to find Uptodown app pages."""
    query = quote_plus(f'site:uptodown.com/android/download "{ctx.pkg}"')
    try:
        ctx.limiter.wait()
        resp = ctx.scraper.get(f"https://www.bing.com/search?q={query}", timeout=60)
        if resp.status_code == 200:
            matches = re.findall(r'https://[a-z0-9-]+\.en\.uptodown\.com/android/download',
                                 resp.text)
            if matches:
                print("[INFO] Found Uptodown link via Bing.")
                return matches[0].replace("/download", "")
    except requests.exceptions.RequestException as err:
        print(f"[WARN] Bing fallback failed: {err}")
    return None


def _fetch_uptodown_page(ctx: Context, base_url: str) -> Tuple[str | None, str | None]:
    """Handles WAF checks and Bing fallback for Uptodown."""
    ctx.limiter.wait()
    resp = ctx.scraper.get(base_url, timeout=60)
    if _is_waf_blocked(resp.status_code, resp.text):
        return None, None

    if resp.status_code in (404, 410):
        print("[INFO] Direct URL missed. Attempting Bing fallback...")
        fallback_url = _resolve_bing_uptodown_fallback(ctx)
        if not fallback_url:
            return None, None
        ctx.limiter.wait()
        f_resp = ctx.scraper.get(fallback_url, timeout=60)
        if _is_waf_blocked(f_resp.status_code, f_resp.text):
            return None, None
        return f_resp.text, fallback_url
    return resp.text, base_url


def _find_uptodown_version(ctx: Context, base_url: str, html_text: str) -> Tuple:
    """Finds the version URL on Uptodown."""
    app_elem = BeautifulSoup(html_text, 'html.parser').find(id="detail-app-name")
    if not app_elem or not app_elem.has_attr("data-code"):
        return None, None, False

    d_code = app_elem["data-code"]
    raw_ver = ctx.target_ver
    base_ver = raw_ver.split('-')[0] if '-' in raw_ver and raw_ver[0].isdigit() else raw_ver

    for i in range(1, 21):
        ctx.limiter.wait()
        resp = ctx.scraper.get(f"{base_url}/apps/{d_code}/versions/{i}", timeout=60)
        if resp.status_code != 200:
            break
        try:
            for v_data in resp.json().get("data", []):
                if v_data.get("version") in (ctx.target_ver, base_ver):
                    v_obj = v_data.get("versionURL", {})
                    if v_obj.get("url") and v_obj.get("versionID") != "None":
                        v_url = f"{v_obj['url']}/{v_obj['extraURL']}/{v_obj['versionID']}"
                        return v_url, d_code, v_data.get("kindFile") == "xapk"
        except ValueError:
            continue
    return None, None, False


def _resolve_uptodown_variants(ctx: Context, soup: BeautifulSoup,
                               d_code: str, base_url: str) -> Any:
    """Resolves variant download buttons for Uptodown."""
    v_btn = soup.find(class_="button variants")
    if v_btn and v_btn.has_attr("data-version"):
        ctx.limiter.wait()
        url = f"https://en.uptodown.com/android/app/{d_code}/version/{v_btn['data-version']}/files"
        f_resp = ctx.scraper.get(url, timeout=60)
        if f_resp.status_code == 200:
            f_soup = BeautifulSoup(f_resp.json().get("content", ""), 'html.parser')
            sel_id = next(
                (v.find(class_='v-report')['data-file-id']
                 for v in f_soup.find_all('div', class_='variant')
                 if (ctx.arch.lower() in v.text.lower() or 'universal' in v.text.lower()) and
                 v.find(class_='v-report') and v.find(class_='v-report').has_attr('data-file-id')),
                None
            )
            if not sel_id:
                rep = f_soup.find(class_='v-report')
                sel_id = rep['data-file-id'] if rep and rep.has_attr('data-file-id') else None

            if sel_id:
                ctx.limiter.wait()
                url2 = f"{base_url}/download/{sel_id}-x"
                d_soup = BeautifulSoup(ctx.scraper.get(url2, timeout=60).text, 'html.parser')
                return d_soup.find(id="detail-download-button")
    return None


def scrape_uptodown(ctx: Context) -> str | None:
    """Scrape the APK from Uptodown."""
    print(f"[TIER 5] Uptodown API: v{ctx.target_ver}")
    search = ctx.app_data.get("search_term", ctx.pkg.replace('-', ' '))
    pkg_str = search.lower().replace(' ', '-')
    base_url = ctx.app_data.get("uptodown_url") or f"https://{pkg_str}.en.uptodown.com/android"

    try:
        html_text, valid_url = _fetch_uptodown_page(ctx, base_url)
        if not html_text or not valid_url:
            return None

        v_url, d_code, is_bundle = _find_uptodown_version(ctx, valid_url, html_text)
        if not v_url:
            print("[WARN] Version not found.")
            return None

        ctx.limiter.wait()
        soup = BeautifulSoup(ctx.scraper.get(v_url, timeout=60).text, 'html.parser')
        dl_btn = soup.find(id="detail-download-button")
        if not dl_btn:
            dl_btn = _resolve_uptodown_variants(ctx, soup, d_code, valid_url)

        if dl_btn and dl_btn.has_attr("data-url"):
            out_path = ctx.get_out_path(".xapk" if is_bundle else ".apk")
            print("[INFO] Downloading from Uptodown...")
            url2 = f"https://dw.uptodown.com/dwn/{dl_btn['data-url']}"
            if download_file_stream(ctx.scraper, url2, out_path, v_url, True):
                return out_path
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 5 failed: {err}")
    return None


# --- TIER 6: ARCHIVE.ORG ---
def _find_archive_link(ctx: Context, soup: BeautifulSoup, base_url: str) -> str | None:
    """Finds the matching archive link from the page soup."""
    valid = [ctx.arch.lower(), "universal", "noarch", "all"]
    link1 = next((f"{base_url}/{link.get('href')}" for link in soup.find_all('a')
                  if ctx.pkg in link.get('href', '') and ctx.target_ver in link.get('href', '') and
                  (any(v in link.get('href', '').lower() for v in valid) or ctx.arch == "all")),
                 None)
    if link1:
        return link1

    return next((f"{base_url}/{link.get('href')}" for link in soup.find_all('a')
                 if ctx.pkg in link.get('href', '') and ctx.target_ver in link.get('href', '')),
                None)


def scrape_archive(ctx: Context) -> str | None:
    """Scrape the APK from Archive.org as a final fallback."""
    arch_id = ctx.app_data.get("archive_id")
    if not arch_id:
        return None
    print(f"[TIER 6] Archive.org: v{ctx.target_ver}")
    ctx.limiter.wait()
    base_url = f"https://archive.org/download/{arch_id}"

    try:
        resp = ctx.scraper.get(f"{base_url}/", timeout=60)
        if resp.status_code != 200:
            return None

        dl_link = _find_archive_link(ctx, BeautifulSoup(resp.text, 'html.parser'), base_url)
        if dl_link:
            orig_ext = os.path.splitext(dl_link)[1]
            orig_ext = orig_ext if orig_ext in ['.apk', '.xapk', '.apkm', '.apks'] else '.apk'
            out_path = ctx.get_out_path(orig_ext)
            print("[INFO] Downloading from Archive...")
            if download_file_stream(ctx.scraper, dl_link, out_path):
                return out_path
        print("[WARN] Not found on Archive.")
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 6 failed: {err}")
    return None


# --- TIER 7: GITHUB RELEASES ---
def scrape_github(ctx: Context) -> str | None:
    """Scrape the APK directly from GitHub Releases."""
    gh_repo, gh_asset = ctx.app_data.get("github_repo"), ctx.app_data.get("github_asset")
    if not gh_repo or not gh_asset:
        return None
    print(f"[TIER 7] GitHub Releases: v{ctx.target_ver}")

    for tag in [f"v{ctx.target_ver}", ctx.target_ver]:
        ctx.limiter.wait()
        dl_link = f"https://github.com/{gh_repo}/releases/download/{tag}/{gh_asset}"
        out_path = ctx.get_out_path(".apk")
        try:
            if ctx.scraper.head(dl_link, timeout=10, allow_redirects=True).status_code == 200:
                print("[INFO] Downloading from GitHub...")
                if download_file_stream(ctx.scraper, dl_link, out_path):
                    return out_path
        except requests.exceptions.RequestException:
            continue
    return None


# --- TIER 8: DIRECT URL ---
def scrape_direct(ctx: Context) -> str | None:
    """Scrape the APK directly from a patterned URL."""
    tmpl = ctx.app_data.get("direct_url")
    if not tmpl:
        return None

    print(f"[TIER 8] Direct URL: v{ctx.target_ver}")
    dl_link = tmpl.replace("[VERSI]", ctx.target_ver).replace("[ARCH]", ctx.arch)
    out_path = ctx.get_out_path(".apk")

    ctx.limiter.wait()
    try:
        if ctx.scraper.head(dl_link, timeout=10, allow_redirects=True).status_code == 200:
            print("[INFO] Downloading from Direct URL...")
            if download_file_stream(ctx.scraper, dl_link, out_path):
                return out_path
    except requests.exceptions.RequestException:
        pass

    print(f"[WARN] Direct link not reachable: {dl_link}")
    return None


def _run_scraper(name: str, ctx: Context, args: Any, v_code: Any) -> str | None:
    """Routes the download to the correct scraper."""
    scrapers = {
        "direct": lambda: scrape_direct(ctx),
        "github": lambda: scrape_github(ctx),
        "huggingface": lambda: scrape_huggingface(ctx, args.hf_user),
        "apkmirror": lambda: scrape_apkmirror(ctx, v_code),
        "apkpure": lambda: scrape_apkpure(ctx),
        "apkcombo": lambda: scrape_apkcombo(ctx),
        "aptoide": lambda: scrape_aptoide(ctx),
        "uptodown": lambda: scrape_uptodown(ctx),
        "archive": lambda: scrape_archive(ctx)
    }
    func = scrapers.get(name)
    if func:
        return func()
    return None


def download_apk(ctx: Context, args: Any) -> str | None:
    """Fallback mechanism or targeted download for APK through multiple sources."""
    if ctx.target_ver.lower() == "any":
        print("[ERROR] Version defined as 'Any'. Skipping.")
        return None

    os.makedirs(os.path.join(ctx.out_dir, ctx.pkg), exist_ok=True)
    v_code = ctx.app_data.get("version_codes", {}).get(ctx.arch)

    path = _run_scraper(args.download_source.lower(), ctx, args, v_code)
    if not path:
        for src in ["direct", "github", "huggingface", "apkmirror", "apkpure",
                    "apkcombo", "aptoide", "uptodown", "archive"]:
            path = _run_scraper(src, ctx, args, v_code)
            if path:
                break

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
    """Helper to parse the custom version argument."""
    if not ver_str:
        return {}
    if '=' in ver_str:
        return {
            p.split('=', 1)[0].strip(): p.split('=', 1)[1].strip()
            for p in ver_str.split(',')
        }
    return {"_global": ver_str.strip()}


def _get_patched_apk_path(app: str, ver: str, arch: str, args: Any, state: dict) -> str:
    """Generates the output path for the patched APK."""
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
    """Generates options JSON file using the CLI."""
    json_file = os.path.join(workspace, f"{_safe_filename(app_name)}-options.json")
    cmd = ["java", "-jar", args.cli, "options-create", "--patches", args.patches,
           "--out", json_file, "--filter-package-name", app_data["package"]]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        err_out = (res.stderr or res.stdout or '').strip()[-500:]
        print(f"[WARN] CLI options-create failed (exit {res.returncode}): {err_out}")
    if app_data.get("options_override") and os.path.exists(json_file):
        print(f"[INFO] Injecting custom patch options for {app_name}...")
        update_options_json(json_file, app_data["options_override"])
    return json_file


def process_single_app(app_name: str, args: Any, app_data: dict,
                       custom_ver: str, state: dict) -> None:
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
