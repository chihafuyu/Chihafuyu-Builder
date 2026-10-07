"""Tier 0 Scraper: HuggingFace Datasets."""

from urllib.parse import quote

import requests

from core.context import Context
from core.utils import download_file_stream
from .base import BaseScraper


DEFAULT_HF_USER = "chihafuyu"
HF_EXTENSIONS = (".apk", ".xapk", ".apkm", ".apks")


class HuggingfaceScraper(BaseScraper):
    """Scrapes APKs directly from HuggingFace Vaults."""

    @classmethod
    def tier_name(cls) -> str:
        """Returns the tier identifier."""
        return "huggingface"

    @staticmethod
    def _resolve_repo(ctx: Context) -> str | None:
        """Resolves the HuggingFace repo id, validating that inputs exist."""
        explicit = ctx.app_data.get("hf_repo")
        if explicit:
            return explicit
        archive_id = ctx.app_data.get("archive_id")
        if not archive_id:
            return None
        hf_user = ctx.app_data.get("hf_user", DEFAULT_HF_USER)
        return f"{hf_user}/{archive_id}"

    @staticmethod
    def _build_url(hf_repo: str, filename: str) -> str:
        """Builds an encoded resolve URL safe for spaces and unicode names."""
        repo_path = "/".join(quote(part, safe="") for part in hf_repo.split("/"))
        return (
            f"https://huggingface.co/datasets/{repo_path}"
            f"/resolve/main/{quote(filename, safe='')}"
        )

    def scrape(self, ctx: Context) -> str | None:
        """Executes the scraping process from HuggingFace."""
        hf_repo = self._resolve_repo(ctx)
        if not hf_repo:
            return None

        print(f"[TIER 0] HuggingFace: v{ctx.target_ver}")
        ctx.limiter.wait()

        for ext in HF_EXTENSIONS:
            filename = f"{ctx.pkg}_{ctx.target_ver}{ext}"
            dl_link = self._build_url(hf_repo, filename)
            out_path = ctx.get_out_path(ext)
            try:
                res = ctx.scraper.head(dl_link, timeout=10, allow_redirects=True)
            except requests.exceptions.RequestException as err:
                print(f"[WARN] HF probe failed for {ext}: {err}")
                continue
            if res.status_code != 200:
                continue
            print("[INFO] Downloading from Vault...")
            if download_file_stream(ctx.scraper, dl_link, out_path):
                return out_path

        print(f"[WARN] Not found in '{hf_repo}'.")
        return None
