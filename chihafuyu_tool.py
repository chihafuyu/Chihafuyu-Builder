"""
Automated APK Downloader and Patcher using the CLI.
Handles multi-tier downloading and dynamic options.json injection.
"""

import argparse
import shutil
import tempfile
import glob
import json
import os
import subprocess
import sys
import time
import urllib.parse
import zipfile
from typing import Dict, Any
from urllib.parse import urljoin, urlparse

try:
    import requests
    import cloudscraper
    from bs4 import BeautifulSoup
except ImportError:
    print("[FATAL] Missing libs. Run: pip install cloudscraper beautifulsoup4 requests")
    sys.exit(1)

def load_config():
    """Loads the ecosystem configuration from the JSON file."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecosystems.json")
    if not os.path.isfile(config_path):
        print(f"[FATAL] '{config_path}' not found.")
        sys.exit(1)
    try:
        with open(config_path, "r", encoding="utf-8") as config_file:
            data = json.load(config_file)
    except (OSError, json.JSONDecodeError) as err:
        print(f"[FATAL] Failed to load configuration: {err}")
        sys.exit(1)
    if not isinstance(data, dict):
        print("[FATAL] ecosystems.json must contain a JSON object.")
        sys.exit(1)
    return data

ECOSYSTEMS: Dict[str, Dict[str, Any]] = load_config()

def get_scraper():
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

MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024

def _validate_http_url(url):
    """Accept only HTTP(S) URLs before handing them to requests."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Unsupported download URL: {url!r}")

def _safe_filename(name, fallback="artifact"):
    """Returns a filesystem-safe single path component."""
    cleaned = os.path.basename(str(name)).strip()
    if not cleaned or cleaned in {".", ".."} or any(ch in cleaned for ch in ('/', '\\', '\x00')):
        return fallback
    return cleaned

def download_file_stream(scraper, url, out_path, referer=None, check_dmca=False):
    """Downloads a file safely with streaming, size limits, and atomic replacement."""
    try:
        _validate_http_url(url)
        headers = {"Referer": referer} if referer else None
        with scraper.get(
            url, stream=True, headers=headers, timeout=(10, 60), allow_redirects=True
        ) as response:
            if response.status_code != 200:
                print(f"[ERROR] Download rejected HTTP {response.status_code}")
                return False
            if check_dmca:
                content_disp = response.headers.get('Content-Disposition', '').lower()
                if 'uptodown-app-store' in content_disp:
                    print("[ERROR] DMCA Trap detected (Store APK).")
                    return False

            declared_size = response.headers.get("Content-Length")
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
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError(f"download exceeds {MAX_DOWNLOAD_BYTES} byte limit")
                        apk_file.write(chunk)
                os.replace(temp_path, out_path)
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            return True
    except (requests.exceptions.RequestException, OSError, ValueError) as req_err:
        print(f"[ERROR] Request failed: {req_err}")
    return False

def _extract_xapk(file_path, zip_obj, namelist):
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

def process_downloaded_file(file_path):
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
    except (zipfile.BadZipFile, OSError, ValueError) as inspect_error:
        print(f"[WARN] Inspection failed: {inspect_error}")
    return None

# ================= OPTIONS.JSON INJECTOR =================
def _update_patch_options(target_dict, override_data):
    """Updates the enabled status and options for a specific patch."""
    if "enabled" in override_data:
        target_dict["enabled"] = override_data["enabled"]
    if "options" in override_data:
        if "options" not in target_dict or not isinstance(target_dict["options"], dict):
            target_dict["options"] = {}
        for key, val in override_data["options"].items():
            target_dict["options"][key] = val

def _search_and_update(obj, patch_name, override_data):
    """Recursively updates the first matching patch and reports whether it was found."""
    if isinstance(obj, dict):
        if patch_name in obj and isinstance(obj[patch_name], dict):
            _update_patch_options(obj[patch_name], override_data)
            return True
        return any(_search_and_update(val, patch_name, override_data) for val in obj.values())
    if isinstance(obj, list):
        return any(_search_and_update(item, patch_name, override_data) for item in obj)
    return False

def update_options_json(filepath, overrides):
    """Injects custom options into the CLI-generated JSON file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as opt_file:
            data = json.load(opt_file)

        for patch_name, override_data in overrides.items():
            success = _search_and_update(data, patch_name, override_data)
            if not success:
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
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as options_err:
        print(f"[WARN] Failed to apply options overrides: {options_err}")

# =========================================================

# TIER 0: HUGGINGFACE DATASETS
def scrape_huggingface(app_data, target_ver, out_dir, hf_user):
    """Scrape the APK directly from HuggingFace Datasets as the primary source."""
    hf_repo = app_data.get("hf_repo", f"{hf_user}/{app_data.get('archive_id')}")
    if not app_data.get("archive_id") and not app_data.get("hf_repo"):
        return None

    print(f"[TIER 0] HuggingFace: v{target_ver}")
    time.sleep(1)
    scraper = get_scraper()
    pkg = app_data["package"]

    base_url = f"https://huggingface.co/datasets/{hf_repo}/resolve/main"

    for ext in ['.apk', '.xapk', '.apkm', '.apks']:
        dl_link = f"{base_url}/{pkg}_{target_ver}{ext}"
        out_path = os.path.join(
            out_dir, f"{_safe_filename(pkg)}_{_safe_filename(target_ver)}{ext}"
        )

        try:
            head_req = scraper.head(dl_link, timeout=10, allow_redirects=True)
            if head_req.status_code == 200:
                print("[INFO] Downloading from HuggingFace Vault...")
                if download_file_stream(scraper, dl_link, out_path):
                    print(f"[INFO] Tier 0 Success ({ext})")
                    return out_path
        except requests.exceptions.RequestException:
            continue

    print(f"[WARN] Not found in HuggingFace dataset '{hf_repo}'.")
    return None

# TIER 1: APKMIRROR
def _find_apkmirror_release(scraper, app_data, version):
    """Finds the release page URL on APKMirror."""
    pkg = app_data["package"]
    query = urllib.parse.quote_plus(f"{pkg} {version}")
    url = f"https://www.apkmirror.com/?post_type=app_release&s={query}"
    resp = scraper.get(url, timeout=30)
    if resp.status_code != 200:
        print(f"[WARN] HTTP {resp.status_code} at search page")
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')
    exclude_kws = ["secondary"] + [k.lower() for k in app_data.get("apkm_exclude", [])]
    include_kws = [k.lower() for k in app_data.get("apkm_include", [])]

    for link in soup.find_all('a', class_='fontBlack'):
        link_text = link.text.lower()
        if version.lower() not in link_text:
            continue
        if any(kw in link_text for kw in exclude_kws):
            continue
        if include_kws and not all(kw in link_text for kw in include_kws):
            continue
        return urljoin("https://www.apkmirror.com", link['href'])
    return None

def _find_apkmirror_variant(scraper, release_url, arch, ver_code):
    """Finds the specific variant download page."""
    resp = scraper.get(release_url, timeout=30)
    if resp.status_code != 200:
        print(f"[WARN] HTTP {resp.status_code} at release page")
        return None, False
    soup = BeautifulSoup(resp.text, 'html.parser')
    valid_archs = [arch.lower(), "universal", "noarch"]

    for row in soup.find_all('div', class_='table-row'):
        text = row.text.lower()
        if "apk" in text and "bundle" not in text and any(a in text for a in valid_archs):
            if ver_code and str(ver_code) not in text:
                continue
            link = row.find('a', class_='accent_color')
            if link:
                return urljoin("https://www.apkmirror.com", link['href']), False

    for row in soup.find_all('div', class_='table-row'):
        text = row.text.lower()
        if "bundle" in text and any(a in text for a in valid_archs):
            if ver_code and str(ver_code) not in text:
                continue
            link = row.find('a', class_='accent_color')
            if link:
                return urljoin("https://www.apkmirror.com", link['href']), True
    return None, False

def _download_apkmirror_variant(scraper, var_url, is_bundle, file_meta):
    """Downloads the exact variant from APKMirror."""
    resp = scraper.get(var_url, timeout=30)
    if resp.status_code != 200:
        print(f"[WARN] HTTP {resp.status_code} at variant page")
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')
    dl_btn = soup.find('a', class_='downloadButton')

    if not dl_btn:
        print("[WARN] Download button not found on variant page.")
        return None

    dl_page = urljoin("https://www.apkmirror.com", dl_btn['href'])

    resp = scraper.get(dl_page, timeout=30)
    if resp.status_code != 200:
        print(f"[WARN] HTTP {resp.status_code} at download page")
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')
    dl_btn = soup.find("a", {"rel": "nofollow"})

    if dl_btn and 'href' in dl_btn.attrs:
        out_path = os.path.join(
            file_meta[2],
            f"{_safe_filename(file_meta[0])}_{_safe_filename(file_meta[1])}"
            f"{'.apkm' if is_bundle else '.apk'}"
        )
        print("[INFO] Downloading from APKMirror...")
        if download_file_stream(
            scraper, urljoin("https://www.apkmirror.com", dl_btn['href']), out_path, dl_page
        ):
            print(f"[INFO] Tier 1 Success ({'.apkm' if is_bundle else '.apk'})")
            return out_path
    else:
        print("[WARN] Direct download link missing.")

    return None

def scrape_apkmirror(app_data, target_ver, arch, ver_code, out_dir):
    """Scrape the APK from APKMirror."""
    print(f"[TIER 1] APKMirror: v{target_ver}")
    time.sleep(3)
    scraper = get_scraper()
    pkg = app_data["package"]
    try:
        rel_url = _find_apkmirror_release(scraper, app_data, target_ver)
        if not rel_url:
            print("[WARN] Release not found.")
            return None

        var_url, is_bundle = _find_apkmirror_variant(scraper, rel_url, arch, ver_code)
        if not var_url:
            print("[WARN] Variant missing.")
            return None

        return _download_apkmirror_variant(
            scraper, var_url, is_bundle, (pkg, target_ver, out_dir)
        )
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 1 failed: {err}")
    return None

# TIER 2: APKPURE
def _download_apkpure(pkg, target_ver, dl_dir):
    """Downloads APK from APKPure via apkeep, isolated from stale artifacts."""
    print(f"[TIER 2] APKPure: v{target_ver}")
    os.makedirs(dl_dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="apkeep-") as temp_dir:
        try:
            result = subprocess.run(
                ["apkeep", "-a", f"{pkg}@{target_ver}", "-d", "apk-pure", temp_dir],
                capture_output=True, text=True, check=False
            )
        except OSError as err:
            print(f"[WARN] Failed to execute apkeep: {err}")
            return None
        if result.returncode != 0:
            print(f"[WARN] APKPure failed (apkeep exit code {result.returncode}).")
            return None
        files = []
        for ext in ("*.apk", "*.xapk", "*.apkm", "*.apks"):
            files.extend(glob.glob(os.path.join(temp_dir, ext)))
        if not files:
            print("[WARN] APKPure returned no package artifact.")
            return None
        source = files[0]
        destination = os.path.join(dl_dir, _safe_filename(os.path.basename(source)))
        shutil.copy2(source, destination)
        print("[INFO] Tier 2 Success.")
        return destination

# TIER 3: APKCOMBO
def _find_apkcombo_page(scraper, pkg, version):
    """Find the APKCombo download page."""
    app_url = f"https://apkcombo.com/a/{pkg}/"
    resp = scraper.get(app_url, timeout=30)
    if resp.status_code != 200:
        print(f"[ERROR] HTTP {resp.status_code}")
        return None
    soup = BeautifulSoup(resp.text, 'html.parser')
    ver_tag = soup.find('span', class_='version')
    if ver_tag and version in ver_tag.text:
        btn = soup.find('a', class_='button-download')
        if btn:
            return btn.get('href')

    v_soup = BeautifulSoup(scraper.get(f"{app_url}old-versions/", timeout=30).text, 'html.parser')
    for link in v_soup.find_all('a', href=True):
        if pkg in link['href'] and '/download/' in link['href']:
            ver_text = link.find(class_='vername')
            if ver_text and version in ver_text.text:
                return link['href']
    return None

def _find_apkcombo_dl(scraper, page_url, arch):
    """Extracts the exact download link from APKCombo."""
    if not page_url.startswith('http'):
        page_url = "https://apkcombo.com" + page_url
    soup = BeautifulSoup(scraper.get(page_url, timeout=30).text, 'html.parser')
    for link in soup.select('ul.list-download li a'):
        href, text = link.get('href', ''), link.text.lower()
        if href.endswith(('.apk', '.apks')) or '&fp=' in href:
            if arch.lower() in text or 'universal' in text or 'armeabi' in text:
                return href
    first_link = soup.select_one('ul.list-download li a')
    return first_link.get('href') if first_link else None

def scrape_apkcombo(app_data, target_ver, arch, out_dir):
    """Scrape the APK from APKCombo."""
    print(f"[TIER 3] APKCombo: v{target_ver}")
    time.sleep(3)
    scraper = get_scraper()
    pkg = app_data["package"]
    try:
        dl_page_url = _find_apkcombo_page(scraper, pkg, target_ver)
        if not dl_page_url:
            print("[WARN] Version not found.")
            return None

        final_dl = _find_apkcombo_dl(scraper, dl_page_url, arch)
        if final_dl:
            ext = ".apks" if "apks" in final_dl else ".apk"
            out_path = os.path.join(
                out_dir, f"{_safe_filename(pkg)}_{_safe_filename(target_ver)}{ext}"
            )
            print("[INFO] Downloading from APKCombo...")
            if download_file_stream(scraper, final_dl, out_path):
                print(f"[INFO] Tier 3 Success ({ext})")
                return out_path
        print("[WARN] Extraction failed.")
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 3 failed: {err}")
    return None

# TIER 4: APTOIDE
def scrape_aptoide(app_data, target_ver, out_dir):
    """Scrape the APK from Aptoide."""
    print(f"[TIER 4] Aptoide API: v{target_ver}")
    time.sleep(2)
    scraper = get_scraper()
    pkg = app_data["package"]
    try:
        api_url = f"https://ws75.aptoide.com/api/7/apps/search/query={pkg}/limit=10"
        resp = scraper.get(api_url, timeout=30)
        if resp.status_code != 200:
            print(f"[ERROR] Aptoide API failed (HTTP {resp.status_code}).")
            return None
        try:
            data = resp.json()
        except ValueError:
            print("[WARN] Blocked by Cloudflare.")
            return None

        dl_url = None
        for app in data.get("datalist", {}).get("list", []):
            if app.get("package") == pkg and app.get("file", {}).get("vername") == target_ver:
                dl_url = app.get("file", {}).get("path")
                break

        if dl_url:
            out_path = os.path.join(
                out_dir, f"{_safe_filename(pkg)}_{_safe_filename(target_ver)}.apk"
            )
            print("[INFO] Downloading from Aptoide...")
            if download_file_stream(scraper, dl_url, out_path):
                print("[INFO] Tier 4 Success (.apk)")
                return out_path
        else:
            print("[WARN] Version not found.")
    except (requests.exceptions.RequestException, ValueError, OSError) as err:
        print(f"[ERROR] Tier 4 failed: {err}")
    return None

# TIER 5: UPTODOWN
def _find_uptodown_version(scraper, base_url, version):
    """Finds the version URL on Uptodown."""
    soup = BeautifulSoup(scraper.get(base_url, timeout=30).text, 'html.parser')
    app_elem = soup.find(id="detail-app-name")
    if not app_elem or not app_elem.has_attr("data-code"):
        return None, None, False

    data_code = app_elem["data-code"]
    is_bundle = False
    version_url = None

    for i in range(1, 21):
        api_resp = scraper.get(f"{base_url}/apps/{data_code}/versions/{i}", timeout=30)
        if api_resp.status_code != 200:
            break
        try:
            for v_data in api_resp.json().get("data", []):
                if v_data.get("version") == version:
                    if v_data.get("kindFile") == "xapk":
                        is_bundle = True
                    v_url_obj = v_data.get("versionURL", {})
                    if v_url_obj.get("url") and v_url_obj.get("versionID") != "None":
                        url_part = v_url_obj['url']
                        extra = v_url_obj['extraURL']
                        vid = v_url_obj['versionID']
                        version_url = f"{url_part}/{extra}/{vid}"
                    break
            if version_url:
                break
        except ValueError:
            continue
    return version_url, data_code, is_bundle

def _extract_uptodown_file_id(f_soup, arch):
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

def _get_uptodown_dl_btn(scraper, base_url, version_url, data_code, arch):
    """Extracts the final download button element."""
    soup = BeautifulSoup(scraper.get(version_url, timeout=30).text, 'html.parser')
    dl_btn = soup.find(id="detail-download-button")
    if dl_btn:
        return dl_btn

    v_btn = soup.find(class_="button variants")
    if v_btn and v_btn.has_attr("data-version"):
        v_url = (
            f"https://en.uptodown.com/android/app/{data_code}"
            f"/version/{v_btn['data-version']}/files"
        )
        files_resp = scraper.get(v_url, timeout=30)
        if files_resp.status_code == 200:
            f_soup = BeautifulSoup(files_resp.json().get("content", ""), 'html.parser')
            sel_id = _extract_uptodown_file_id(f_soup, arch)
            if sel_id:
                d_url = f"{base_url}/download/{sel_id}-x"
                d_soup = BeautifulSoup(scraper.get(d_url, timeout=30).text, 'html.parser')
                return d_soup.find(id="detail-download-button")
    return None

def _download_uptodown_variant(scraper, dl_info, file_meta):
    """Downloads the exact variant from Uptodown."""
    dl_btn, v_url, is_bundle = dl_info
    pkg, target_ver, out_dir = file_meta
    final_dl = f"https://dw.uptodown.com/dwn/{dl_btn['data-url']}"
    print("[INFO] Downloading from Uptodown...")
    ext = ".xapk" if is_bundle else ".apk"
    out_path = os.path.join(
        out_dir, f"{_safe_filename(pkg)}_{_safe_filename(target_ver)}{ext}"
    )

    if download_file_stream(scraper, final_dl, out_path, v_url, True):
        print(f"[INFO] Tier 5 Success ({ext})")
        return out_path
    return None

def scrape_uptodown(app_data, target_ver, arch, out_dir):
    """Scrape the APK from Uptodown."""
    print(f"[TIER 5] Uptodown API: v{target_ver}")
    time.sleep(3)
    scraper = get_scraper()
    pkg = app_data["package"]
    search = app_data.get("search_term", pkg.replace('-', ' '))
    up_url = app_data.get("uptodown_url")
    base_url = up_url or f"https://{search.lower().replace(' ', '-')}.en.uptodown.com/android"

    try:
        if scraper.get(base_url, timeout=30).status_code == 410:
            print("[ERROR] HTTP 410 (Gone). Region-blocked or DMCA.")
            return None
        v_url, d_code, is_bundle = _find_uptodown_version(scraper, base_url, target_ver)
        if v_url:
            dl_btn = _get_uptodown_dl_btn(scraper, base_url, v_url, d_code, arch)
            if dl_btn and dl_btn.has_attr("data-url"):
                return _download_uptodown_variant(
                    scraper, (dl_btn, v_url, is_bundle), (pkg, target_ver, out_dir)
                )
        print("[WARN] Version not found.")
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 5 failed: {err}")
    return None

# TIER 6: ARCHIVE.ORG
def _get_archive_link(soup, pkg, target_ver, arch, base_url):
    """Parses Archive.org soup to find the exact APK link."""
    valid_archs = [arch.lower(), "universal", "noarch", "all"]
    for link in soup.find_all('a'):
        href = link.get('href', '')
        if pkg in href and target_ver in href:
            if any(a in href.lower() for a in valid_archs) or arch == "all":
                return f"{base_url}/{href}"
    for link in soup.find_all('a'):
        href = link.get('href', '')
        if pkg in href and target_ver in href:
            print(f"[WARN] Arch mismatch fallback: {href}")
            return f"{base_url}/{href}"
    return None

def scrape_archive(app_data, target_ver, arch, out_dir):
    """Scrape the APK from Archive.org as a final fallback."""
    arch_id = app_data.get("archive_id")
    if not arch_id:
        return None
    print(f"[TIER 6] Archive.org: v{target_ver}")
    time.sleep(2)
    scraper = get_scraper()
    pkg = app_data["package"]
    try:
        base_url = f"https://archive.org/download/{arch_id}"
        resp = scraper.get(f"{base_url}/", timeout=30)
        if resp.status_code != 200:
            print(f"[ERROR] Archive.org HTTP {resp.status_code}")
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')
        dl_link = _get_archive_link(soup, pkg, target_ver, arch, base_url)

        if dl_link:
            print("[INFO] Downloading from Archive...")
            orig_ext = os.path.splitext(dl_link)[1]
            if orig_ext not in ['.apk', '.xapk', '.apkm', '.apks']:
                orig_ext = '.apk'
            out_path = os.path.join(
                out_dir, f"{_safe_filename(pkg)}_{_safe_filename(target_ver)}{orig_ext}"
            )
            if download_file_stream(scraper, dl_link, out_path):
                print(f"[INFO] Tier 6 Success ({orig_ext})")
                return out_path
        else:
            print(f"[WARN] Not found. (Did you name it '{pkg}_{target_ver}.apk'?)")
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 6 failed: {err}")
    return None

def download_apk(app_data, target_ver, arch, out_dir, args):
    """Fallback mechanism or targeted download for APK through multiple sources."""
    if target_ver.lower() == "any":
        print("[ERROR] Version defined as 'Any'. Skipping.")
        return None

    pkg = app_data["package"]
    dl_dir = os.path.join(out_dir, pkg)
    os.makedirs(dl_dir, exist_ok=True)

    ver_code = app_data.get("version_codes", {}).get(arch)
    source = args.download_source.lower()
    path = None

    if source == "huggingface":
        path = scrape_huggingface(app_data, target_ver, dl_dir, args.hf_user)
    elif source == "archive":
        path = scrape_archive(app_data, target_ver, arch, dl_dir)
    elif source == "apkmirror":
        path = scrape_apkmirror(app_data, target_ver, arch, ver_code, dl_dir)
    elif source == "apkpure":
        path = _download_apkpure(pkg, target_ver, dl_dir)
    elif source == "apkcombo":
        path = scrape_apkcombo(app_data, target_ver, arch, dl_dir)
    elif source == "aptoide":
        path = scrape_aptoide(app_data, target_ver, dl_dir)
    elif source == "uptodown":
        path = scrape_uptodown(app_data, target_ver, arch, dl_dir)
    else:
        path = (
            scrape_huggingface(app_data, target_ver, dl_dir, args.hf_user) or
            scrape_apkmirror(app_data, target_ver, arch, ver_code, dl_dir) or
            _download_apkpure(pkg, target_ver, dl_dir) or
            scrape_apkcombo(app_data, target_ver, arch, dl_dir) or
            scrape_aptoide(app_data, target_ver, dl_dir) or
            scrape_uptodown(app_data, target_ver, arch, dl_dir) or
            scrape_archive(app_data, target_ver, arch, dl_dir)
        )

    if path:
        return process_downloaded_file(path)

    print(f"[FATAL] Exhausted sources or specific source failed for {pkg}.")
    return None

def write_changelog(args, apps_patched, workspace, clean_ver):
    """Write the patched apps changelog to a markdown file."""
    log_path = os.path.join(workspace, "changelog.md")
    with open(log_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(f"## Automatically Patched Applications ({args.ecosystem})\n\n")

        # Insert warning if pre-release patches or CLI are utilized
        if args.is_prerelease.lower() == "true":
            file_obj.write("> [!WARNING]\n")
            file_obj.write("> **This application was patched using a pre-release CLI "
                           "and/or patches for experimental purposes. Use with caution.**\n\n")

        file_obj.write(f"Generated using **v{clean_ver}** from `{args.ecosystem}`.\n")
        file_obj.write(f"**Source:** [Repository]({args.repo_url})\n\n### Apps:\n")
        for app in apps_patched:
            build = f" (Build: {app['build']})" if app.get('build') else ""
            file_obj.write(
                f"- **{app['name']}** (v{app['version']}{build} - `{app['arch']}`)\n"
            )
        file_obj.write("\n---\n### ⚠️ microG Required\n")
        file_obj.write("For Google Apps, install [microG-RE]")
        file_obj.write(f"({args.microg_url}).\n")

def parse_custom_versions(custom_version_str):
    """Parses the custom version string into a dictionary."""
    parsed_versions = {}
    if not custom_version_str:
        return parsed_versions

    if "=" in custom_version_str:
        parts = custom_version_str.split(',')
        for part in parts:
            if "=" in part:
                key, val = part.split('=', 1)
                parsed_versions[key.strip()] = val.strip()
    else:
        parsed_versions["_global"] = custom_version_str.strip()

    return parsed_versions

def resolve_target_version(app_data, version_selection, app_custom_version):
    """Resolves the target version to download based on user input."""
    if version_selection.lower() == "custom":
        if app_custom_version:
            return app_custom_version
        print("[WARN] Custom version missing for this app. Falling back to stable.")
    if version_selection.lower() in ["beta", "pre-release", "latest", "experimental"]:
        if "beta" in app_data and app_data["beta"]:
            return app_data["beta"][0]
        print("[WARN] No beta version defined. Falling back to stable.")
    return app_data["stable"][0]

def build_patch_command(args, app_data, files, target_arch):
    """Builds the shell command for the CLI."""
    cmd = [
        "java", "-Xmx4G", "-jar", args.cli, "patch", "--patches", args.patches,
        "--options-file", files[1], "--out", files[2], "--bytecode-mode", "FULL"
    ]
    ver_sel = args.version_selection.lower()
    if ver_sel in ("beta", "pre-release", "latest", "experimental") or ver_sel == "custom":
        cmd.append("--force")
    if app_data.get("strip"):
        cmd.extend(["--striplibs", target_arch])
    if args.continue_on_error.lower() == "true":
        cmd.append("--continue-on-error")
    if args.keystore and args.ks_alias and args.ks_pass:
        cmd.extend([
            "--keystore", args.keystore,
            "--keystore-entry-alias", args.ks_alias,
            "--keystore-password", args.ks_pass,
            "--keystore-entry-password", args.ks_pass
        ])
        if args.signer:
            cmd.extend(["--signer", args.signer])
    cmd.append(files[0])
    return cmd

def execute_patch_cli(patch_cmd):
    """Executes the patch command and streams output."""
    zero_patches = False
    try:
        with subprocess.Popen(
            patch_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        ) as proc:
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

def _generate_options_json(app_name, args, app_data, workspace):
    """Generates options JSON file using the CLI."""
    json_file = os.path.join(workspace, f"{_safe_filename(app_name)}-options.json")
    cmd_opts = [
        "java", "-jar", args.cli, "options-create", "--patches", args.patches,
        "--out", json_file, "--filter-package-name", app_data["package"]
    ]
    result = subprocess.run(cmd_opts, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        print(f"[WARN] CLI options-create failed (exit {result.returncode}): {stderr[-500:]}")
    if app_data.get("options_override") and os.path.exists(json_file):
        print(f"[INFO] Injecting custom patch options for {app_name}...")
        update_options_json(json_file, app_data["options_override"])
    return json_file

def process_single_app(app_name, args, app_data, app_custom_version, state):
    """Processes a single app for downloading and patching."""
    target_ver = resolve_target_version(app_data, args.version_selection, app_custom_version)
    arch = app_data.get("force_arch", args.arch)

    print(f"\n--- {app_name} ({app_data['package']}) ---")

    apk_path = download_apk(
        app_data, target_ver, arch, state["in_dir"], args
    )

    if not apk_path:
        return

    json_file = _generate_options_json(app_name, args, app_data, state["workspace"])
    apk_name = (
        f"{_safe_filename(app_name)}_{_safe_filename(args.ecosystem)}_patched_"
        f"{_safe_filename(target_ver)}-{_safe_filename(arch)}_patches_"
        f"{_safe_filename(state['clean_ver'])}.apk"
    )
    out_apk = os.path.join(state["out_dir"], apk_name)

    print("[INFO] Patching via CLI...")
    ret_code, zero_patches = execute_patch_cli(
        build_patch_command(args, app_data, (apk_path, json_file, out_apk), arch)
    )

    if ret_code == 0 and not zero_patches:
        print(f"\n[INFO] SUCCESS: {app_name}")
        state["success"].append({
            "name": app_name,
            "version": target_ver,
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
        print(f"[FATAL] Ecosystem '{args.ecosystem}' not found in JSON.")
        sys.exit(1)

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
        print(f"[FATAL] Ecosystem '{args.ecosystem}' has no valid 'apps' configuration.")
        sys.exit(1)
    app_list = list(ecosystem_apps.keys()) if args.apps.lower() == "all" else args.apps.split(',')

    custom_versions_dict = parse_custom_versions(args.custom_version)

    for app_name in app_list:
        clean_name = app_name.strip()
        if clean_name in ecosystem_apps:
            app_custom_ver = (
                custom_versions_dict.get(clean_name) or
                custom_versions_dict.get("_global")
            )
            process_single_app(
                clean_name, args, ecosystem_apps[clean_name],
                app_custom_ver, state
            )

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
    parser.add_argument(
        "--microg-url",
        default="https://github.com/MorpheApp/MicroG-RE/releases/latest"
    )
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
