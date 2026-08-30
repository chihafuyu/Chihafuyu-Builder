"""Tier 9 Scraper: Google Play via Apkeep."""

import base64
import glob
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from core.context import Context
from core.utils import _safe_filename
from .base import BaseScraper


class GooglePlayScraper(BaseScraper):
    """Downloads APK from Play Store securely using GitHub Secrets."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "google_play"

    def _find_and_copy_apk(self, tmp_dir: str, dl_dir: str) -> Optional[str]:
        """Finds the downloaded file in the temp directory and moves it."""
        for ext in ("*.apk", "*.xapk", "*.apkm", "*.apks", "*.zip"):
            found = glob.glob(os.path.join(tmp_dir, ext))
            if found:
                dst = os.path.join(dl_dir, _safe_filename(os.path.basename(found[0])))
                shutil.copy2(found[0], dst)
                return dst
        return None

    def _execute_apkeep(
        self, ctx: Context, dl_dir: str, email: str, aas_token: str, props_b64: Optional[str]
    ) -> Optional[str]:
        """Handles the temporary directory generation and subprocess execution."""
        with tempfile.TemporaryDirectory(prefix="apkeep-play-") as tmp:
            try:
                # Generate apkeep.ini on the fly
                ini_path = os.path.join(tmp, "apkeep.ini")
                with open(ini_path, "w", encoding="utf-8") as f_obj:
                    f_obj.write(f"[google]\nemail = {email}\naas_token = {aas_token}\n")

                cmd = [
                    "apkeep",
                    "-a", f"{ctx.pkg}@{ctx.target_ver}",
                    "-d", "google-play",
                    "-i", ini_path
                ]

                # Decode Base64 to device.properties on the fly
                if props_b64:
                    props_path = os.path.join(tmp, "device.properties")
                    with open(props_path, "wb") as f_obj:
                        f_obj.write(base64.b64decode(props_b64))
                    cmd.extend([
                        "-o", f"device=default,device_properties_file={props_path}"
                    ])

                cmd.append(tmp)
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)

                if res.returncode != 0:
                    print(f"[WARN] Apkeep Play Store failed: {res.stderr.strip()}")
                    return None
            except OSError as err:
                print(f"[ERROR] apkeep execution failed: {err}")
                return None

            return self._find_and_copy_apk(tmp, dl_dir)

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process via Google Play and Apkeep."""
        print(f"[TIER 9] Secure Google Play: v{ctx.target_ver}")

        email = os.getenv("PLAY_EMAIL")
        aas_token = os.getenv("PLAY_AAS_TOKEN")
        props_b64 = os.getenv("DEVICE_PROPERTIES_B64")

        if not email or not aas_token:
            print("[WARN] Missing 'PLAY_EMAIL' or 'PLAY_AAS_TOKEN' in env.")
            return None

        dl_dir = os.path.join(ctx.out_dir, ctx.pkg)
        os.makedirs(dl_dir, exist_ok=True)

        return self._execute_apkeep(ctx, dl_dir, email, aas_token, props_b64)
