import pkgutil
import importlib

def discover_handlers():
    """Dynamically import all handlers."""
    print("\n--- [DEBUG] FROZEN HANDLER DISCOVERY START ---")
    count = 0
    
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.ispkg:
            continue
        full_name = f'{__name__}.{module_info.name}'
        try:
            importlib.import_module(full_name)
            print(f'Imported handler: {full_name}')
            count += 1
        except Exception as e:
            print(f'Failed to import {full_name}: {e}')

    print(f"--- [DEBUG] END DISCOVERY. Total imported: {count} ---\n")
