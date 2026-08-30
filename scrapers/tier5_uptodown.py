"""Tier 5 Scraper: Uptodown."""

import re
from typing import Any, Optional, Tuple
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
import requests

from core.context import Context
from core.utils import _is_waf_blocked, download_file_stream
from .base import BaseScraper


class UptodownScraper(BaseScraper):
    """Scrapes APKs from Uptodown, handling Bing fallback for DMCA/WAF pages."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "uptodown"

    def _resolve_bing_fallback(self, ctx: Context) -> Optional[str]:
        query = quote_plus(f'site:uptodown.com/android/download "{ctx.pkg}"')
        try:
            ctx.limiter.wait()
            resp = ctx.scraper.get(
                f"https://www.bing.com/search?q={query}", timeout=60
            )
            if resp.status_code == 200:
                matches = re.findall(
                    r"https://[a-z0-9-]+\.en\.uptodown\.com/android/download",
                    resp.text,
                )
                if matches:
                    print("[INFO] Found Uptodown link via Bing.")
                    return matches[0].replace("/download", "")
        except requests.exceptions.RequestException as err:
            print(f"[WARN] Bing fallback failed: {err}")
        return None

    def _fetch_page(
        self, ctx: Context, base_url: str
    ) -> Tuple[Optional[str], Optional[str]]:
        ctx.limiter.wait()
        resp = ctx.scraper.get(base_url, timeout=60)
        if _is_waf_blocked(resp.status_code, resp.text):
            return None, None

        if resp.status_code in (404, 410):
            print("[INFO] Direct URL missed. Attempting Bing fallback...")
            fallback_url = self._resolve_bing_fallback(ctx)
            if not fallback_url:
                return None, None
            ctx.limiter.wait()
            f_resp = ctx.scraper.get(fallback_url, timeout=60)
            if _is_waf_blocked(f_resp.status_code, f_resp.text):
                return None, None
            return f_resp.text, fallback_url
        return resp.text, base_url

    def _find_version(
        self, ctx: Context, base_url: str, html_text: str
    ) -> Tuple[Optional[str], Optional[str], bool]:
        app_elem = BeautifulSoup(html_text, "html.parser").find(
            id="detail-app-name"
        )
        if not app_elem or not app_elem.has_attr("data-code"):
            return None, None, False

        d_code = app_elem["data-code"]
        t_ver = ctx.target_ver
        base_ver = t_ver.split("-")[0] if "-" in t_ver and t_ver[:1].isdigit() else t_ver

        for i in range(1, 21):
            ctx.limiter.wait()
            resp = ctx.scraper.get(
                f"{base_url}/apps/{d_code}/versions/{i}", timeout=60
            )
            if resp.status_code != 200:
                break
            try:
                for v_data in resp.json().get("data", []):
                    if v_data.get("version") in (ctx.target_ver, base_ver):
                        v_obj = v_data.get("versionURL", {})
                        if v_obj.get("url") and v_obj.get("versionID") != "None":
                            v_url = (
                                f"{v_obj['url']}/{v_obj['extraURL']}/"
                                f"{v_obj['versionID']}"
                            )
                            return v_url, d_code, v_data.get("kindFile") == "xapk"
            except ValueError:
                continue
        return None, None, False

    def _resolve_variants(
        self, ctx: Context, soup: BeautifulSoup, d_code: str, base_url: str
    ) -> Any:
        v_btn = soup.find(class_="button variants")
        if v_btn and v_btn.has_attr("data-version"):
            ctx.limiter.wait()
            url = (
                f"https://en.uptodown.com/android/app/{d_code}/"
                f"version/{v_btn['data-version']}/files"
            )
            f_resp = ctx.scraper.get(url, timeout=60)
            if f_resp.status_code == 200:
                f_soup = BeautifulSoup(
                    f_resp.json().get("content", ""), "html.parser"
                )
                sel_id = next(
                    (
                        v.find(class_="v-report")["data-file-id"]
                        for v in f_soup.find_all("div", class_="variant")
                        if (
                            ctx.arch.lower() in v.text.lower()
                            or "universal" in v.text.lower()
                        )
                        and v.find(class_="v-report")
                        and v.find(class_="v-report").has_attr("data-file-id")
                    ),
                    None,
                )
                if not sel_id:
                    rep = f_soup.find(class_="v-report")
                    sel_id = (
                        rep["data-file-id"]
                        if rep and rep.has_attr("data-file-id")
                        else None
                    )

                if sel_id:
                    ctx.limiter.wait()
                    url2 = f"{base_url}/download/{sel_id}-x"
                    d_soup = BeautifulSoup(
                        ctx.scraper.get(url2, timeout=60).text, "html.parser"
                    )
                    return d_soup.find(id="detail-download-button")
        return None

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process from Uptodown."""
        print(f"[TIER 5] Uptodown API: v{ctx.target_ver}")
        search = ctx.app_data.get("search_term", ctx.pkg.replace("-", " "))
        pkg_str = search.lower().replace(" ", "-")
        base_url = (
            ctx.app_data.get("uptodown_url")
            or f"https://{pkg_str}.en.uptodown.com/android"
        )

        try:
            html_text, valid_url = self._fetch_page(ctx, base_url)
            if not html_text or not valid_url:
                return None

            v_url, d_code, is_bundle = self._find_version(
                ctx, valid_url, html_text
            )
            if not v_url:
                print("[WARN] Version not found.")
                return None

            ctx.limiter.wait()
            soup = BeautifulSoup(
                ctx.scraper.get(v_url, timeout=60).text, "html.parser"
            )
            dl_btn = soup.find(id="detail-download-button")
            if not dl_btn:
                dl_btn = self._resolve_variants(ctx, soup, d_code, valid_url)

            if dl_btn and dl_btn.has_attr("data-url"):
                out_path = ctx.get_out_path(".xapk" if is_bundle else ".apk")
                print("[INFO] Downloading from Uptodown...")
                url2 = f"https://dw.uptodown.com/dwn/{dl_btn['data-url']}"
                if download_file_stream(ctx.scraper, url2, out_path, v_url, True):
                    return out_path
        except (requests.exceptions.RequestException, OSError) as err:
            print(f"[ERROR] Tier 5 failed: {err}")
        return None
