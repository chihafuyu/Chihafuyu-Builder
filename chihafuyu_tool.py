"""
Automated APK Downloader and Patcher using the CLI.
Handles multi-tier downloading and dynamic options.json injection.
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.parse
import zipfile
from typing import Dict, Any

try:
    import requests
    import cloudscraper
    from bs4 import BeautifulSoup
except ImportError:
    print("[FATAL] Missing libs. Run: pip install cloudscraper beautifulsoup4 requests")
    sys.exit(1)

def load_config():
    """Loads the ecosystem configuration from the JSON file."""
    if not os.path.exists("ecosystems.json"):
        print("[FATAL] 'ecosystems.json' not found.")
        sys.exit(1)
    with open("ecosystems.json", "r", encoding="utf-8") as config_file:
        try:
            return json.load(config_file)
        except json.JSONDecodeError as err:
            print(f"[FATAL] Invalid JSON: {err}")
            sys.exit(1)
    return {}

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

def download_file_stream(scraper, url, out_path, referer=None, check_dmca=False):
    """Downloads a file using the scraper and saves it in chunks."""
    headers = {"Referer": referer} if referer else None
    try:
        apk_data = scraper.get(url, stream=True, headers=headers, timeout=30)
        if apk_data.status_code == 200:
            if check_dmca:
                content_disp = apk_data.headers.get('Content-Disposition', '').lower()
                if 'uptodown-app-store' in content_disp:
                    print("[ERROR] DMCA Trap detected (Store APK).")
                    return False
            with open(out_path, 'wb') as apk_file:
                for chunk in apk_data.iter_content(chunk_size=8192):
                    apk_file.write(chunk)
            return True
        print(f"[ERROR] Download rejected HTTP {apk_data.status_code}")
    except (requests.exceptions.RequestException, OSError) as req_err:
        print(f"[ERROR] Request failed: {req_err}")
    return False

def _extract_xapk(file_path, zip_obj, namelist):
    """Extracts a pure APK from an XAPK or APKM wrapper."""
    apk_files = [item for item in namelist if item.endswith('.apk')]
    if len(apk_files) == 1 and not file_path.endswith('.apkm'):
        print("[INFO] XAPK Wrapper detected. Extracting APK...")
        new_path = file_path.rsplit('.', 1)[0] + '.apk'
        with zip_obj.open(apk_files[0]) as source, open(new_path, 'wb') as target:
            target.write(source.read())
        os.remove(file_path)
        return new_path
    return file_path

def process_downloaded_file(file_path):
    """Processes downloaded files, handling pure APKs and XAPK wrappers."""
    try:
        if not zipfile.is_zipfile(file_path):
            return file_path
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
    except (zipfile.BadZipFile, OSError) as inspect_error:
        print(f"[WARN] Inspection failed: {inspect_error}")
    return file_path

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
    """Recursively searches for a patch by name and applies overrides."""
    if isinstance(obj, dict):
        if patch_name in obj and isinstance(obj[patch_name], dict):
            _update_patch_options(obj[patch_name], override_data)
            return True
        for val in obj.values():
            if _search_and_update(val, patch_name, override_data):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _search_and_update(item, patch_name, override_data):
                return True
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

        with open(filepath, 'w', encoding='utf-8') as opt_file:
            json.dump(data, opt_file, indent=4)
        print("[INFO] Custom patch options injected successfully.")
    except (OSError, json.JSONDecodeError) as options_err:
        print(f"[WARN] Failed to apply options overrides: {options_err}")
# =========================================================

# TIER 0: ARCHIVE.ORG
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
    """Scrape the APK from Archive.org."""
    arch_id = app_data.get("archive_id")
    if not arch_id:
        return None
    print(f"[TIER 0] Archive.org: v{target_ver}")
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
            out_path = os.path.join(out_dir, f"{pkg}_{target_ver}{orig_ext}")
            if download_file_stream(scraper, dl_link, out_path):
                print(f"[INFO] Tier 0 Success ({orig_ext})")
                return out_path
        else:
            print(f"[WARN] Not found. (Did you name it '{pkg}_{target_ver}.apk'?)")
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 0 failed: {err}")
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
        return "https://www.apkmirror.com" + link['href']
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
                return "https://www.apkmirror.com" + link['href'], False

    for row in soup.find_all('div', class_='table-row'):
        text = row.text.lower()
        if "bundle" in text and any(a in text for a in valid_archs):
            if ver_code and str(ver_code) not in text:
                continue
            link = row.find('a', class_='accent_color')
            if link:
                return "https://www.apkmirror.com" + link['href'], True
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

    dl_page = "https://www.apkmirror.com" + dl_btn['href']

    resp = scraper.get(dl_page, timeout=30)
    if resp.status_code != 200:
        print(f"[WARN] HTTP {resp.status_code} at download page")
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')
    dl_btn = soup.find("a", {"rel": "nofollow"})

    if dl_btn and 'href' in dl_btn.attrs:
        out_path = os.path.join(
            file_meta[2],
            f"{file_meta[0]}_{file_meta[1]}{'.apkm' if is_bundle else '.apk'}"
        )
        print("[INFO] Downloading from APKMirror...")
        if download_file_stream(
            scraper, "https://www.apkmirror.com" + dl_btn['href'], out_path, dl_page
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
            out_path = os.path.join(out_dir, f"{pkg}_{target_ver}{ext}")
            print("[INFO] Downloading from APKCombo...")
            if download_file_stream(scraper, final_dl, out_path):
                print(f"[INFO] Tier 3 Success ({ext})")
                return out_path
        print("[WARN] Extraction failed.")
    except (requests.exceptions.RequestException, OSError) as err:
        print(f"[ERROR] Tier 3 failed: {err}")
    return None

# TIER 4: APTOIDE API
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
            out_path = os.path.join(out_dir, f"{pkg}_{target_ver}.apk")
            print("[INFO] Downloading from Aptoide...")
            if download_file_stream(scraper, dl_url, out_path):
                print("[INFO] Tier 4 Success (.apk)")
                return out_path
        else:
            print("[WARN] Version not found.")
    except (requests.exceptions.RequestException, ValueError, OSError) as err:
        print(f"[ERROR] Tier 4 failed: {err}")
    return None

# TIER 5: UPTODOWN API
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
    out_path = os.path.join(out_dir, f"{pkg}_{target_ver}{ext}")

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

# TIER 6: HUGGING FACE DATASETS
def scrape_huggingface(app_data, target_ver, out_dir):
    """Scrape the APK directly from Hugging Face Datasets as a final fallback."""
    hf_repo = app_data.get("hf_repo", f"chihafuyu/{app_data.get('archive_id')}")
    if not app_data.get("archive_id") and not app_data.get("hf_repo"):
        return None

    print(f"[TIER 6] Hugging Face: v{target_ver}")
    time.sleep(1)
    scraper = get_scraper()
    pkg = app_data["package"]

    base_url = f"https://huggingface.co/datasets/{hf_repo}/resolve/main"

    for ext in ['.apk', '.xapk', '.apkm', '.apks']:
        dl_link = f"{base_url}/{pkg}_{target_ver}{ext}"
        out_path = os.path.join(out_dir, f"{pkg}_{target_ver}{ext}")

        try:
            # Added allow_redirects=True to handle Hugging Face Git LFS 302 redirects
            head_req = scraper.head(dl_link, timeout=10, allow_redirects=True)
            if head_req.status_code == 200:
                print("[INFO] Downloading from Hugging Face Vault...")
                if download_file_stream(scraper, dl_link, out_path):
                    print(f"[INFO] Tier 6 Success ({ext})")
                    return out_path
        except requests.exceptions.RequestException:
            continue

    print(f"[WARN] Not found in Hugging Face dataset '{hf_repo}'.")
    return None

def _download_apkpure(pkg, target_ver, dl_dir):
    """Downloads APK from APKPure via apkeep."""
    print(f"[TIER 2] APKPure: v{target_ver}")
    subprocess.run(
        ["apkeep", "-a", f"{pkg}@{target_ver}", "-d", "apk-pure", dl_dir],
        capture_output=True, text=True, check=False
    )
    files = []
    for ext in ("*.apk", "*.xapk", "*.apkm", "*.apks"):
        files.extend(glob.glob(os.path.join(dl_dir, ext)))
    if files:
        print("[INFO] Tier 2 Success.")
        return files[0]
    print("[WARN] APKPure failed.")
    return None

def download_apk(app_data, target_ver, arch, out_dir, dl_source="default"):
    """Fallback mechanism or targeted download for APK through multiple sources."""
    if target_ver.lower() == "any":
        print("[ERROR] Version defined as 'Any'. Skipping.")
        return None

    pkg = app_data["package"]
    dl_dir = os.path.join(out_dir, pkg)
    os.makedirs(dl_dir, exist_ok=True)

    ver_code = app_data.get("version_codes", {}).get(arch)
    source = dl_source.lower()
    path = None

    if source == "huggingface":
        path = scrape_huggingface(app_data, target_ver, dl_dir)
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
            scrape_archive(app_data, target_ver, arch, dl_dir) or
            scrape_apkmirror(app_data, target_ver, arch, ver_code, dl_dir) or
            _download_apkpure(pkg, target_ver, dl_dir) or
            scrape_apkcombo(app_data, target_ver, arch, dl_dir) or
            scrape_aptoide(app_data, target_ver, dl_dir) or
            scrape_uptodown(app_data, target_ver, arch, dl_dir) or
            scrape_huggingface(app_data, target_ver, dl_dir)
        )

    if path:
        return process_downloaded_file(path)

    print(f"[FATAL] Exhausted sources or specific source failed for {pkg}.")
    return None

def write_changelog(ecosystem, repo_url, patches_version, apps_patched, workspace):
    """Write the patched apps changelog to a markdown file."""
    log_path = os.path.join(workspace, "changelog.md")
    with open(log_path, "w", encoding="utf-8") as file_obj:
        file_obj.write(f"## Automatically Patched Applications ({ecosystem})\n\n")
        file_obj.write(f"Generated using **v{patches_version}** from `{ecosystem}`.\n")
        file_obj.write(f"**Source:** [Repository]({repo_url})\n\n### Apps:\n")
        for app in apps_patched:
            build = f" (Build: {app['build']})" if app.get('build') else ""
            file_obj.write(
                f"- **{app['name']}** (v{app['version']}{build} - `{app['arch']}`)\n"
            )
        file_obj.write("\n---\n### ⚠️ microG Required\n")
        file_obj.write("For Google Apps, install [microG-RE]")
        file_obj.write("(https://github.com/MorpheApp/MicroG-RE/releases/latest).\n")

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

def _generate_options_json(app_name, args, app_data, workspace):
    """Generates options JSON file using the CLI."""
    json_file = os.path.join(workspace, f"{app_name}-options.json")
    cmd_opts = [
        "java", "-jar", args.cli, "options-create", "--patches", args.patches,
        "--out", json_file, "--filter-package-name", app_data["package"]
    ]
    subprocess.run(cmd_opts, capture_output=True, check=False)
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
        app_data, target_ver, arch, state["in_dir"], args.download_source
    )

    if not apk_path:
        return

    json_file = _generate_options_json(app_name, args, app_data, state["workspace"])
    apk_name = (
        f"{app_name}_{args.ecosystem}_patched_{target_ver}-"
        f"{arch}_patches_{state['clean_ver']}.apk"
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
    workspace = f"./{args.ecosystem}"
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
    if args.ecosystem not in ECOSYSTEMS:
        print(f"[FATAL] Ecosystem '{args.ecosystem}' not found in JSON.")
        sys.exit(1)

    ecosystem_apps = ECOSYSTEMS[args.ecosystem]["apps"]
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
        write_changelog(
            args.ecosystem, args.repo_url, state["clean_ver"],
            state["success"], workspace
        )

def parse_arguments():
    """Parses command line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--ecosystem", required=True)
    parser.add_argument("--apps", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--version-selection", required=True)
    parser.add_argument("--custom-version", default="")
    parser.add_argument("--download-source", default="default")
    parser.add_argument("--continue-on-error", default="false")
    parser.add_argument("--cli", required=True)
    parser.add_argument("--patches", required=True)
    parser.add_argument("--patches-version", required=True)
    parser.add_argument("--repo-url", required=True)
    parser.add_argument("--keystore")
    parser.add_argument("--ks-alias")
    parser.add_argument("--ks-pass")
    parser.add_argument("--signer")
    return parser.parse_args()

if __name__ == "__main__":
    run_patcher(parse_arguments())
