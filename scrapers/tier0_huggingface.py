"""Tier 0 Scraper: HuggingFace Datasets."""

from typing import Optional
import requests

from core.context import Context
from core.utils import download_file_stream
from .base import BaseScraper


class HuggingfaceScraper(BaseScraper):
    """Scrapes APKs directly from HuggingFace Vaults."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "huggingface"

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process from HuggingFace."""
        hf_user = ctx.app_data.get("hf_user", "chihafuyu")
        hf_repo = ctx.app_data.get(
            "hf_repo", f"{hf_user}/{ctx.app_data.get('archive_id')}"
        )

        if not ctx.app_data.get("archive_id") and not ctx.app_data.get("hf_repo"):
            return None

        print(f"[TIER 0] HuggingFace: v{ctx.target_ver}")
        ctx.limiter.wait()
        base_url = f"https://huggingface.co/datasets/{hf_repo}/resolve/main"

        for ext in [".apk", ".xapk", ".apkm", ".apks"]:
            dl_link = f"{base_url}/{ctx.pkg}_{ctx.target_ver}{ext}"
            out_path = ctx.get_out_path(ext)
            try:
                res = ctx.scraper.head(dl_link, timeout=10, allow_redirects=True)
                if res.status_code == 200:
                    print("[INFO] Downloading from Vault...")
                    if download_file_stream(ctx.scraper, dl_link, out_path):
                        return out_path
            except requests.exceptions.RequestException:
                continue

        print(f"[WARN] Not found in '{hf_repo}'.")
        return None
