'''
Imports all editor modules from the current directory.

Checks the registry as new modules are imported to ensure they are registered, raising RuntimeError if not.
Will raise errors from editor module importing.
'''
import pkgutil
import importlib
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.registry import Registry as RegistryType

def discover_editors(registry: 'type[RegistryType]') -> list[tuple[str, Exception]]:
    """Dynamically import all editors."""
    errors: list[tuple[str, Exception]] = []
    module_names: list[str] = []

    for module_info in pkgutil.iter_modules(__path__):
        if module_info.ispkg:
            continue
        module_names.append(f'{__name__}.{module_info.name}')

    if not module_names:
        errors.append((__name__, RuntimeError(
            f'{__name__}: pkgutil.iter_modules found no editor modules to import.'
        )))
        return errors

    for full_name in module_names:
        before = len(registry._editor_profiles)
        try:
            importlib.import_module(full_name)
        except Exception as e:
            errors.append((full_name, e))
            continue

        after = len(registry._editor_profiles)
        if after == before:
            errors.append((full_name, RuntimeError(
                f'{full_name} imported successfully but found no profile to register. '
                f'Ensure that the editor is defined with @Registry.register_editor decorator.'
            )))

    return errors
