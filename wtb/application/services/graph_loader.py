"""
Graph Factory Loader - Application layer module for dynamic graph loading.

Extracted from domain model (VariantCombination.create_graph) to maintain DIP:
domain models should not use importlib directly.

Security: Only loads from explicitly specified module paths. No arbitrary code
execution - the factory function must already be importable.
"""

import importlib
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def load_graph_factory(module_path: str, factory_name: str) -> Callable[[], Any]:
    """
    Load a graph factory function from a module path.

    Args:
        module_path: Fully qualified Python module path (e.g. 'my_app.graphs')
        factory_name: Name of the factory function in the module

    Returns:
        Callable that creates a graph when invoked

    Raises:
        ImportError: If module cannot be imported
        AttributeError: If factory_name not found in module
    """
    module = importlib.import_module(module_path)
    factory = getattr(module, factory_name)
    if not callable(factory):
        raise TypeError(
            f"{module_path}.{factory_name} is not callable "
            f"(got {type(factory).__name__})"
        )
    logger.debug(f"Loaded graph factory: {module_path}.{factory_name}")
    return factory
