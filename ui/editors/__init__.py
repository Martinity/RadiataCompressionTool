import pkgutil
import importlib

def discover_editors():
    """Dynamically import all editors."""
    print("\n--- [DEBUG] FROZEN EDITOR DISCOVERY START ---")
    count = 0
    
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.ispkg:
            continue
        full_name = f'{__name__}.{module_info.name}'
        try:
            importlib.import_module(full_name)
            print(f'Imported editor: {full_name}')
            count += 1
        except Exception as e:
            print(f'Failed to import {full_name}: {e}')

    print(f"--- [DEBUG] END DISCOVERY. Total imported: {count} ---\n")
