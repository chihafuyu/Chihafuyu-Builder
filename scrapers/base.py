"""
Abstract Base Class module for all APK Scraper tiers.
Enforces a strict interface for dynamic discovery and execution.
"""

import abc
from typing import Optional, Any
from core.context import Context


class BaseScraper(abc.ABC):
    """
    Blueprint for all scraper modules.
    Any new downloader tier must inherit from this class.
    """

    @classmethod
    @abc.abstractmethod
    def tier_name(cls) -> str:
        """
        Defines the string identifier for the scraper.
        Returns:
            str: The name of the scraper (e.g., 'github', 'apkmirror').
        """

    @abc.abstractmethod
    def scrape(self, ctx: Context) -> Optional[str]:
        """
        Executes the scraping logic for the specific tier.
        """

    def close(self) -> None:
        """
        Cleans up any allocated resources (e.g., proxy sessions).
        Should be overridden by subclasses if needed.
        """

    def __enter__(self) -> "BaseScraper":
        """Enters the runtime context related to this object."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exits the runtime context and cleans up resources."""
        self.close()
