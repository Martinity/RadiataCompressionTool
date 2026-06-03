'''
Registry; the global lookup for all handlers and editors, and their purposes
Registration happens at startup catching errors before runtime and locks preventing runtime mutations

HandlerProfile, EditorProfile are created for @Registry.register, @Registry.register_editor respectively
FormatResolver features the lookup API
'''
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from pathlib import Path
from core.contracts import BaseEditor, BaseHandler
from core.workers import ActionDef, ActionType

from core.handlers import discover_handlers
from ui.editors import discover_editors

if TYPE_CHECKING:
    from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###-------------------------------------- Globals ------------------------------------------###

GLOBAL_ACTIONS: tuple[ActionDef, ...] = (
    ActionDef('Export as Raw Bytes', ActionType.EXPORT),
    ActionDef('Import and Replace', ActionType.IMPORT),
)
_GLOBAL_ACTIONS_BY_NAME: dict[str, ActionDef] = {a.name: a for a in GLOBAL_ACTIONS}

###------------------------------------ Format Resolver --------------------------------------------###

@runtime_checkable
class FormatResolver(Protocol):
    '''Inject FormatResolver rather than importing Registry directly to decouple subsystems.'''
    def get_handler_profile(self, node: 'VfsNode') -> 'HandlerProfile | None': ...
    def get_editor_profile(self, editor_class: 'type[BaseEditor]') -> 'type[EditorProfile] | None': ...
    def get_handler(self, source: 'VfsNode | Path') -> 'type[BaseHandler] | None': ...
    def get_editors(self, node: 'VfsNode') -> 'list[type[BaseHandler]]': ...
    def get_action(self, node: 'VfsNode', action_name: str) -> 'ActionDef | None': ...
    def get_handler_for_editor(self, editor: 'type[BaseEditor]') -> 'type[BaseHandler] | None': ...

###---------------------------------------- Format Profiles ------------------------------------------------###

@dataclass(frozen=True)
class HandlerProfile:
    '''one profile to one handler. Owns extensions, categories, actions. everything needed for processing a node'''
    name:          str
    handler_class: type[BaseHandler]
    extensions:    tuple[str, ...]
    categories:    tuple[str, ...] = ()
    actions:       tuple[ActionDef, ...] = ()
    is_fallback:   bool = False

    _action_map: dict[str, ActionDef] = field(
        default_factory=dict, init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        '''builds the 0(1) lookup'''
        object.__setattr__(self, '_action_map', {a.name: a for a in self.actions})

    def get_action(self, name: str) -> ActionDef | None:
        return self._action_map.get(name)
    
    def primary_expand_action(self) -> ActionDef | None:
        return next(
            (a for a in self.actions if a.action_type == ActionType.TREE_EXPAND),
            None
        )

@dataclass(frozen=True)
class EditorProfile:
    '''one profile to one editor. Owns editor class, pairs handler, dispay metadata'''
    name:          str
    handler_class: type[BaseHandler]
    editor_class:  type[BaseEditor]
    extensions:    tuple[str, ...] = ()
    categories:    tuple[str, ...] = ()
    is_fallback:   bool = False
    
###------------------------------------------ Registry --------------------------------------------###

class Registry:
    '''
    Central format service locator. Supports two kinds of resolutions.
    Node resolution      get_profile(node) / get_handler(node)
    Editor resolution    get_handler_for_editor(editor)
    '''
    _handler_profiles: list[HandlerProfile] = []
    _editor_profiles:  list[EditorProfile] = []
    _handler_by_ext:   dict[str, HandlerProfile] = {}
    _handler_by_cat:   dict[str, HandlerProfile] = {}
    _editor_by_ext:    dict[str, list[EditorProfile]] = {}
    _editor_by_cat:    dict[str, list[EditorProfile]] = {}
    _global_editors:   list[EditorProfile] = []
    _locked:       bool = False

    @classmethod
    def lock(cls) -> None:
        '''freeze the registry. called after all plugins are loaded'''
        cls._locked = True
        logger.info(
            f'Locked -- {len(cls._handler_profiles)} handler(s), {len(cls._editor_profiles)} editor(s)'
            f'{len(cls._global_editors)} global editor(s)'
        )
        logger.debug(cls.summary())

    @classmethod
    def reset(cls) -> None:
        '''for testing'''
        cls._handler_profiles.clear()
        cls._editor_profiles.clear()
        cls._handler_by_ext.clear()
        cls._handler_by_cat.clear()
        cls._editor_by_ext.clear()
        cls._editor_by_cat.clear()
        cls._global_editors.clear()
        cls._locked = False

    ###----------------------------------- register ------------------------------------------###
    @classmethod
    def register(
        cls, 
        name:              str, 
        extensions:        tuple[str, ...] = (), 
        categories:        tuple[str, ...] = (), 
        supported_actions: tuple[ActionDef, ...] | dict[str, ActionType] | None = None,
        is_fallback:       bool = False,
    ):
        '''
        Decorator for BaseHandler subclasses. 
        
        supported_actions accepts:
            tuple[ActionDef, ...]   preferred ActionDef
            dict[str, ActionType]   shorthand get converted to ActionDef tuple
            None                    no format specific actions
        '''
        def decorator(cls_or_func):
            if cls._locked:
                raise RuntimeError(
                    f'Locked - Cannot register "{name}" after discover_all() has completed'
                    f'Ensure all plugins are imported inside discover_all()'
                )
            if not issubclass(cls_or_func, BaseHandler):
                raise TypeError(
                    f'@Registry.register is for BaseHandler subclasses only. Use @Registry.register_editor for BaseEditor. '
                    f'{cls_or_func.__name__!r} is not a BaseHandler.'
                )
            actions = _normalise_actions(supported_actions)
            cls_or_func._plugin_name = name # type: ignore BaseHandler checks _plugin_name for get_identity checks

            profile = HandlerProfile(
                name=name,
                handler_class=cls_or_func,
                extensions=extensions,
                categories=categories,
                actions=actions,
                is_fallback=is_fallback,
            )
            cls._handler_profiles.append(profile)

            for ext in extensions:
                if ext not in cls._handler_by_ext or not profile.is_fallback:
                    cls._handler_by_ext[ext] = profile
            for cat in categories:
                if cat not in cls._handler_by_cat or not profile.is_fallback:
                    cls._handler_by_cat[cat] = profile

            return cls_or_func
        return decorator
    
    @classmethod
    def register_editor(
            cls,
            name:        str,
            handler:     type[BaseHandler],
            extensions:  tuple[str, ...] = (),
            categories:  tuple[str, ...] = (),
            is_fallback: bool = False,
    ):
        '''
        Decorator for BaseEditor subclasses
        
        handler= is required and is the actual handler class
        '''
        def decorator(cls_or_func):
            if cls._locked:
                raise RuntimeError(
                    f'Locked -- cannot register editor "{name}" after discover_all()'
                )
            if not issubclass(cls_or_func, BaseEditor):
                raise TypeError(
                    f'@Registry.register_editor is for BaseEditor subclasses only. '
                    f'{cls_or_func.__name__!r} is not a BaseEdtor.'
                )
            if not (isinstance(handler, type) and issubclass(handler, BaseHandler)):
                raise TypeError(
                    f'register_editor "{name}": handler= must be a BaseHandler subclass, got {handler!r}.'
                )
            cls_or_func._plugin_name = name # type: ignore BaseHandler checks _plugin_name for get_identity checks
            profile = EditorProfile(
                name=name, 
                handler_class=handler,
                editor_class=cls_or_func,
                extensions=extensions,
                categories=categories,
                is_fallback=is_fallback,
            )
            cls._editor_profiles.append(profile)
            if is_fallback:
                cls._global_editors.append(profile)

            for ext in extensions:
                cls._editor_by_ext.setdefault(ext, []).append(profile)
            for cat in categories:
                cls._editor_by_cat.setdefault(cat, []).append(profile)

            return cls_or_func
        return decorator


    ###------------------------------- Lookups ----------------------------------###
    @classmethod
    def get_handler_profile(cls, node: VfsNode) -> HandlerProfile | None:
        '''Returns best handler profile for a node'''
        fallback: HandlerProfile | None = None
        if node.extension:
            p = cls._handler_by_ext.get(node.extension)
            if p:
                if p.is_fallback:
                    fallback = fallback or p
                else:
                    return p
        if node.category:
            for cat in node.category:
                if cat == 'Unknown':
                    continue
                p = cls._handler_by_cat.get(cat)
                if p:
                    if p.is_fallback:
                        fallback = fallback or p
                    else:
                        return p
        return fallback

    @classmethod
    def get_handler(cls, source: VfsNode | Path) -> type[BaseHandler] | None:
        '''Return handler class for source type'''
        if isinstance(source, Path):
            p = cls._handler_by_ext.get(source.suffix.lower())
            return p.handler_class if p else None
        profile = cls.get_handler_profile(source)
        return profile.handler_class if profile else None

    @classmethod
    def get_editors(cls, node: VfsNode) -> list[type[BaseEditor]]:
        '''Return all valid editors for a node, ordered by fallbacks last'''
        seen:    set[type[BaseEditor]] = set()
        editors: list[type[BaseEditor]] = []

        def _add(profile: EditorProfile) -> None:
            if profile.editor_class not in seen:
                seen.add(profile.editor_class)
                editors.append(profile.editor_class)

        if node.extension:
            for p in cls._editor_by_ext.get(node.extension, []):
                _add(p)
        if node.category:
            for cat in node.category:
                if cat == 'Unknown':
                    continue
                for p in cls._editor_by_cat.get(cat, []):
                    _add(p)
        for p in cls._global_editors:
            _add(p)
        return editors
    
    @classmethod
    def get_editor_profile(cls, editor_class: type[BaseEditor]) -> EditorProfile | None:
        '''Return the EditorProfile associated with a specific editor'''
        return next(
            (p for p in cls._editor_profiles if p.editor_class is editor_class),
            None,
        )
    
    @classmethod
    def get_handler_for_editor(cls, editor: 'BaseEditor') -> type[BaseHandler] | None:
        '''Return the handler declared by an editor's profile'''
        profile = cls.get_editor_profile(editor.__class__)
        if not profile:
            logger.warning(f'{editor.__class__.__name__} has no EditorProfile - falling back to node handler')
            return None
        return profile.handler_class
    
    @classmethod
    def get_action(cls, node: 'VfsNode', action_name: str) -> ActionDef | None:
        '''Resolve ActionDef from action_name for a given node. 
        Checks node's HandlerProfile first falling back to global actions.'''
        profile = cls.get_handler_profile(node)
        if profile:
            action = profile.get_action(action_name)
            if action:
                return action
        return _GLOBAL_ACTIONS_BY_NAME.get(action_name)
    
    ###----------------------------------- Diagnostics ---------------------------------------###
    @classmethod
    def summary(cls) -> str:
        '''human-readable registration summary'''
        lines = [f'Registry (locked={cls._locked})']
        lines.append(f'  Handlers ({len(cls._handler_profiles)}):')
        for p in cls._handler_profiles:
            actions_str  = ', '.join(a.name for a in p.actions) or '-'
            lines.append(
                f'    {p.name!r:40s} - {p.handler_class.__name__:30s} ext={p.extensions} actions=[{actions_str}]'
            )
        lines.append(f'  Editors ({len(cls._editor_profiles)}):')
        for p in cls._editor_profiles:
            fallback = ' [global]' if p.is_fallback else ''
            lines.append(
                f'    {p.name!r:40s} - {p.editor_class.__name__:30s} handler={p.handler_class.__name__} '
                f'ext={p.extensions} role={p.editor_class!r}{fallback}'
            )
        return '\n'.join(lines)

###---------------------------------------- Helpers-----------------------------------------###

def _normalise_actions(actions: tuple[ActionDef, ...] | dict[str, ActionType] | None) -> tuple[ActionDef, ...]:
    '''
    Convert supported_actions to a uniform tuple[ActionDef]
    tuple[ActionDef, ...]  returned as-is after validated
    dict[str, ActionType]  key/values become ActionDef[key, value]
    None                   returns empty tuple
    '''
    if not actions:
        return ()
    if isinstance(actions, (tuple, list)):
        for item in actions:
            if not isinstance(item, ActionDef):
                raise TypeError(f'supported_actions items must be ActionDef, got {type(item).__name__!r}')
        return tuple(actions)
    if isinstance(actions, dict):
        return tuple(ActionDef(name=k, action_type=v) for k, v in actions.items())
    raise TypeError(
        f'supported_actions must be tuple[ActionDef] or dict[str, ActionType], got {type(actions).__name__!r}'
    )


def discover_all() -> None:
    '''Import all handlers/editors and lock the registry'''
    discover_handlers()
    discover_editors()
    Registry.lock()
    logger.info('Registry filled and locked.')