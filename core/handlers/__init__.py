'''
Imports all handler modules from the current directory.

Will raise a RuntimeError if the discovery count does not match the number of handler modules.
Will raise errors from handler module importing.
'''
import pkgutil
import importlib
from pathlib import Path

def discover_handlers():
    """Dynamically import all handlers."""
    count = 0
    errors: list[tuple[str, Exception]] = []

    current_dir = Path(__file__).parent
    expected_files = [
        f for f in current_dir.iterdir()
        if f.is_file() and f.suffix == '.py' and f.name != '__init__.py'
    ]
    expected_count = len(expected_files)

    for module_info in pkgutil.iter_modules(__path__):
        if module_info.ispkg:
            continue
        full_name = f'{__name__}.{module_info.name}'
        try:
            importlib.import_module(full_name)
            count += 1
        except Exception as e:
            errors.append((full_name, e))

    if count != expected_count and len(errors) == 0:
        mismatch_msg = (
            f'Discovery mismatch in {__name__}: Fount {expected_count} valid .py files '
            f'in the directory, but only successfully imported {count}.'
        )
        errors.append((__name__, RuntimeError(mismatch_msg)))

    return errors
