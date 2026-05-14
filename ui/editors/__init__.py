import pkgutil
import importlib
import logging

logger = logging.getLogger('radiata')

def discover_editors():
    """Dynamically import all modules in this package to trigger registration."""
    logger.debug("[REGISTRY] Starting dynamic handler discovery...")
    # __name__ is 'ui.widgets'
    # __path__ is the physical disk path of this folder
    for loader, module_name, is_pkg in pkgutil.walk_packages(__path__, __name__ + "."):
        if not is_pkg:
            logger.debug(f"[REGISTRY] Importing: {module_name}")
            importlib.import_module(module_name)