"""Tier 9 Scraper: Google Play via Apkeep."""

import base64
import binascii
import os
import shutil
import tempfile

from core.context import Context
from core.utils import _safe_filename, copy_first_match, run_apkeep
from .base import BaseScraper


BUNDLE_EXTENSIONS = ("*.xapk", "*.apkm", "*.apks", "*.zip")
# AUTH tokens issued by dispenser services (e.g. Aurora Store) start
# with this prefix. AAS tokens obtained via the OAuth flow do not.
AUTH_TOKEN_PREFIX = "ya29."


class GooglePlayScraper(BaseScraper):
    """Downloads APK from Play Store securely using GitHub Secrets."""

    @classmethod
    def tier_name(cls) -> str:
        """Returns the tier identifier."""
        return "google_play"

    def _find_and_copy_apk(
        self, ctx: Context, tmp_dir: str, dl_dir: str
    ) -> str | None:
        """Finds the downloaded file or packs split APK directory into .apks."""
        direct = copy_first_match(tmp_dir, dl_dir, BUNDLE_EXTENSIONS)
        if direct:
            return direct

        apk_files = sorted(
            os.path.join(tmp_dir, f)
            for f in os.listdir(tmp_dir)
            if f.endswith(".apk")
        )
        if apk_files:
            if len(apk_files) == 1:
                dst = os.path.join(
                    dl_dir, _safe_filename(os.path.basename(apk_files[0]))
                )
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

        for item in sorted(os.listdir(tmp_dir)):
            item_path = os.path.join(tmp_dir, item)
            if os.path.isdir(item_path):
                base_name = os.path.join(dl_dir, _safe_filename(item))
                shutil.make_archive(base_name, "zip", item_path)
                dst = f"{base_name}.apks"
                os.replace(f"{base_name}.zip", dst)
                return dst

        return None

    @staticmethod
    def _decode_properties(props_b64: str) -> bytes | None:
        """Strictly decodes the device.properties blob, returning None on bad input."""
        try:
            return base64.b64decode(props_b64, validate=True)
        except (binascii.Error, ValueError) as err:
            print(f"[WARN] Invalid DEVICE_PROPERTIES_B64: {err}")
            return None

    @staticmethod
    def _build_auth_args(email: str, token: str) -> list[str]:
        """Returns the correct apkeep auth flags for the given token type.

        AUTH tokens (prefix `ya29.`) come from a token dispenser and
        require `--auth-token`. AAS tokens obtained via the OAuth flow use
        the `-t` short flag. `--accept-tos` is added in both cases so a
        fresh account does not block the download on the ToS dialog.
        """
        if token.startswith(AUTH_TOKEN_PREFIX):
            return ["-e", email, "--auth-token", token, "--accept-tos"]
        return ["-e", email, "-t", token, "--accept-tos"]

    def _prepare_cmd(
        self,
        ctx: Context,
        tmp: str,
        email: str,
        token: str,
        props_b64: str | None,
    ) -> list[str] | None:
        """Builds the apkeep command adhering to the 1.1.0 CLI spec."""
        cmd = ["apkeep", "-a", ctx.pkg, "-d", "google-play"]
        cmd.extend(self._build_auth_args(email, token))

        options = ["split_apk=true"]

        if props_b64:
            decoded = self._decode_properties(props_b64)
            if decoded is None:
                return None
            props_path = os.path.join(tmp, "device.properties")
            with open(props_path, "wb") as f_obj:
                f_obj.write(decoded)
            options.extend(
                ["device=default", f"device_properties_file={props_path}"]
            )

        cmd.extend(["-o", ",".join(options)])
        # OUTPATH must always be placed at the very end of the command arguments.
        cmd.append(tmp)
        return cmd

    def _execute_apkeep(
        self,
        ctx: Context,
        dl_dir: str,
        email: str,
        token: str,
        props_b64: str | None,
    ) -> str | None:
        """Handles the temporary directory generation and subprocess execution."""
        with tempfile.TemporaryDirectory(prefix="apkeep-play-") as tmp:
            cmd = self._prepare_cmd(ctx, tmp, email, token, props_b64)
            if cmd is None:
                return None

            res = run_apkeep(cmd, tag="Apkeep Play Store")
            if res is None:
                return None

            copied_file = self._find_and_copy_apk(ctx, tmp, dl_dir)
            if not copied_file:
                err_log = (res.stderr or res.stdout or "").strip()[-500:]
                print(f"[WARN] Apkeep produced no output. Log: {err_log}")
            return copied_file

    def scrape(self, ctx: Context) -> str | None:
        """Executes the scraping process via Google Play and Apkeep."""
        print(f"[TIER 9] Secure Google Play: v{ctx.target_ver}")

        email = os.getenv("PLAY_EMAIL")
        token = os.getenv("PLAY_AAS_TOKEN")
        props_b64 = os.getenv("DEVICE_PROPERTIES_B64")

        if not email or not token:
            print("[WARN] Missing 'PLAY_EMAIL' or 'PLAY_AAS_TOKEN' in env.")
            return None

        token_kind = (
            "AUTH" if token.startswith(AUTH_TOKEN_PREFIX) else "AAS"
        )
        print(f"[INFO] Detected {token_kind} token.", flush=True)

        dl_dir = os.path.join(ctx.out_dir, ctx.pkg)
        os.makedirs(dl_dir, exist_ok=True)

        return self._execute_apkeep(
            ctx, dl_dir, email, token, props_b64
        )
