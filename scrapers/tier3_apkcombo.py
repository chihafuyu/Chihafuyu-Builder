"""Tier 3 Scraper: APKCombo."""

from typing import Optional
from bs4 import BeautifulSoup
import requests

from core.context import Context
from core.utils import _is_waf_blocked, download_file_stream
from .base import BaseScraper


class ApkcomboScraper(BaseScraper):
    """Scrapes APKs from APKCombo."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "apkcombo"

    def _find_page(self, ctx: Context) -> Optional[str]:
        t_ver = ctx.target_ver
        base_ver = t_ver.split("-")[0] if "-" in t_ver and t_ver[:1].isdigit() else t_ver
        app_url = f"https://apkcombo.com/a/{ctx.pkg}/"

        ctx.limiter.wait()
        resp = ctx.scraper.get(app_url, timeout=60)
        if _is_waf_blocked(resp.status_code, resp.text):
            return None
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            if (v_tag := soup.find("span", class_="version")) and base_ver in v_tag.text:
                if btn := soup.find("a", class_="button-download"):
                    return btn.get("href")

        ctx.limiter.wait()
        old_resp = ctx.scraper.get(f"{app_url}old-versions/", timeout=60)
        if (
            _is_waf_blocked(old_resp.status_code, old_resp.text)
            or old_resp.status_code != 200
        ):
            return None
        for link in BeautifulSoup(old_resp.text, "html.parser").find_all(
            "a", href=True
        ):
            href = link["href"]
            if ctx.pkg in href and "/download/" in href:
                if (v_text := link.find(class_="vername")) and base_ver in v_text.text:
                    return href
        return None

    def _find_dl(self, ctx: Context, page_url: str) -> Optional[str]:
        p_url = (
            page_url
            if page_url.startswith("http")
            else f"https://apkcombo.com{page_url}"
        )
        ctx.limiter.wait()
        resp = ctx.scraper.get(p_url, timeout=60)
        if _is_waf_blocked(resp.status_code, resp.text):
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        for link in soup.select("ul.list-download li a"):
            href = link.get("href", "")
            text = link.text.lower()
            if href.endswith((".apk", ".apks")) or "&fp=" in href:
                if (
                    ctx.arch.lower() in text
                    or "universal" in text
                    or "armeabi" in text
                ):
                    return href

        if f_link := soup.select_one("ul.list-download li a"):
            return f_link.get("href")
        return None

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process from APKCombo."""
        print(f"[TIER 3] APKCombo: v{ctx.target_ver}")
        try:
            dl_page_url = self._find_page(ctx)
            if not dl_page_url:
                print("[WARN] Version not found.")
                return None
            if final_dl := self._find_dl(ctx, dl_page_url):
                out_path = ctx.get_out_path(
                    ".apks" if "apks" in final_dl else ".apk"
                )
                print("[INFO] Downloading from APKCombo...")
                if download_file_stream(ctx.scraper, final_dl, out_path):
                    return out_path
        except (requests.exceptions.RequestException, OSError) as err:
            print(f"[ERROR] Tier 3 failed: {err}")
        return None
