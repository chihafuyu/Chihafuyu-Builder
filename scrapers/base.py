"""
Abstract Base Class module for all APK Scraper tiers.
Enforces a strict interface for dynamic discovery and execution.
"""

import abc
from typing import Any

from core.context import Context


class BaseScraper(abc.ABC):
    """Blueprint for all scraper modules."""

    @classmethod
    @abc.abstractmethod
    def tier_name(cls) -> str:
        """String identifier for this scraper (e.g. 'github', 'apkmirror')."""
        raise NotImplementedError

    @abc.abstractmethod
    def scrape(self, ctx: Context) -> str | None:
        """Executes the scraping logic for the specific tier."""
        raise NotImplementedError

    def close(self) -> None:
        """Optional cleanup hook for subclasses (e.g. closing proxy sessions)."""

    def __enter__(self) -> BaseScraper:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
