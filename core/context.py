"""
Core context structures and state management for the APK Patcher.
Provides shared execution state for all scraper tiers.
"""

import os
import time
from dataclasses import dataclass
from typing import Any
from core.utils import _safe_filename


class RateLimiter:
    """Ensures a minimum delay between requests to avoid rate limits."""

    def __init__(self, delay: float):
        self.delay = delay
        self.last_req = 0.0

    def wait(self) -> None:
        """Waits if the time since the last request is less than the delay."""
        now = time.monotonic()
        if now - self.last_req < self.delay:
            time.sleep(self.delay - (now - self.last_req))
        self.last_req = time.monotonic()

    def reset(self) -> None:
        """Resets the internal timer."""
        self.last_req = 0.0


@dataclass
class Context:
    """Holds common variables for the scraping process across all tiers."""
    scraper: Any
    app_data: dict
    target_ver: str
    arch: str
    out_dir: str
    limiter: RateLimiter

    @property
    def pkg(self) -> str:
        """Returns the package name from app_data."""
        return self.app_data["package"]

    def get_out_path(self, ext: str) -> str:
        """Returns the safe output path for the downloaded file."""
        pkg_str = _safe_filename(self.pkg)
        ver_str = _safe_filename(self.target_ver)
        return os.path.join(self.out_dir, f"{pkg_str}_{ver_str}{ext}")
