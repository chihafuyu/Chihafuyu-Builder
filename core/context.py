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
        """Sleeps if the configured delay has not elapsed since the last call."""
        now = time.monotonic()
        elapsed = now - self.last_req
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_req = time.monotonic()

    def reset(self) -> None:
        """Clears the internal timer so the next wait() does not throttle."""
        self.last_req = 0.0


@dataclass
class Context:
    """Holds shared state for the scraping pipeline across all tiers."""

    scraper: Any
    app_data: dict
    target_ver: str
    arch: str
    out_dir: str
    limiter: RateLimiter

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
