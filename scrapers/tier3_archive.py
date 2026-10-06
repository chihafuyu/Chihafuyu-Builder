"""Tier 6 Scraper: Archive.org."""

import os

from bs4 import BeautifulSoup
import requests

from core.context import Context
from core.utils import download_file_stream
from .base import BaseScraper


VALID_ARCH_MARKERS = ("universal", "noarch", "all")


class ArchiveScraper(BaseScraper):
    """Scrapes APKs from Archive.org."""

    @classmethod
    def tier_name(cls) -> str:
        """Returns the tier identifier."""
        return "archive"

    def _find_link(
        self, ctx: Context, soup: BeautifulSoup, base_url: str
    ) -> str | None:
        """Returns the best-matching APK URL, preferring arch-specific files."""
        arch_lower = ctx.arch.lower()
        valid = [arch_lower, *VALID_ARCH_MARKERS]

        fallback: str | None = None
        for link in soup.find_all("a"):
            href = link.get("href", "")
            if ctx.pkg not in href or ctx.target_ver not in href:
                continue
            full = f"{base_url}/{href}"
            if fallback is None:
                fallback = full
            if arch_lower == "all" or any(v in href.lower() for v in valid):
                return full
        return fallback

    def scrape(self, ctx: Context) -> str | None:
        """Executes the scraping process from Archive.org."""
        arch_id = ctx.app_data.get("archive_id")
        if not arch_id:
            return None

        print(f"[TIER 6] Archive.org: v{ctx.target_ver}")
        ctx.limiter.wait()
        base_url = f"https://archive.org/download/{arch_id}"

        try:
            resp = ctx.scraper.get(f"{base_url}/", timeout=60)
        except requests.exceptions.RequestException as err:
            print(f"[ERROR] Tier 6 request failed: {err}")
            return None

        if resp.status_code != 200:
            print(f"[WARN] Archive index returned HTTP {resp.status_code}.")
            return None

        dl_link = self._find_link(
            ctx, BeautifulSoup(resp.text, "html.parser"), base_url
        )
        if not dl_link:
            print("[WARN] Not found on Archive.")
            return None

        orig_ext = os.path.splitext(dl_link)[1]
        if orig_ext not in (".apk", ".xapk", ".apkm", ".apks"):
            orig_ext = ".apk"

        out_path = ctx.get_out_path(orig_ext)
        print("[INFO] Downloading from Archive...")
        if download_file_stream(ctx.scraper, dl_link, out_path):
            return out_path
        return None
