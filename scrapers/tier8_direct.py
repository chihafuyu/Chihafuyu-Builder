"""Tier 8 Scraper: Direct URL."""

from typing import Optional
import requests

from core.context import Context
from core.utils import download_file_stream
from .base import BaseScraper


class DirectScraper(BaseScraper):
    """Scrapes APKs directly from patterned URLs."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "direct"

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process from a Direct URL."""
        tmpl = ctx.app_data.get("direct_url")
        if not tmpl:
            return None

        print(f"[TIER 8] Direct URL: v{ctx.target_ver}")
        dl_link = (
            tmpl.replace("[VERSI]", ctx.target_ver)
            .replace("[ARCH]", ctx.arch)
        )
        out_path = ctx.get_out_path(".apk")

        ctx.limiter.wait()
        try:
            res = ctx.scraper.head(dl_link, timeout=10, allow_redirects=True)
            if res.status_code == 200:
                print("[INFO] Downloading from Direct URL...")
                if download_file_stream(ctx.scraper, dl_link, out_path):
                    return out_path
        except requests.exceptions.RequestException:
            pass

        print(f"[WARN] Direct link not reachable: {dl_link}")
        return None
