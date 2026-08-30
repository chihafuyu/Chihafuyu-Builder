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

    def _find_and_copy_apk(self, ctx: Context, tmp_dir: str, dl_dir: str) -> Optional[str]:
        """Finds the downloaded file or packed split APK directory."""
        for ext in ("*.xapk", "*.apkm", "*.apks", "*.zip"):
            found = glob.glob(os.path.join(tmp_dir, ext))
            if found:
                dst = os.path.join(dl_dir, _safe_filename(os.path.basename(found[0])))
                shutil.copy2(found[0], dst)
                return dst

        apk_files = glob.glob(os.path.join(tmp_dir, "*.apk"))
        if apk_files:
            if len(apk_files) == 1:
                dst = os.path.join(dl_dir, _safe_filename(os.path.basename(apk_files[0])))
                shutil.copy2(apk_files[0], dst)
                return dst

            pack_dir = os.path.join(tmp_dir, "split_pack")
            os.makedirs(pack_dir, exist_ok=True)
            for apk in apk_files:
                shutil.move(apk, pack_dir)

            base_name = os.path.join(dl_dir, _safe_filename(ctx.pkg))
            shutil.make_archive(base_name, "zip", pack_dir)
            dst = f"{base_name}.apks"
            os.replace(f"{base_name}.zip", dst)
            return dst

        for item in os.listdir(tmp_dir):
            item_path = os.path.join(tmp_dir, item)
            if os.path.isdir(item_path):
                base_name = os.path.join(dl_dir, _safe_filename(item))
                shutil.make_archive(base_name, "zip", item_path)
                dst = f"{base_name}.apks"
                os.replace(f"{base_name}.zip", dst)
                return dst

        return None

    def _prepare_cmd(
        self, ctx: Context, tmp: str, email: str, aas_token: str, props_b64: Optional[str]
    ) -> list:
        """Builds the apkeep command adhering strictly to official CLI specs."""
        cmd = [
            "apkeep",
            "-a", ctx.pkg,
            "-d", "google-play",
            "-e", email,
            "-t", aas_token
        ]

        options = ["split_apk=true"]

        if props_b64:
            props_path = os.path.join(tmp, "device.properties")
            with open(props_path, "wb") as f_obj:
                f_obj.write(base64.b64decode(props_b64))
            options.extend(["device=default", f"device_properties_file={props_path}"])

        cmd.extend(["-o", ",".join(options)])
        # OUTPATH must always be placed at the very end of the command arguments
        cmd.append(tmp)
        return cmd

    def _execute_apkeep(
        self, ctx: Context, dl_dir: str, email: str, aas_token: str, props_b64: Optional[str]
    ) -> Optional[str]:
        """Handles the temporary directory generation and subprocess execution."""
        with tempfile.TemporaryDirectory(prefix="apkeep-play-") as tmp:
            try:
                cmd = self._prepare_cmd(ctx, tmp, email, aas_token, props_b64)
                res = subprocess.run(cmd, capture_output=True, text=True, check=False)

                if res.returncode != 0:
                    print(f"[WARN] Apkeep Play Store failed: {res.stderr.strip()}")
                    return None

            except OSError as err:
                print(f"[ERROR] apkeep execution failed: {err}")
                return None

            copied_file = self._find_and_copy_apk(ctx, tmp, dl_dir)

            if not copied_file:
                err_log = res.stderr.strip() or res.stdout.strip()
                print(f"[WARN] Apkeep skipped silently. Log: {err_log}")

            return copied_file

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
