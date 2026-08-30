"""
Abstract Base Class module for all APK Scraper tiers.
Enforces a strict interface for dynamic discovery and execution.
"""

import abc
from typing import Optional
from core.context import Context


class BaseScraper(abc.ABC):
    """
    Blueprint for all scraper modules.
    Any new downloader tier must inherit from this class.
    """

    @property
    @abc.abstractmethod
    def tier_name(self) -> str:
        """
        Defines the string identifier for the scraper.
        Returns:
            str: The name of the scraper (e.g., 'github', 'apkmirror').
        """

    @abc.abstractmethod
    def scrape(self, ctx: Context) -> Optional[str]:
        """
        Executes the scraping logic for the specific tier.

        Args:
            ctx (Context): The shared execution context holding app data,
                           the scraper instance, and rate limiters.

        Returns:
            Optional[str]: The absolute path to the downloaded file,
                           or None if the download failed/was not found.
        """
