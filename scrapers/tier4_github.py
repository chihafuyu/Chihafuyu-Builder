"""
Tier 7 Scraper: GitHub Releases.
Directly targets attached assets in GitHub release tags.
"""

from typing import Optional
import requests

from core.context import Context
from core.utils import download_file_stream
from .base import BaseScraper


class GithubScraper(BaseScraper):
    """Scraper implementation for downloading APKs from GitHub Releases."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "github"

    def scrape(self, ctx: Context) -> Optional[str]:
        """Scrapes the APK directly from GitHub Releases."""
        gh_repo = ctx.app_data.get("github_repo")
        gh_asset = ctx.app_data.get("github_asset")

        if not gh_repo or not gh_asset:
            return None

        print(f"[TIER 7] GitHub Releases: v{ctx.target_ver}")

        # Fallback list for tag naming conventions
        tags_to_try = [f"v{ctx.target_ver}", ctx.target_ver]

        for tag in tags_to_try:
            ctx.limiter.wait()
            dl_link = f"https://github.com/{gh_repo}/releases/download/{tag}/{gh_asset}"
            out_path = ctx.get_out_path(".apk")

            try:
                # Use HEAD request to check availability efficiently
                response = ctx.scraper.head(dl_link, timeout=10, allow_redirects=True)
                if response.status_code == 200:
                    print("[INFO] Downloading from GitHub...")
                    if download_file_stream(ctx.scraper, dl_link, out_path):
                        return out_path
            except requests.exceptions.RequestException as err:
                print(f"[WARN] Connection error while checking tag {tag}: {err}")
                continue

        return None
