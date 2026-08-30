"""Tier 4 Scraper: Aptoide."""

from typing import Optional
import requests

from core.context import Context
from core.utils import _is_waf_blocked, download_file_stream
from .base import BaseScraper


class AptoideScraper(BaseScraper):
    """Scrapes APK metadata via Aptoide API."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "aptoide"

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process via Aptoide API."""
        print(f"[TIER 4] Aptoide API: v{ctx.target_ver}")
        t_ver = ctx.target_ver
        base_ver = t_ver.split("-")[0] if "-" in t_ver and t_ver[:1].isdigit() else t_ver
        try:
            ctx.limiter.wait()
            req_url = (
                f"https://ws75.aptoide.com/api/7/apps/search/query={ctx.pkg}/limit=10"
            )
            resp = ctx.scraper.get(req_url, timeout=60)
            if (
                _is_waf_blocked(resp.status_code, resp.text)
                or resp.status_code != 200
            ):
                return None

            dl_url = next(
                (
                    app.get("file", {}).get("path")
                    for app in resp.json().get("datalist", {}).get("list", [])
                    if app.get("package") == ctx.pkg
                    and app.get("file", {}).get("vername")
                    in (ctx.target_ver, base_ver)
                ),
                None,
            )

            if dl_url:
                out_path = ctx.get_out_path(".apk")
                print("[INFO] Downloading from Aptoide...")
                if download_file_stream(ctx.scraper, dl_url, out_path):
                    return out_path
            print("[WARN] Version not found.")
        except (requests.exceptions.RequestException, ValueError, OSError) as err:
            print(f"[ERROR] Tier 4 failed: {err}")
        return None
