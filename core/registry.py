from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional, Union
from pathlib import Path

if TYPE_CHECKING:
    from core.node import VfsNode
    from core.contracts import BaseEditor, BaseHandler

import logging
logger = logging.getLogger(f'radiata.{__name__}')
    
###---------------------------------------- Registry ------------------------------------------------###

@dataclass(frozen=True)
class FormatProfile:
    '''Format Metadata/Logic data'''
    name: str
    handler_class: type[BaseHandler]
    extensions: tuple[str, ...] = ()
    magics: tuple[bytes, ...] = ()
    supported_actions: tuple[str, ...] = ()
    editor_class: type[BaseEditor] | None = None
    categories: tuple[str, ...] = ()
    is_fallback: bool = False

class Registry:
    _profiles: list[FormatProfile] = []

    @classmethod
    def register(
        cls, 
        name: str, 
        extensions: tuple = (), 
        magics: tuple = (), 
        supported_actions: tuple = (), 
        categories: tuple = (), 
        is_fallback: bool = False
    ):
        def decorator(cls_or_func):
            if hasattr(cls_or_func, 'get_file_tree'): # (Handler) for data/tree logic registration
                profile = FormatProfile(
                    name=name,
                    extensions=extensions,
                    magics=magics, # TODO remove or keep and implement in get_profile_for_nore
                    handler_class=cls_or_func,
                    supported_actions=supported_actions,
                    categories=categories,
                    is_fallback=is_fallback
                )
            else:  # (Editor) for editor registration
                from core.handlers.generic_binary_handler import GenericBinaryHandler
                profile = FormatProfile(
                    name=name,
                    extensions=extensions,
                    magics=magics, # TODO remove or keep and implement in get_profile_for_nore
                    handler_class=GenericBinaryHandler,
                    editor_class=cls_or_func,
                    supported_actions=supported_actions,
                    categories=categories,
                    is_fallback=is_fallback
                )
            cls._profiles.append(profile)
            logger.debug(f'[REGISTRY] Register {name}')
            return cls_or_func
        return decorator

    @classmethod
    def get_profile(cls, node: VfsNode) -> Optional[FormatProfile]:
        '''Return a node's profile'''
        if node.extension:  # Check for extension match
            for p in cls._profiles:
                if node.extension in p.extensions:
                    return p
        if node.category != 'Unknown': # Check for category match
            for p in cls._profiles:
                if node.category in p.categories:
                    return p
        return None

    @classmethod
    def get_handler(cls, source: Union[VfsNode, Path]) -> Optional[type[BaseHandler]]:
        '''Return handler class for source type'''
        if isinstance(source, Path):
            logger.debug(f'Attempting to get physical handler for {source.name}')
            suffix = source.suffix.lower()
            for profile in cls._profiles:
                if suffix in profile.extensions:
                    return profile.handler_class
        else:
            logger.debug(f'Attempting to get handler for node {source.name}')
            profile = cls.get_profile(source)
            if profile:
                logger.debug(f'Found handler {profile.handler_class.__name__}')
                return profile.handler_class
        logger.warning('No handler found...')
        return None

    @classmethod
    def get_editor(cls, node: VfsNode) -> type[BaseEditor]:
        '''Return editor widgets'''
        profile = cls.get_profile(node)
        if profile and profile.editor_class:
            return profile.editor_class

        from plugins.hex_editor import HexEditorWidget
        return HexEditorWidget

