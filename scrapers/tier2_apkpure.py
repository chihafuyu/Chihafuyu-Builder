"""Tier 2 Scraper: APKPure."""

import os
import tempfile

from core.context import Context
from core.utils import copy_first_match, run_apkeep
from .base import BaseScraper


PACKAGE_EXTENSIONS = ("*.apk", "*.xapk", "*.apkm", "*.apks")


class ApkpureScraper(BaseScraper):
    """Downloads APK from APKPure via apkeep."""

    @classmethod
    def tier_name(cls) -> str:
        """Returns the tier identifier."""
        return "apkpure"

    def scrape(self, ctx: Context) -> str | None:
        """Executes the scraping process via apkeep."""
        print(f"[TIER 2] APKPure: v{ctx.target_ver}")
        dl_dir = os.path.join(ctx.out_dir, ctx.pkg)
        os.makedirs(dl_dir, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="apkeep-") as tmp:
            cmd = [
                "apkeep",
                "-a", f"{ctx.pkg}@{ctx.target_ver}",
                "-d", "apk-pure",
                tmp,
            ]
            if run_apkeep(cmd, tag="apkeep (APKPure)") is None:
                return None

            result = copy_first_match(tmp, dl_dir, PACKAGE_EXTENSIONS)
            if not result:
                print("[WARN] apkeep produced no output file.")
            return result
