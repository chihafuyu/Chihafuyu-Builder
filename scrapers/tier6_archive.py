"""Tier 6 Scraper: Archive.org."""

import os
from typing import Optional
from bs4 import BeautifulSoup
import requests

from core.context import Context
from core.utils import download_file_stream
from .base import BaseScraper


class ArchiveScraper(BaseScraper):
    """Scrapes APKs from Archive.org."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "archive"

    def _find_link(
        self, ctx: Context, soup: BeautifulSoup, base_url: str
    ) -> Optional[str]:
        valid = [ctx.arch.lower(), "universal", "noarch", "all"]
        link1 = next(
            (
                f"{base_url}/{link.get('href')}"
                for link in soup.find_all("a")
                if ctx.pkg in link.get("href", "")
                and ctx.target_ver in link.get("href", "")
                and (
                    any(
                        v in link.get("href", "").lower() for v in valid
                    )
                    or ctx.arch == "all"
                )
            ),
            None,
        )
        if link1:
            return link1
        return next(
            (
                f"{base_url}/{link.get('href')}"
                for link in soup.find_all("a")
                if ctx.pkg in link.get("href", "")
                and ctx.target_ver in link.get("href", "")
            ),
            None,
        )

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process from Archive.org."""
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

            dl_link = self._find_link(
                ctx, BeautifulSoup(resp.text, "html.parser"), base_url
            )
            if dl_link:
                orig_ext = os.path.splitext(dl_link)[1]
                orig_ext = (
                    orig_ext
                    if orig_ext in [".apk", ".xapk", ".apkm", ".apks"]
                    else ".apk"
                )
                out_path = ctx.get_out_path(orig_ext)
                print("[INFO] Downloading from Archive...")
                if download_file_stream(ctx.scraper, dl_link, out_path):
                    return out_path
            print("[WARN] Not found on Archive.")
        except (requests.exceptions.RequestException, OSError) as err:
            print(f"[ERROR] Tier 6 failed: {err}")
        return None
