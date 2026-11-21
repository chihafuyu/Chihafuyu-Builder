"""
Core context structures and state management for the APK Patcher.
Provides shared execution state for all scraper tiers.
"""

import os
import re
import time
from dataclasses import dataclass
from typing import Any

from core.utils import _safe_filename


# Characters that are invalid in Windows filenames and awkward in URLs.
# Tier 0 and the vault sync uploader share this sanitizer so a cached file is
# always found under the exact name that was uploaded.
VAULT_UNSAFE_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


class RateLimiter:
    """Ensures a minimum delay between requests to avoid rate limits."""

    def __init__(self, delay: float):
        self.delay = delay
        self.last_req = 0.0

    def wait(self) -> None:
        """Sleeps if the configured delay has not elapsed since the last call."""
        now = time.monotonic()
        elapsed = now - self.last_req
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_req = time.monotonic()


def vault_name_part(value: str) -> str:
    """Sanitizes one component of a HuggingFace vault filename."""
    safe = VAULT_UNSAFE_CHARS.sub("_", _safe_filename(value)).strip()
    return safe or "artifact"


def vault_filename(package: str, version: str, ext: str) -> str:
    """Builds the canonical vault filename shared by Tier 0 and the sync job.

    The package/version pair is sanitized exactly once so the uploader and
    the runtime lookup can never disagree about the cached path.
    """
    return f"{vault_name_part(package)}_{vault_name_part(version)}{ext}"


@dataclass
class Context:
    """Holds shared state for the scraping pipeline across all tiers."""

    scraper: Any
    app_data: dict
    target_ver: str
    arch: str
    out_dir: str
    limiter: RateLimiter
    # Download provenance used by per-app build logs.
    original_name: str = ""
    source_name: str = ""
    source_url: str = ""

    @property
    def pkg(self) -> str:
        """Package name declared in app_data."""
        return self.app_data["package"]

    @property
    def pkg_dir(self) -> str:
        """Per-package download directory under out_dir."""
        return os.path.join(self.out_dir, _safe_filename(self.pkg))

    def get_out_path(self, ext: str) -> str:
        """Returns the sanitized output path for the given file extension."""
        pkg_str = _safe_filename(self.pkg)
        ver_str = _safe_filename(self.target_ver)
        return os.path.join(self.out_dir, f"{pkg_str}_{ver_str}{ext}")
