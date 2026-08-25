"""
Automated APK Downloader and Patcher using the CLI.
Handles multi-tier downloading, dynamic options.json injection,
and exclusive patch execution.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from typing import Dict, Any
from urllib.parse import urljoin, urlparse

try:
    import requests
    import cloudscraper
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("[FATAL] Missing libs. Run: pip install cloudscraper beautifulsoup4 requests")

MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024

@dataclass
class Context:
    """Holds common variables for the scraping process."""
    scraper: Any
    app_data: dict
    target_ver: str
    arch: str
    out_dir: str

    @property
    def pkg(self) -> str:
        """Returns the package name."""
        return self.app_data["package"]

def load_config() -> Dict[str, Dict[str, Any]]:
    """Loads the ecosystem configuration from the JSON file."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecosystems.json")
    if not os.path.isfile(config_path):
        sys.exit(f"[FATAL] '{config_path}' not found.")
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (OSError, json.JSONDecodeError) as err:
        sys.exit(f"[FATAL] Failed to load configuration: {err}")
    if not isinstance(data, dict):
        sys.exit("[FATAL] ecosystems.json must contain a JSON object.")
    return data

ECOSYSTEMS: Dict[str, Dict[str, Any]] = load_config()

def get_scraper() -> cloudscraper.CloudScraper:
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

def _validate_http_url(url: str):
    """Accept only HTTP(S) URLs before handing them to requests."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Unsupported download URL: {url!r}")

def _safe_filename(name: str, fallback: str = "artifact") -> str:
    """Returns a filesystem-safe single path component."""
    cleaned = os.path.basename(str(name)).strip()
    if not cleaned or cleaned in {".", ".."} or any(c in cleaned for c in ('/', '\\', '\x00')):
        return fallback
    return cleaned

def download_file_stream(scraper, url: str, out_path: str, referer: str = None,
                         check_dmca: bool = False) -> bool:
    """Downloads a file safely with streaming, size limits, and atomic replacement."""
    try:
        _validate_http_url(url)
        headers = {"Referer": referer} if referer else None
        with scraper.get(
            url, stream=True, headers=headers, timeout=(10, 60), allow_redirects=True
        ) as resp:
            if resp.status_code != 200:
                print(f"[ERROR] Download rejected HTTP {resp.status_code}")
                return False
            if check_dmca:
                content_disp = resp.headers.get('Content-Disposition', '').lower()
                if 'uptodown-app-store' in content_disp:
                    print("[ERROR] DMCA Trap detected (Store APK).")
                    return False

            declared_size = resp.headers.get("Content-Length")
            if declared_size and declared_size.isdigit():
                if int(declared_size) > MAX_DOWNLOAD_BYTES:
                    print(f"[ERROR] Download exceeds {MAX_DOWNLOAD_BYTES} byte limit.")
                    return False

            out_path = os.path.abspath(out_path)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix=".download-", dir=os.path.dirname(out_path))
            os.close(fd)
            total = 0
            try:
                with open(temp_path, 'wb') as apk_file:
                    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError(f"download exceeds {MAX_DOWNLOAD_BYTES} limit")
                        apk_file.write(chunk)
                os.replace(temp_path, out_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            return True
    except (requests.exceptions.RequestException, OSError, ValueError) as err:
        print(f"[ERROR] Request failed: {err}")
    return False

def _extract_xapk(file_path: str, zip_obj, namelist: list) -> str:
    """Extracts a single APK from an XAPK/APKM wrapper without loading it into RAM."""
    apk_files = [item for item in namelist if item.lower().endswith('.apk')]
    if len(apk_files) == 1 and not file_path.lower().endswith('.apkm'):
        print("[INFO] XAPK Wrapper detected. Extracting APK...")
        member = zip_obj.getinfo(apk_files[0])
        if member.file_size > MAX_DOWNLOAD_BYTES:
            raise ValueError("embedded APK exceeds extraction size limit")
        new_path = file_path.rsplit('.', 1)[0] + '.apk'
        with zip_obj.open(member) as source, open(new_path, 'wb') as target:
            shutil.copyfileobj(source, target, length=CHUNK_SIZE)
        os.remove(file_path)
        return new_path
    return file_path

def process_downloaded_file(file_path: str):
    """Processes downloaded files, handling pure APKs and XAPK wrappers."""
    try:
        if not zipfile.is_zipfile(file_path):
            print("[ERROR] Downloaded artifact is not a valid ZIP/APK container.")
            return None
        with zipfile.ZipFile(file_path, 'r') as zip_obj:
            namelist = zip_obj.namelist()
            if 'AndroidManifest.xml' in namelist and 'classes.dex' in namelist:
                if not file_path.endswith('.apk'):
                    new_path = file_path.rsplit('.', 1)[0] + '.apk'
                    os.replace(file_path, new_path)
                    print("[INFO] Auto-corrected: Pure APK detected.")
                    return new_path
                return file_path
            return _extract_xapk(file_path, zip_obj, namelist)
    except (zipfile.BadZipFile, OSError, ValueError) as err:
        print(f"[WARN] Inspection failed: {err}")
    return None

# --- Options Injector ---
def _update_patch_options(target_dict: dict, override_data: dict):
    """Updates the enabled status and options for a specific patch."""
    if "enabled" in override_data:
        target_dict["enabled"] = override_data["enabled"]
    if "options" in override_data:
        if "options" not in target_dict or not isinstance(target_dict["options"], dict):
            target_dict["options"] = {}
        for key, val in override_data["options"].items():
            target_dict["options"][key] = val

def _search_and_update(obj, patch_name: str, override_data: dict) -> bool:
    """Recursively updates the first matching patch and reports whether it was found."""
    if isinstance(obj, dict):
        if patch_name in obj and isinstance(obj[patch_name], dict):
            _update_patch_options(obj[patch_name], override_data)
            return True
        for val in obj.values():
            if _search_and_update(val, patch_name, override_data):
                return True
        return False
    if isinstance(obj, list):
        for item in obj:
            if _search_and_update(item, patch_name, override_data):
                return True
        return False
    return False

def update_options_json(filepath: str, overrides: dict):
    """Injects custom options into the CLI-generated JSON file."""
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
        print("[INFO] Custom patch options injected successfully.")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as err:
        print(f"[WARN] Failed to apply options overrides: {err}")

# --- TIER 0: HUGGINGFACE DATASETS ---
def scrape_huggingface(ctx: Context, hf_user: str):
    """Scrape the APK directly from HuggingFace Datasets as the primary source."""
    hf_repo = ctx.app_data.get("hf_repo", f"{hf_user}/{ctx.app_data.get('archive_id')}")
    if not ctx.app_data.get("archive_id") and not ctx.app_data.get("hf_repo"):
        return None

    print(f"[TIER 0] HuggingFace: v{ctx.target_ver}")
    time.sleep(1)
    base_url = f"https://huggingface.co/datasets/{hf_repo}/resolve/main"

    for ext in ['.apk', '.xapk', '.apkm', '.apks']:
        dl_link = f"{base_url}/{ctx.pkg}_{ctx.target_ver}{ext}"
        safe_pkg = _safe_filename(ctx.pkg)
        safe_ver = _safe_filename(ctx.target_ver)
        out_path = os.path.join(ctx.out_dir, f"{safe_pkg}_{safe_ver}{ext}")
        try:
            req = ctx.scraper.head(dl_link, timeout=10, allow_redirects=True)
            if req.status_code == 200:
                print("[INFO] Downloading from HuggingFace Vault...")
                if download_file_stream(ctx.scraper, dl_link, out_path):
                    print(f"[INFO] Tier 0 Success ({ext})")
                    return out_path
        except requests.exceptions.RequestException:
            continue

    print(f"[WARN] Not found in HuggingFace dataset '{hf_repo}'.")
    return None

# --- TIER 1: APKMIRROR ---
def _find_apkmirror_release(ctx: Context):
    """Finds the release page URL on APKMirror."""
    ver = ctx.target_ver
    base_ver = ver.split('-')[0] if '-' in ver and ver[0].isdigit() else ver
    search_kw = ctx.app_data.get("search_term", ctx.pkg)
    query = urllib.parse.quote_plus(f"{search_kw} {base_ver}")
    url = f"https://www.apkmirror.com/?post_type=app_release&s={query}"

    resp = ctx.scraper.get(url, timeout=30)
    if resp.status_code != 200:
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

def _process_apkmirror_variant_page(ctx: Context, var_url: str, is_bundle: bool):
    """Processes the specific variant download page and retrieves the file."""
    v_resp = ctx.scraper.get(var_url, timeout=30)
    if v_resp.status_code != 200:
        return None
    v_soup = BeautifulSoup(v_resp.text, 'html.parser')
    btn = v_soup.find('a', class_='downloadButton')
    if not btn:
        return None

    dl_page = urljoin("https://www.apkmirror.com", btn['href'])
    d_resp = ctx.scraper.get(dl_page, timeout=30)
    if d_resp.status_code != 200:
        return None
    d_soup = BeautifulSoup(d_resp.text, 'html.parser')
    dl_btn = d_soup.find("a", {"rel": "nofollow"})

    if dl_btn and 'href' in dl_btn.attrs:
        ext = '.apkm' if is_bundle else '.apk'
        safe_pkg = _safe_filename(ctx.pkg)
        safe_ver = _safe_filename(ctx.target_ver)
        out_path = os.path.join(ctx.out_dir, f"{safe_pkg}_{safe_ver}{ext}")
        dl_url = urljoin("https://www.apkmirror.com", dl_btn['href'])
        print("[INFO] Downloading from APKMirror...")
        if download_file_stream(ctx.scraper, dl_url, out_path, dl_page):
            print(f"[INFO] Tier 1 Success ({ext})")
            return out_path
    return None

def _download_apkmirror_variant(ctx: Context, rel_url: str, ver_code: str, force_b: bool):
    """Finds and routes the specific variant from APKMirror."""
    resp = ctx.scraper.get(rel_url, timeout=30)
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, 'html.parser')
    valid = [ctx.arch.lower(), "universal", "noarch"]
    var_url = None
    is_bundle = False

    for row in soup.find_all('div', class_='table-row'):
        text = row.text.lower()
        is_apk = "apk" in text and "bundle" not in text
        if (not force_b and is_apk) or ("bundle" in text):
            if any(a in text for a in valid) and (not ver_code or str(ver_code) in text):
                link = row.find('a', class_='accent_color')
                if link:
                    var_url = urljoin("https://www.apkmirror.com", link['href'])
                    is_bundle = "bundle" in text
                    break

    if not var_url:
        return None

    return _process_apkmirror_variant_page(ctx, var_url, is_bundle)

def scrape_apkmirror(ctx: Context, ver_code: str):
    """Scrape the APK from APKMirror."""
    print(f"[TIER 1] APKMirror: v{ctx.target_ver}")
    time.sleep(3)
    try:
        rel_url = _find_apkmirror_release(ctx)
        if not rel_url:
            print("[WARN] Release not found.")
            return None
        force_b = ctx.app_data.get("force_bundle", False)
        return _download_apkmirror_variant(ctx, rel_url, ver_code, force_b)
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 1 failed: {err}")
    return None

# --- TIER 2: APKPURE ---
def scrape_apkpure(ctx: Context):
    """Downloads APK from APKPure via apkeep."""
    print(f"[TIER 2] APKPure: v{ctx.target_ver}")
    dl_dir = os.path.join(ctx.out_dir, ctx.pkg)
    os.makedirs(dl_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="apkeep-") as tmp:
        try:
            cmd = ["apkeep", "-a", f"{ctx.pkg}@{ctx.target_ver}", "-d", "apk-pure", tmp]
            result = subprocess.run(cmd, capture_output=True, check=False)
            if result.returncode != 0:
                print("[WARN] APKPure failed.")
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
        print("[INFO] Tier 2 Success.")
        return dst

# --- TIER 3: APKCOMBO ---
def _find_apkcombo_page(ctx: Context):
    """Find the APKCombo download page."""
    ver = ctx.target_ver
    base_ver = ver.split('-')[0] if '-' in ver and ver[0].isdigit() else ver
    app_url = f"https://apkcombo.com/a/{ctx.pkg}/"
    resp = ctx.scraper.get(app_url, timeout=30)
    if resp.status_code == 200:
        soup = BeautifulSoup(resp.text, 'html.parser')
        ver_tag = soup.find('span', class_='version')
        if ver_tag and base_ver in ver_tag.text:
            btn = soup.find('a', class_='button-download')
            if btn:
                return btn.get('href')

    old_resp = ctx.scraper.get(f"{app_url}old-versions/", timeout=30)
    if old_resp.status_code == 200:
        old_soup = BeautifulSoup(old_resp.text, 'html.parser')
        for link in old_soup.find_all('a', href=True):
            href = link['href']
            if ctx.pkg in href and '/download/' in href:
                v_text = link.find(class_='vername')
                if v_text and base_ver in v_text.text:
                    return href
    return None

def _find_apkcombo_dl(ctx: Context, page_url: str):
    """Extracts the exact download link from APKCombo."""
    p_url = page_url
    if not p_url.startswith('http'):
        p_url = "https://apkcombo.com" + page_url
    soup = BeautifulSoup(ctx.scraper.get(p_url, timeout=30).text, 'html.parser')

    for link in soup.select('ul.list-download li a'):
        href = link.get('href', '')
        text = link.text.lower()
        if href.endswith(('.apk', '.apks')) or '&fp=' in href:
            if ctx.arch.lower() in text or 'universal' in text or 'armeabi' in text:
                return href

    f_link = soup.select_one('ul.list-download li a')
    if f_link:
        return f_link.get('href')
    return None

def scrape_apkcombo(ctx: Context):
    """Scrape the APK from APKCombo."""
    print(f"[TIER 3] APKCombo: v{ctx.target_ver}")
    time.sleep(3)
    try:
        dl_page_url = _find_apkcombo_page(ctx)
        if not dl_page_url:
            print("[WARN] Version not found.")
            return None

        final_dl = _find_apkcombo_dl(ctx, dl_page_url)
        if final_dl:
            ext = ".apks" if "apks" in final_dl else ".apk"
            safe_pkg = _safe_filename(ctx.pkg)
            safe_ver = _safe_filename(ctx.target_ver)
            out_path = os.path.join(ctx.out_dir, f"{safe_pkg}_{safe_ver}{ext}")
            print("[INFO] Downloading from APKCombo...")
            if download_file_stream(ctx.scraper, final_dl, out_path):
                print(f"[INFO] Tier 3 Success ({ext})")
                return out_path
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 3 failed: {err}")
    return None

# --- TIER 4: APTOIDE ---
def scrape_aptoide(ctx: Context):
    """Scrape the APK from Aptoide."""
    print(f"[TIER 4] Aptoide API: v{ctx.target_ver}")
    time.sleep(2)
    ver = ctx.target_ver
    base_ver = ver.split('-')[0] if '-' in ver and ver[0].isdigit() else ver

    try:
        api_url = f"https://ws75.aptoide.com/api/7/apps/search/query={ctx.pkg}/limit=10"
        resp = ctx.scraper.get(api_url, timeout=30)
        if resp.status_code != 200:
            return None

        data = resp.json()
        dl_url = None
        for app in data.get("datalist", {}).get("list", []):
            if app.get("package") == ctx.pkg:
                if app.get("file", {}).get("vername") in (ver, base_ver):
                    dl_url = app.get("file", {}).get("path")
                    break

        if dl_url:
            safe_pkg = _safe_filename(ctx.pkg)
            safe_ver = _safe_filename(ctx.target_ver)
            out_path = os.path.join(ctx.out_dir, f"{safe_pkg}_{safe_ver}.apk")
            print("[INFO] Downloading from Aptoide...")
            if download_file_stream(ctx.scraper, dl_url, out_path):
                print("[INFO] Tier 4 Success (.apk)")
                return out_path
        print("[WARN] Version not found.")
    except (requests.exceptions.RequestException, ValueError, OSError) as err:
        print(f"[ERROR] Tier 4 failed: {err}")
    return None

# --- TIER 5: UPTODOWN ---
def _find_uptodown_version(ctx: Context, base_url: str):
    """Finds the version URL on Uptodown."""
    soup = BeautifulSoup(ctx.scraper.get(base_url, timeout=30).text, 'html.parser')
    app_elem = soup.find(id="detail-app-name")
    if not app_elem or not app_elem.has_attr("data-code"):
        return None, None, False

    d_code = app_elem["data-code"]
    ver = ctx.target_ver
    base_ver = ver.split('-')[0] if '-' in ver and ver[0].isdigit() else ver

    for i in range(1, 21):
        resp = ctx.scraper.get(f"{base_url}/apps/{d_code}/versions/{i}", timeout=30)
        if resp.status_code != 200:
            break
        try:
            data = resp.json().get("data", [])
        except ValueError:
            continue

        for v_data in data:
            if v_data.get("version") in (ver, base_ver):
                v_obj = v_data.get("versionURL", {})
                if v_obj.get("url") and v_obj.get("versionID") != "None":
                    v_url = f"{v_obj['url']}/{v_obj['extraURL']}/{v_obj['versionID']}"
                    is_bundle = v_data.get("kindFile") == "xapk"
                    return v_url, d_code, is_bundle
    return None, None, False

def _extract_uptodown_file_id(f_soup, arch: str):
    """Extracts the precise file ID for a specific architecture."""
    sel_id = None
    for variant in f_soup.find_all('div', class_='variant'):
        text = variant.text.lower()
        if arch.lower() in text or 'universal' in text:
            rep = variant.find(class_='v-report')
            if rep and rep.has_attr('data-file-id'):
                sel_id = rep['data-file-id']
                break
    if not sel_id:
        rep = f_soup.find(class_='v-report')
        if rep and rep.has_attr('data-file-id'):
            sel_id = rep['data-file-id']
    return sel_id

def _resolve_uptodown_variants(ctx: Context, soup, d_code: str, base_url: str):
    """Resolves variant download buttons for Uptodown."""
    v_btn = soup.find(class_="button variants")
    if v_btn and v_btn.has_attr("data-version"):
        f_url = (
            f"https://en.uptodown.com/android/app/{d_code}"
            f"/version/{v_btn['data-version']}/files"
        )
        f_resp = ctx.scraper.get(f_url, timeout=30)
        if f_resp.status_code == 200:
            f_soup = BeautifulSoup(f_resp.json().get("content", ""), 'html.parser')
            sel_id = _extract_uptodown_file_id(f_soup, ctx.arch)
            if sel_id:
                d_url = f"{base_url}/download/{sel_id}-x"
                d_resp = ctx.scraper.get(d_url, timeout=30)
                d_soup = BeautifulSoup(d_resp.text, 'html.parser')
                return d_soup.find(id="detail-download-button")
    return None

def scrape_uptodown(ctx: Context):
    """Scrape the APK from Uptodown."""
    print(f"[TIER 5] Uptodown API: v{ctx.target_ver}")
    time.sleep(3)
    search = ctx.app_data.get("search_term", ctx.pkg.replace('-', ' '))
    base_url = ctx.app_data.get("uptodown_url")
    if not base_url:
        base_url = f"https://{search.lower().replace(' ', '-')}.en.uptodown.com/android"

    try:
        if ctx.scraper.get(base_url, timeout=30).status_code == 410:
            return None
        v_url, d_code, is_bundle = _find_uptodown_version(ctx, base_url)
        if not v_url:
            print("[WARN] Version not found.")
            return None

        soup = BeautifulSoup(ctx.scraper.get(v_url, timeout=30).text, 'html.parser')
        dl_btn = soup.find(id="detail-download-button")

        if not dl_btn:
            dl_btn = _resolve_uptodown_variants(ctx, soup, d_code, base_url)

        if dl_btn and dl_btn.has_attr("data-url"):
            ext = ".xapk" if is_bundle else ".apk"
            safe_pkg = _safe_filename(ctx.pkg)
            safe_ver = _safe_filename(ctx.target_ver)
            out_path = os.path.join(ctx.out_dir, f"{safe_pkg}_{safe_ver}{ext}")
            dl_url = f"https://dw.uptodown.com/dwn/{dl_btn['data-url']}"
            print("[INFO] Downloading from Uptodown...")
            if download_file_stream(ctx.scraper, dl_url, out_path, v_url, True):
                print(f"[INFO] Tier 5 Success ({ext})")
                return out_path
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 5 failed: {err}")
    return None

# --- TIER 6: ARCHIVE.ORG ---
def scrape_archive(ctx: Context):
    """Scrape the APK from Archive.org as a final fallback."""
    arch_id = ctx.app_data.get("archive_id")
    if not arch_id:
        return None

    print(f"[TIER 6] Archive.org: v{ctx.target_ver}")
    time.sleep(2)
    base_url = f"https://archive.org/download/{arch_id}"

    try:
        resp = ctx.scraper.get(f"{base_url}/", timeout=30)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        valid = [ctx.arch.lower(), "universal", "noarch", "all"]
        dl_link = None

        for link in soup.find_all('a'):
            href = link.get('href', '')
            if ctx.pkg in href and ctx.target_ver in href:
                if any(v in href.lower() for v in valid) or ctx.arch == "all":
                    dl_link = f"{base_url}/{href}"
                    break

        if not dl_link:
            for link in soup.find_all('a'):
                href = link.get('href', '')
                if ctx.pkg in href and ctx.target_ver in href:
                    dl_link = f"{base_url}/{href}"
                    break

        if dl_link:
            orig_ext = os.path.splitext(dl_link)[1]
            if orig_ext not in ['.apk', '.xapk', '.apkm', '.apks']:
                orig_ext = '.apk'
            safe_pkg = _safe_filename(ctx.pkg)
            safe_ver = _safe_filename(ctx.target_ver)
            out_path = os.path.join(ctx.out_dir, f"{safe_pkg}_{safe_ver}{orig_ext}")
            print("[INFO] Downloading from Archive...")
            if download_file_stream(ctx.scraper, dl_link, out_path):
                print(f"[INFO] Tier 6 Success ({orig_ext})")
                return out_path
        print(f"[WARN] Not found. (Did you name it '{ctx.pkg}_{ctx.target_ver}.apk'?)")
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 6 failed: {err}")
    return None

# --- TIER 7: GITHUB RELEASES ---
def scrape_github(ctx: Context):
    """Scrape the APK directly from GitHub Releases."""
    gh_repo = ctx.app_data.get("github_repo")
    gh_asset = ctx.app_data.get("github_asset")
    if not gh_repo or not gh_asset:
        return None

    print(f"[TIER 7] GitHub Releases: v{ctx.target_ver}")
    time.sleep(1)

    for tag in [f"v{ctx.target_ver}", ctx.target_ver]:
        dl_link = f"https://github.com/{gh_repo}/releases/download/{tag}/{gh_asset}"
        safe_pkg = _safe_filename(ctx.pkg)
        safe_ver = _safe_filename(ctx.target_ver)
        out_path = os.path.join(ctx.out_dir, f"{safe_pkg}_{safe_ver}.apk")
        try:
            req = ctx.scraper.head(dl_link, timeout=10, allow_redirects=True)
            if req.status_code == 200:
                print("[INFO] Downloading from GitHub Releases...")
                if download_file_stream(ctx.scraper, dl_link, out_path):
                    print("[INFO] Tier 7 Success (.apk)")
                    return out_path
        except requests.exceptions.RequestException:
            continue
    print(f"[WARN] Not found in GitHub Releases for '{gh_repo}'.")
    return None

def download_apk(ctx: Context, args):
    """Fallback mechanism or targeted download for APK through multiple sources."""
    if ctx.target_ver.lower() == "any":
        print("[ERROR] Version defined as 'Any'. Skipping.")
        return None

    os.makedirs(os.path.join(ctx.out_dir, ctx.pkg), exist_ok=True)
    src = args.download_source.lower()
    path = None

    if src == "huggingface":
        path = scrape_huggingface(ctx, args.hf_user)
    elif src == "archive":
        path = scrape_archive(ctx)
    elif src == "apkmirror":
        v_code = ctx.app_data.get("version_codes", {}).get(ctx.arch)
        path = scrape_apkmirror(ctx, v_code)
    elif src == "apkpure":
        path = scrape_apkpure(ctx)
    elif src == "apkcombo":
        path = scrape_apkcombo(ctx)
    elif src == "aptoide":
        path = scrape_aptoide(ctx)
    elif src == "uptodown":
        path = scrape_uptodown(ctx)
    elif src == "github":
        path = scrape_github(ctx)
    else:
        v_code = ctx.app_data.get("version_codes", {}).get(ctx.arch)
        path = (
            scrape_github(ctx) or
            scrape_huggingface(ctx, args.hf_user) or
            scrape_apkmirror(ctx, v_code) or
            scrape_apkpure(ctx) or
            scrape_apkcombo(ctx) or
            scrape_aptoide(ctx) or
            scrape_uptodown(ctx) or
            scrape_archive(ctx)
        )

    if path:
        return process_downloaded_file(path)

    print(f"[FATAL] Exhausted sources or specific source failed for {ctx.pkg}.")
    return None

def write_changelog(args, apps_patched: list, workspace: str, clean_ver: str):
    """Write the patched apps changelog to a markdown file."""
    log_path = os.path.join(workspace, "changelog.md")
    with open(log_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(f"## Automatically Patched Applications ({args.ecosystem})\n\n")

        if args.is_prerelease.lower() == "true":
            file_obj.write("> [!WARNING]\n")
            file_obj.write("> **Patched using pre-release tools. Use with caution.**\n\n")

        file_obj.write(f"Generated using **v{clean_ver}** from `{args.ecosystem}`.\n")
        file_obj.write(f"**Source:** [Repository]({args.repo_url})\n\n### Apps:\n")
        for app in apps_patched:
            b_str = f" (Build: {app['build']})" if app.get('build') else ""
            app_n, app_v = app['name'], app['version']
            app_a = app['arch']
            file_obj.write(f"- **{app_n}** (v{app_v}{b_str} - `{app_a}`)\n")

        file_obj.write("\n---\n")
        file_obj.write("### ⚠️ microG Required\n")
        file_obj.write("For Google Apps, install [microG-RE]")
        file_obj.write(f"({args.microg_url}).\n")

def parse_custom_versions(custom_version_str: str) -> dict:
    """Parses the custom version string into a dictionary."""
    parsed = {}
    if not custom_version_str:
        return parsed
    if "=" in custom_version_str:
        for part in custom_version_str.split(','):
            if "=" in part:
                key, val = part.split('=', 1)
                parsed[key.strip()] = val.strip()
    else:
        parsed["_global"] = custom_version_str.strip()
    return parsed

def resolve_target_version(app_data: dict, selection: str, custom_ver: str) -> str:
    """Resolves the target version to download based on user input."""
    if selection.lower() == "custom":
        if custom_ver:
            return custom_ver
        print("[WARN] Custom version missing. Falling back to stable.")
    if selection.lower() in ["beta", "pre-release", "latest", "experimental"]:
        if app_data.get("beta"):
            return app_data["beta"][0]
        print("[WARN] No beta version defined. Falling back to stable.")
    return app_data["stable"][0]

def build_patch_command(args, app_data: dict, paths: tuple, target_arch: str) -> list:
    """Builds the shell command for the CLI, including exclusive patch handling."""
    input_apk, json_file, out_apk = paths
    cmd = [
        "java", "-Xmx4G", "-jar", args.cli, "patch", "--patches", args.patches,
        "--options-file", json_file, "--out", out_apk, "--bytecode-mode", "FULL"
    ]
    if args.version_selection.lower() in ("beta", "pre-release", "latest",
                                          "experimental", "custom"):
        cmd.append("--force")
    if app_data.get("strip"):
        cmd.extend(["--striplibs", target_arch])
    if args.continue_on_error.lower() == "true":
        cmd.append("--continue-on-error")

    if args.keystore and args.ks_alias and args.ks_pass:
        cmd.extend([
            "--keystore", args.keystore, "--keystore-entry-alias", args.ks_alias,
            "--keystore-password", args.ks_pass, "--keystore-entry-password", args.ks_pass
        ])
        if args.signer:
            cmd.extend(["--signer", args.signer])

    exc_list = app_data.get("exclusive_patches", [])
    if exc_list:
        print("[INFO] Exclusive mode detected. Generating targeted patch command...")
        cmd.append("--exclusive")
        for patch_name in exc_list:
            cmd.extend(["-e", patch_name])

    cmd.append(input_apk)
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

def _generate_options_json(app_name: str, args, app_data: dict, workspace: str) -> str:
    """Generates options JSON file using the CLI."""
    json_file = os.path.join(workspace, f"{_safe_filename(app_name)}-options.json")
    cmd = [
        "java", "-jar", args.cli, "options-create", "--patches", args.patches,
        "--out", json_file, "--filter-package-name", app_data["package"]
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        err_str = (res.stderr or res.stdout or "").strip()
        print(f"[WARN] CLI options-create failed (exit {res.returncode}): {err_str[-500:]}")
    if app_data.get("options_override") and os.path.exists(json_file):
        print(f"[INFO] Injecting custom patch options for {app_name}...")
        update_options_json(json_file, app_data["options_override"])
    return json_file

def process_single_app(app_name: str, args, app_data: dict, custom_ver: str, state: dict):
    """Processes a single app for downloading and patching."""
    t_ver = resolve_target_version(app_data, args.version_selection, custom_ver)
    arch = app_data.get("force_arch", args.arch)

    print(f"\n--- {app_name} ({app_data['package']}) ---")
    ctx = Context(get_scraper(), app_data, t_ver, arch, state["in_dir"])
    apk_path = download_apk(ctx, args)
    if not apk_path:
        return

    json_file = _generate_options_json(app_name, args, app_data, state["workspace"])

    out_apk = os.path.join(
        state["out_dir"],
        f"{_safe_filename(app_name)}_{_safe_filename(args.ecosystem)}_patched_"
        f"{_safe_filename(t_ver)}-{_safe_filename(arch)}_patches_"
        f"{_safe_filename(state['clean_ver'])}.apk"
    )

    print("[INFO] Patching via CLI...")
    ret_code, zero_patches = execute_patch_cli(
        build_patch_command(args, app_data, (apk_path, json_file, out_apk), arch)
    )

    if ret_code == 0 and not zero_patches:
        print(f"\n[INFO] SUCCESS: {app_name}")
        state["success"].append({
            "name": app_name,
            "version": t_ver,
            "build": app_data.get("version_codes", {}).get(arch),
            "arch": arch
        })
    else:
        reason = "DMCA Trap" if zero_patches else f"Exit code {ret_code}"
        print(f"\n[ERROR] FAILED: {app_name}. Reason: {reason}")
        if os.path.exists(out_apk):
            os.remove(out_apk)
    time.sleep(5)

def run_patcher(args):
    """Main execution function to handle the patching loop."""
    if args.ecosystem not in ECOSYSTEMS:
        sys.exit(f"[FATAL] Ecosystem '{args.ecosystem}' not found in JSON.")

    workspace = f"./{_safe_filename(args.ecosystem)}"
    state = {
        "in_dir": f"{workspace}/Input",
        "out_dir": f"{workspace}/Output",
        "workspace": workspace,
        "clean_ver": args.patches_version.lstrip('v') if args.patches_version else "unknown",
        "success": []
    }
    os.makedirs(state["in_dir"], exist_ok=True)
    os.makedirs(state["out_dir"], exist_ok=True)

    print(f"=== INITIALIZING WORKSPACE: {args.ecosystem.upper()} ===")
    ecosystem_apps = ECOSYSTEMS[args.ecosystem].get("apps")
    if not isinstance(ecosystem_apps, dict):
        sys.exit(f"[FATAL] '{args.ecosystem}' has no valid 'apps' config.")

    app_list = list(ecosystem_apps.keys()) if args.apps.lower() == "all" else args.apps.split(',')
    custom_vers = parse_custom_versions(args.custom_version)

    for app_name in [a.strip() for a in app_list]:
        if app_name in ecosystem_apps:
            c_ver = custom_vers.get(app_name) or custom_vers.get("_global")
            process_single_app(app_name, args, ecosystem_apps[app_name], c_ver, state)

    if state["success"]:
        write_changelog(args, state["success"], workspace, state["clean_ver"])

def parse_arguments():
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
