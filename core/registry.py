from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from pathlib import Path
from core.contracts import BaseEditor, BaseHandler
from core.workers import ActionDef, ActionType

if TYPE_CHECKING:
    from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###-------------------------------------- Global Actions ------------------------------------------###

GLOBAL_ACTIONS: dict[str, ActionDef] = {
    'Export': ActionDef(name='Export', action_type=ActionType.EXPORT, title='Export Node'),
    'Import': ActionDef(name='Import', action_type=ActionType.IMPORT, title='Import Node'),
}

###---------------------------------------- Registry ------------------------------------------------###

@dataclass(frozen=True)
class FormatProfile:
    '''Format Metadata/Logic data'''
    name: str
    handler_class: type[BaseHandler]
    extensions: tuple[str, ...] = ()
    actions: dict[str, ActionDef] = field(default_factory=dict)
    editor_class: type[BaseEditor] | None = None
    categories: tuple[str, ...] = ()
    is_fallback: bool = False

    def primary_expand_action(self) -> ActionDef | None:
        '''Return TREE_EXPAND action for the defined format'''
        return next(
            (a for a in self.actions.values() if a.action_type == ActionType.TREE_EXPAND), 
            None
        )

    def get_action(self, name: str) -> ActionDef | None:
        '''Look up specific action by name'''
        return self.actions.get(name) or GLOBAL_ACTIONS.get(name)

class Registry:
    _profiles:     list[FormatProfile] = []
    _by_extension: dict[str, FormatProfile] = {}
    _by_category:  dict[str, FormatProfile] = {}

    @classmethod
    def register(
        cls, 
        name:             str, 
        extensions:       tuple[str, ...] = (), 
        supported_actions: dict[str, ActionDef] | tuple[str, ...] | None = None,
        categories:       tuple[str, ...] = (), 
        is_fallback:      bool = False
    ):
        '''Decorator for handlers and editors. '''
        def decorator(cls_or_func):
            actions = _normalise_actions(supported_actions)

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
            cls._profiles.append(profile)
            # build lookup dicts
            for ext in extensions:
                if ext not in cls._by_extension or not profile.is_fallback:
                    cls._by_extension[ext] = profile
            for cat in categories:
                if cat not in cls._by_category or not profile.is_fallback:
                    cls._by_category[cat] = profile

            logger.debug(f'[REGISTRY] Register {name}')
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
            if profile:
                return profile.handler_class
            for profile in cls._profiles:
                if suffix in profile.extensions and profile.is_fallback:
                    return profile.handler_class
            return None
        else:
            profile = cls.get_profile(source)
            return profile.handler_class if profile else None

    @classmethod
    def get_editor(cls, node: VfsNode) -> type[BaseEditor] | None:
        '''Return editor widgets'''
        profile = cls.get_profile(node)
        if profile and profile.editor_class:
            return profile.editor_class
        fallback = next((p for p in cls._profiles if p.is_fallback and p.editor_class), None)
        return fallback.editor_class if fallback else None
    
    @classmethod
    def get_action(cls, node: 'VfsNode', action_name: str) -> ActionDef | None:
        '''Resolve ActionDef from action_name for a given node. 
        Checks node's profile first falling back to global actions.'''
        profile = cls.get_profile(node)
        if profile:
            action = profile.actions.get(action_name)
            if action:
                return action
        return GLOBAL_ACTIONS.get(action_name)

###---------------------------------------- Helpers-----------------------------------------###

def _normalise_actions(actions: dict[str, ActionDef] | tuple[str, ...] | None) -> dict[str, ActionDef]:
    '''For converting legacy tuple and dicts into the same data type'''
    if not actions:
        return {}
    if isinstance(actions, dict):
        for key, val in actions.items():
            if not isinstance(val, ActionDef):
                raise TypeError(f'supported_actions values must be ActionDef instances, got {type(val).__name__!r} for key {key!r}')
        return actions
    return {
        name: ActionDef(name=name, action_type=ActionType.PROCESS, title=name) 
        for name in actions
    }