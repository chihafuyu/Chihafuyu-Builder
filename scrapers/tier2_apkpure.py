"""Tier 2 Scraper: APKPure."""

import glob
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from core.context import Context
from core.utils import _safe_filename
from .base import BaseScraper


class ApkpureScraper(BaseScraper):
    """Downloads APK from APKPure via apkeep."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "apkpure"

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process via apkeep."""
        print(f"[TIER 2] APKPure: v{ctx.target_ver}")
        dl_dir = os.path.join(ctx.out_dir, ctx.pkg)
        os.makedirs(dl_dir, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="apkeep-") as tmp:
            try:
                cmd = [
                    "apkeep",
                    "-a",
                    f"{ctx.pkg}@{ctx.target_ver}",
                    "-d",
                    "apk-pure",
                    tmp,
                ]
                res = subprocess.run(cmd, capture_output=True, check=False)
                if res.returncode != 0:
                    return None
            except OSError as err:
                print(f"[WARN] apkeep execution failed: {err}")
                return None

            files = []
            for ext in ("*.apk", "*.xapk", "*.apkm", "*.apks"):
                files.extend(glob.glob(os.path.join(tmp, ext)))
            if not files:
                return None
            dst = os.path.join(
                dl_dir, _safe_filename(os.path.basename(files[0]))
            )
            shutil.copy2(files[0], dst)
            return dst
