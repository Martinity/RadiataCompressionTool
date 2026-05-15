from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from pathlib import Path
from core.contracts import BaseEditor, BaseHandler
from core.workers import ActionDef, ActionType

if TYPE_CHECKING:
    from core.node import VfsNode

###-------------------------------------- Globals ------------------------------------------###

GLOBAL_ACTIONS: tuple[ActionDef, ...] = (
    ActionDef('Export', ActionType.EXPORT),
    ActionDef('Import', ActionType.IMPORT),
)
_GLOBAL_ACTIONS_BY_NAME: dict[str, ActionDef] = {a.name: a for a in GLOBAL_ACTIONS}

###---------------------------------------- Registry ------------------------------------------------###

@dataclass(frozen=True)
class FormatProfile:
    '''
    Format Metadata/Logic data for all handler/editors
    actions  Tuple of ActionDef, name (self-identifying) is the key 
             A lookup dict is built in __post_init__ for 0(1) access
    '''
    name:          str
    handler_class: type[BaseHandler]
    extensions:    tuple[str, ...] = ()
    actions:       tuple[ActionDef, ...] = ()
    editor_class:  type[BaseEditor] | None = None
    categories:    tuple[str, ...] = ()
    is_fallback:   bool = False

    _action_map: dict[str, ActionDef] = field(
        default_factory=dict, init=False, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, '_action_map', {a.name: a for a in self.actions})

    def get_action(self, name: str) -> ActionDef | None:
        '''Look up specific action by name'''
        return self._action_map.get(name)

    def primary_expand_action(self) -> ActionDef | None:
        '''Return TREE_EXPAND action for the defined format'''
        return next(
            (a for a in self.actions if a.action_type == ActionType.TREE_EXPAND), 
            None
        )

class Registry:
    _profiles:     list[FormatProfile] = []
    _by_extension: dict[str, FormatProfile] = {}
    _by_category:  dict[str, FormatProfile] = {}
    _editors:      list[type[BaseEditor]] = []

    @classmethod
    def register(
        cls, 
        name:              str, 
        extensions:        tuple[str, ...] = (), 
        supported_actions: tuple[ActionDef, ...] | dict[str, ActionType] | None = None,
        categories:        tuple[str, ...] = (), 
        is_fallback:       bool = False
    ):
        '''
        Decorator for handlers and editors. 
        
        supported_actions accespts:
            tuple[ActionDef, ...]   preferred ActionDef
            dict[str, ActionType]   shorthand get converted to ActionDef tuple
            None                    no format specific actions
        '''
        def decorator(cls_or_func):
            actions = _normalise_actions(supported_actions)

            cls_or_func._plugin_name = name # stamps the class with the registered name used for identity

            if issubclass(cls_or_func, BaseHandler): # (Handler) for data/tree logic registration
                profile = FormatProfile(
                    name=name,
                    extensions=extensions,
                    handler_class=cls_or_func,
                    actions=actions,
                    categories=categories,
                    is_fallback=is_fallback
                )
            else:  # (Editor) for editor registration
                from core.handlers.generic_binary_handler import GenericBinaryHandler
                profile = FormatProfile(
                    name=name,
                    extensions=extensions,
                    handler_class=GenericBinaryHandler,
                    editor_class=cls_or_func,
                    actions=actions,
                    categories=categories,
                    is_fallback=is_fallback
                )
                if is_fallback and cls_or_func not in cls._editors:
                    cls._editors.append(cls_or_func)
            cls._profiles.append(profile)
            # build lookup dicts
            for ext in extensions:
                if ext not in cls._by_extension or not profile.is_fallback:
                    cls._by_extension[ext] = profile
            for cat in categories:
                if cat not in cls._by_category or not profile.is_fallback:
                    cls._by_category[cat] = profile

            return cls_or_func
        return decorator

    ###------------------------------- Lookups ----------------------------------###
    @classmethod
    def get_profile(cls, node: VfsNode) -> FormatProfile | None:
        '''Return a node's profile'''
        fallback: FormatProfile | None = None
        if node.extension:  # Check for extension match
            profile = cls._by_extension.get(node.extension)
            if profile:
                if profile.is_fallback:
                    fallback = fallback or profile
                else:
                    return profile
        if node.category and node.category != 'Unknown': # Check for category match
            profile = cls._by_category.get(node.category)
            if profile:
                if profile.is_fallback:
                    fallback = fallback or profile
                else:
                    return profile
        return fallback

    @classmethod
    def get_handler(cls, source: VfsNode | Path) -> type[BaseHandler] | None:
        '''Return handler class for source type'''
        if isinstance(source, Path):
            suffix = source.suffix.lower()
            profile = cls._by_extension.get(suffix)
            return profile.handler_class if profile else None
        profile = cls.get_profile(source)
        return profile.handler_class if profile else None

    @classmethod
    def get_editors(cls, node: VfsNode) -> list[type[BaseEditor]]:
        '''Return all valid editor widgets for a node.'''
        editors: list[type[BaseEditor]] = []
        profile = cls.get_profile(node) # format specific editors
        if profile and profile.editor_class:
            editors.append(profile.editor_class)
        for editor in cls._editors: # global editors
            if editor not in editors:
                editors.append(editor)
        return editors
    
    @classmethod
    def get_editor_profile(cls, editor_class: type[BaseEditor]) -> FormatProfile | None:
        '''Return the FormatProfile associated with a specific editor'''
        return next(
            (profile for profile in cls._profiles if profile.editor_class is editor_class),
            None,
        )
    
    @classmethod
    def get_action(cls, node: 'VfsNode', action_name: str) -> ActionDef | None:
        '''Resolve ActionDef from action_name for a given node. 
        Checks node's profile first falling back to global actions.'''
        profile = cls.get_profile(node)
        if profile:
            action = profile.get_action(action_name)
            if action:
                return action
        return _GLOBAL_ACTIONS_BY_NAME.get(action_name)

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