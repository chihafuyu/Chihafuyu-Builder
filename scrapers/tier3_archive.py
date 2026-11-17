"""Tier 3 Scraper: Archive.org."""

import os

from bs4 import BeautifulSoup
import requests

from core.context import Context
from .base import BaseScraper
from ._common import probe_and_download


VALID_ARCH_MARKERS = ("universal", "noarch", "all")
VALID_EXTENSIONS = (".apk", ".xapk", ".apkm", ".apks")


def _resolve_download_ext(url: str) -> str:
    """Returns the APK container extension advertised by *url*.

    Query strings and fragments are stripped first so a URL such as
    "app.apk?dl=1" resolves to ".apk" instead of producing the bogus
    extension ".apk?dl=1" and falling back blindly.
    """
    clean_url = url.split("?", 1)[0].split("#", 1)[0]
    ext = os.path.splitext(clean_url)[1].lower()
    return ext if ext in VALID_EXTENSIONS else ".apk"


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

        print(f"[TIER 3] Archive.org: v{ctx.target_ver}")
        ctx.limiter.wait()
        base_url = f"https://archive.org/download/{arch_id}"

        try:
            resp = ctx.scraper.get(f"{base_url}/", timeout=60)
        except requests.exceptions.RequestException as err:
            print(f"[ERROR] Tier 3 request failed: {err}")
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

        out_path = ctx.get_out_path(_resolve_download_ext(dl_link))
        return probe_and_download(
            ctx, dl_link, out_path, "Archive.org", skip_probe=True
        )
