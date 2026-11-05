"""Shared helper functions for scraper tiers."""

import requests

from core.context import Context
from core.utils import download_file_stream


def probe_and_download(
    ctx: Context,
    url: str,
    out_path: str,
    source_name: str,
    *,
    allowed_statuses: tuple[int, ...] = (200,),
    skip_probe: bool = False,
    referer: str = "",
) -> str | None:
    """Standardized probe and download helper for scraper tiers."""
    ctx.limiter.wait()

    if not skip_probe:
        try:
            res = ctx.scraper.head(url, timeout=10, allow_redirects=True)
        except requests.exceptions.RequestException as err:
            print(f"[WARN] {source_name} probe failed: {err}")
            return None

        if res.status_code not in allowed_statuses:
            print(f"[WARN] {source_name} probe returned HTTP {res.status_code}")
            return None

    print(f"[INFO] Downloading from {source_name}...")
    if download_file_stream(ctx.scraper, url, out_path, referer=referer):
        return out_path
    return None
