"""
Dynamic module loader for scraper tiers.
Automatically detects and registers any BaseScraper implementations.
"""

import importlib
import pkgutil
import inspect
from typing import Dict, Type
from .base import BaseScraper

def load_all_scrapers() -> Dict[str, Type[BaseScraper]]:
    """
    Discovers all scraper classes in the current package directory.

    Returns:
        Dict[str, Type[BaseScraper]]: A registry mapping tier names to their classes.
    """
    registry: Dict[str, Type[BaseScraper]] = {}

    # Iterate through all files in the scrapers/ directory
    for _, module_name, _ in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{module_name}")

        # Inspect all classes inside the loaded module
        for _, obj in inspect.getmembers(module, inspect.isclass):
            # Ensure it inherits from BaseScraper but is NOT the BaseScraper itself
            if issubclass(obj, BaseScraper) and obj is not BaseScraper:
                try:
                    # Instantiate to grab the tier_name property
                    instance = obj()
                    registry[instance.tier_name] = obj
                except TypeError:
                    # Skips classes that fail to implement abstract methods
                    continue

    return registry

# Instantiated registry ready to be imported by the main script
AVAILABLE_SCRAPERS = load_all_scrapers()
