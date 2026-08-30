"""Tier 9 Scraper: Google Play via Aurora Dispenser & Apkeep."""

import glob
import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple
import requests

from core.context import Context
from core.utils import _safe_filename
from scrapers.base import BaseScraper


class AuroraPlaystoreScraper(BaseScraper):
    """Downloads APK from Play Store using Aurora Dispenser accounts."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "aurora_play"

    def _get_anonymous_token(self) -> Tuple[Optional[str], Optional[str]]:
        """Hits the Aurora Dispenser API to get an active Google token."""
        try:
            # Using the GET endpoint Aurora Store
            resp = requests.get(
                "https://auroraoss.com/api/auth", timeout=15
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("email"), data.get("auth")
            if resp.status_code == 403:
                print("[WARN] The GitHub Runner IP address is blocked.")
        except requests.exceptions.RequestException as err:
            print(f"[WARN] Aurora Dispenser unreachable: {err}")
        return None, None

    def _get_device_codename(self, arch: str) -> str:
        """Spoofs the device based on architecture for Apkeep."""
        spoof_map = {
            "arm64-v8a": "oriole",
            "armeabi-v7a": "walleye",
            "x86": "generic_x86",
            "x86_64": "generic_x86_64",
        }
        return spoof_map.get(arch.lower(), "oriole")

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process via Google Play and Apkeep."""
        print(f"[TIER 9] Aurora/PlayStore: v{ctx.target_ver}")

        email, token = self._get_anonymous_token()
        if not email or not token:
            print("[WARN] Failed to obtain credentials from the Aurora Dispenser.")
            return None

        dl_dir = os.path.join(ctx.out_dir, ctx.pkg)
        os.makedirs(dl_dir, exist_ok=True)
        device_codename = self._get_device_codename(ctx.arch)

        with tempfile.TemporaryDirectory(prefix="apkeep-aurora-") as tmp:
            try:
                cmd = [
                    "apkeep",
                    "-a",
                    f"{ctx.pkg}@{ctx.target_ver}",
                    "-d",
                    "google-play",
                    "-e",
                    email,
                    "-t",
                    token,
                    "--device",
                    device_codename,
                    tmp,
                ]
                res = subprocess.run(
                    cmd, capture_output=True, text=True, check=False
                )

                if res.returncode != 0:
                    print(
                        f"[WARN] Apkeep Play Store failed: {res.stderr.strip()}"
                    )
                    return None
            except OSError as err:
                print(f"[ERROR] apkeep execution failed: {err}")
                return None

            files = []
            for ext in ("*.apk", "*.xapk", "*.apkm", "*.apks", "*.zip"):
                files.extend(glob.glob(os.path.join(tmp, ext)))
            if not files:
                return None

            dst = os.path.join(
                dl_dir, _safe_filename(os.path.basename(files[0]))
            )
            shutil.copy2(files[0], dst)
            return dst
