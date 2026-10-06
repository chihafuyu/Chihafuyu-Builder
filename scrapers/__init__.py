"""
Dynamic module loader for scraper tiers.
Automatically detects and registers any BaseScraper implementations.
"""

import importlib
import inspect
import pkgutil

from .base import BaseScraper


def load_all_scrapers() -> dict[str, type[BaseScraper]]:
    """Discovers all scraper classes in the current package directory."""
    registry: dict[str, type[BaseScraper]] = {}

    for _, module_name, _ in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{module_name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseScraper) and obj is not BaseScraper:
                try:
                    registry[obj.tier_name()] = obj
                except TypeError:
                    # Skip classes that fail to implement abstract methods.
                    continue

    return registry


# Instantiated registry ready to be imported by the main script.
AVAILABLE_SCRAPERS = load_all_scrapers()
