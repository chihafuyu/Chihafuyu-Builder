"""
Tier 7 Scraper: GitHub Releases.
Directly targets attached assets in GitHub release tags.
"""

import requests

from core.context import Context
from core.utils import download_file_stream
from .base import BaseScraper


class GithubScraper(BaseScraper):
    """Scraper implementation for downloading APKs from GitHub Releases."""

    @classmethod
    def tier_name(cls) -> str:
        """Returns the tier identifier."""
        return "github"

    @staticmethod
    def _candidate_tags(version: str) -> list[str]:
        """Returns plausible tag variants, dropping duplicates."""
        tags = [f"v{version}", version]
        return list(dict.fromkeys(tags))

    def scrape(self, ctx: Context) -> str | None:
        """Scrapes the APK directly from GitHub Releases."""
        gh_repo = ctx.app_data.get("github_repo")
        gh_asset = ctx.app_data.get("github_asset")
        if not gh_repo or not gh_asset:
            return None

        print(f"[TIER 7] GitHub Releases: v{ctx.target_ver}")

        for tag in self._candidate_tags(ctx.target_ver):
            ctx.limiter.wait()
            dl_link = (
                f"https://github.com/{gh_repo}/releases/download/"
                f"{tag}/{gh_asset}"
            )
            out_path = ctx.get_out_path(".apk")

            try:
                head = ctx.scraper.head(
                    dl_link, timeout=10, allow_redirects=True
                )
            except requests.exceptions.RequestException as err:
                print(f"[WARN] GitHub probe failed for {tag}: {err}")
                continue

            # GitHub may return 403/405 to anonymous HEAD requests even
            # when the asset is downloadable, so allow GET to proceed.
            if head.status_code not in (200, 403, 405):
                continue

            print("[INFO] Downloading from GitHub...")
            if download_file_stream(ctx.scraper, dl_link, out_path):
                return out_path

        return None
