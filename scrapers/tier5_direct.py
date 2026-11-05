"""Tier 8 Scraper: Direct URL."""

from urllib.parse import quote

from core.context import Context
from .base import BaseScraper
from ._common import probe_and_download


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

        return probe_and_download(ctx, dl_link, out_path, "Direct URL")
