"""Tier 8 Scraper: Direct URL."""

from urllib.parse import quote

import requests

from core.context import Context
from core.utils import download_file_stream
from .base import BaseScraper


class DirectScraper(BaseScraper):
    """Scrapes APKs directly from patterned URLs."""

    @classmethod
    def tier_name(cls) -> str:
        """Returns the tier identifier."""
        return "direct"

    def scrape(self, ctx: Context) -> str | None:
        """Executes the scraping process from a Direct URL."""
        tmpl = ctx.app_data.get("direct_url")
        if not tmpl:
            return None

        print(f"[TIER 8] Direct URL: v{ctx.target_ver}")
        dl_link = (
            tmpl.replace("[VERSI]", quote(ctx.target_ver, safe=""))
            .replace("[ARCH]", quote(ctx.arch, safe=""))
        )
        out_path = ctx.get_out_path(".apk")

        ctx.limiter.wait()
        try:
            res = ctx.scraper.head(dl_link, timeout=10, allow_redirects=True)
        except requests.exceptions.RequestException as err:
            print(f"[WARN] Direct link probe failed: {err}")
            return None

        if res.status_code != 200:
            print(f"[WARN] Direct link returned HTTP {res.status_code}: {dl_link}")
            return None

        print("[INFO] Downloading from Direct URL...")
        if download_file_stream(ctx.scraper, dl_link, out_path):
            return out_path
        return None
