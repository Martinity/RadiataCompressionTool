"""
Thorough testing for the registration process as it is one of the most fragile
and critical part of the application
"""
from dataclasses import dataclass
from pathlib import Path

import pytest
from core.contracts import BaseEditor, BaseHandler
from core.registry import GLOBAL_ACTIONS, Registry, _normalise_actions
from core.workers import ActionDef, ActionType

###------ Mocks and dummies for testing ------###


class DummyHandler(BaseHandler):
    pass


class DummyFallbackHandler(BaseHandler):
    pass


class DummyEditor(BaseEditor):
    pass


class DummyGlobalEditor(BaseEditor):
    pass


class InvalidHandlerPlugin:
    """A class that does not inherit from BaseEditor"""
    pass


class DummyRegistry:
    _handler_profiles: list = []
    _editor_profiles: list = []


@dataclass
class MockVfsNode:
    extension: str = ''
    category: tuple[str, ...] = ()


###----- Fixtures ------###


@pytest.fixture(autouse=True)
def reset_registry():
    """
    Auto-use fixture: Ensures the registry is completely wiped and unlocked before
    and after every test. This prevents state persistence across tests.
    """
    Registry.reset()
    yield
    Registry.reset()


###----- State and Locking ------###


def test_registry_reset_and_lock():
    Registry.register('TestPlugin')(DummyHandler)
    assert len(Registry._handler_profiles) == 1
    Registry.lock()
    assert Registry._locked is True

    with pytest.raises(RuntimeError, match='Locked'):
        Registry.register('LatePlugin')(DummyHandler)

    Registry.reset()
    assert len(Registry._handler_profiles) == 0
    assert Registry._locked is False

def test_registry_reset_clears_all_internal_state():
    Registry.register('TestHandler', extensions=('.a',), categories=('1',))(DummyHandler)
    Registry.register_editor('TestEditor', handler=DummyHandler, is_fallback=True)(DummyEditor)
    Registry.reset()

    assert Registry._handler_profiles == []
    assert Registry._editor_profiles == []
    assert Registry._handler_by_ext == {}
    assert Registry._handler_by_cat == {}
    assert Registry._editor_by_ext == {}
    assert Registry._editor_by_cat == {}
    assert Registry._global_editors == []
    assert Registry._locked is False


###----- Handler registration ------###


def test_handler_registration_success():
    @Registry.register(
        name='TestHandler',
        extensions=('.txt',),
        categories=('Test',),
        supported_actions=(ActionDef('Export Text', ActionType.EXPORT),),
    )
    class TestHandler(DummyHandler):
        pass

    assert len(Registry._handler_profiles) == 1
    profile = Registry._handler_profiles[0]

    assert profile.name == 'TestHandler'
    assert profile.handler_class is TestHandler
    assert profile.extensions == ('.txt',)
    assert profile.categories == ('Test',)

    # Check internal dict routing
    assert len(Registry._handler_by_ext['.txt']) == 1
    assert len(Registry._handler_by_cat['Test']) == 1

    # Check action normalization
    assert len(profile.actions) == 1
    assert profile.actions[0].name == 'Export Text'
    assert profile.actions[0].action_type == ActionType.EXPORT


def test_handler_registration_invalid_type():
    with pytest.raises(TypeError, match='is for BaseHandler subclasses only'):
        Registry.register('BadHandler')(InvalidHandlerPlugin)

def test_discover_handlers_flags_modules_that_registers_nothing(monkeypatch):
    from core.handlers import discover_handlers
    class MockModuleInfo:
        ispkg = False
        name = 'dummy_handler'

    monkeypatch.setattr('pkgutil.iter_modules', lambda path: [MockModuleInfo()])
    monkeypatch.setattr('importlib.import_module', lambda name: None)
    errors = discover_handlers(DummyRegistry)
    assert len(errors) == 1
    assert errors[0][0] == 'core.handlers.dummy_handler'
    assert 'found no profile to register' in str(errors[0][1])

def test_discover_handler_empty_path_is_a_clear_error(monkeypatch):
    from core.handlers import discover_handlers
    monkeypatch.setattr('pkgutil.iter_modules', lambda path: [])
    errors = discover_handlers(DummyRegistry)
    assert len(errors) == 1
    assert 'found no handler modules to import' in str(errors[0][1])

###----- Editor registration ------###


def test_editor_registration_success():
    Registry.register('DummyHandler')(DummyHandler)
    Registry.register_editor(name='TestEditor', handler=DummyHandler, extensions=('.txt',), is_fallback=True)

    @Registry.register_editor(name='TestEditor', handler=DummyHandler, extensions=('.txt',), is_fallback=True)
    class TestEditor(DummyEditor):
        pass

    assert len(Registry._editor_profiles) == 1
    profile = Registry._editor_profiles[0]

    assert profile.name == 'TestEditor'
    assert profile.editor_class is TestEditor
    assert profile.handler_class is DummyHandler
    assert profile.is_fallback is True
    assert profile in Registry._global_editors

def test_editor_registration_invalid_type():
    Registry.register('DummyHandler')(DummyHandler)
    with pytest.raises(TypeError, match='is for BaseEditor subclasses only'):
        Registry.register_editor(name='BadEditor', handler=DummyHandler)(InvalidHandlerPlugin)


def test_editor_registration_invalid_handler_binding():
    with pytest.raises(TypeError, match='handler must be a BaseHandler subclass'):
        Registry.register_editor(name='BadEditor', handler=InvalidHandlerPlugin, extensions=('.txt',))

def test_discover_editors_flags_modules_that_registers_nothing(monkeypatch):
    from ui.editors import discover_editors
    class MockModuleInfo:
        ispkg = False
        name = 'dummy_editor'

    monkeypatch.setattr('pkgutil.iter_modules', lambda path: [MockModuleInfo()])
    monkeypatch.setattr('importlib.import_module', lambda name: None)
    errors = discover_editors(DummyRegistry)
    assert len(errors) == 1
    assert errors[0][0] == 'ui.editors.dummy_editor'
    assert 'found no profile to register' in str(errors[0][1])

def test_discover_editor_empty_path_is_a_clear_error(monkeypatch):
    from ui.editors import discover_editors
    monkeypatch.setattr('pkgutil.iter_modules', lambda path: [])
    errors = discover_editors(DummyRegistry)
    assert len(errors) == 1
    assert 'found no editor modules to import' in str(errors[0][1])


###----- Resolution and Lookups -----###


def test_get_handler_by_node_extension():
    Registry.register('ExtHandler', extensions=('.slz',))(DummyHandler)

    node = MockVfsNode(extension='.slz')
    handler_class = Registry.get_handler(node)
    assert handler_class is DummyHandler


def test_get_handler_by_path():
    Registry.register('PathHandler', extensions=('.slz',))(DummyHandler)

    path = Path('test.slz')
    handler_class = Registry.get_handler(path)
    assert handler_class is DummyHandler


def test_get_handler_profiles_by_category():
    Registry.register('CatHandler', categories=('test',))(DummyHandler)
    Registry.register('CatHandler2', categories=('test',))(DummyFallbackHandler)

    node = MockVfsNode(category=('test',))
    profiles = Registry.get_handler_profiles(node)
    assert profiles is not None
    assert len(profiles) == 2
    assert profiles[0].name == 'CatHandler'
    assert profiles[1].name == 'CatHandler2'


def test_get_editors_ordering():
    # Should be global fallbacks last
    Registry.register('DummyHandler')(DummyHandler)
    Registry.register_editor('GlobalTestEditor', handler=DummyHandler, is_fallback=True)(DummyGlobalEditor)
    Registry.register_editor('SpecificEditor', handler=DummyHandler, extensions=('.txt',))(DummyEditor)
    node = MockVfsNode(extension='.txt')
    editors = Registry.get_editors(node)
    assert len(editors) == 2
    assert editors[0] is DummyEditor
    assert editors[1] is DummyGlobalEditor


def test_get_handler_for_editor():
    Registry.register(
        'TestHandler',
        extensions=('.kods',),
        supported_actions=(ActionDef('Unpack', ActionType.TREE_EXPAND),),
    )(DummyHandler)
    Registry.register_editor('TestEditor', handler=DummyHandler, extensions=('.kods',))(DummyEditor)

    handler_class = Registry.get_handler_for_editor(DummyEditor)
    assert handler_class is DummyHandler
    node = MockVfsNode(extension='.kods')
    action = Registry.get_action(node, 'Unpack')
    assert action is not None
    assert action.name == 'Unpack'
    assert action.action_type == ActionType.TREE_EXPAND

    global_action_name = GLOBAL_ACTIONS[0].name
    action_fallback = Registry.get_action(node, global_action_name)
    assert action_fallback is not None
    assert action_fallback.name == global_action_name


def test_normalise_actions():
    # Test dict conversion
    dict_actions = {'Extract': ActionType.EXPORT}
    result = _normalise_actions(dict_actions)
    assert len(result) == 1
    assert isinstance(result[0], ActionDef)

    # Test tuple pass-through
    tuple_actions = (ActionDef('Replace', ActionType.IMPORT),)
    result_tuple = _normalise_actions(tuple_actions)
    assert result_tuple == tuple_actions
    # Test None
    assert _normalise_actions(None) == ()
    # Test invalid type
    with pytest.raises(TypeError):
        _normalise_actions(['InvalidStringList'])
