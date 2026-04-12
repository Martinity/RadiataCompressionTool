from dataclasses import dataclass
from typing import Type, TYPE_CHECKING, Optional, Union
from pathlib import Path

if TYPE_CHECKING:
    from core.node import VfsNode
    from core.contracts import BaseEditorWidget, BaseHandler

import logging
logger = logging.getLogger('radiata')
    
###---------------------------------------- Registry ------------------------------------------------###

@dataclass(frozen=True)
class FormatProfile:
    '''Format Metadata/Logic data'''
    name: str
    handler_class: Type['BaseHandler']
    extensions: tuple[str, ...] = ()
    magics: tuple[bytes, ...] = ()
    editor_class: Type['BaseEditorWidget'] | None = None
    categories: tuple[str, ...] = ()
    is_fallback: bool = False

class Registry:
    _profiles: list[FormatProfile] = []

    @classmethod
    def register(cls, name: str, extensions: tuple = (), magics: tuple = (), categories: tuple = (), is_fallback: bool = False):
        def decorator(cls_or_func):
            if hasattr(cls_or_func, 'get_file_tree'): # for data/tree logic registration
                profile = FormatProfile(
                    name=name,
                    extensions=extensions,
                    magics=magics, # TODO remove or keep and implement in get_profile_for_nore
                    handler_class=cls_or_func,
                    categories=categories,
                    is_fallback=is_fallback
                )
            else:                                   # for editor registration
                from core.handlers.generic_binary_handler import GenericBinaryHandler
                profile = FormatProfile(
                    name=name,
                    extensions=extensions,
                    magics=magics,
                    handler_class=GenericBinaryHandler,
                    editor_class=cls_or_func,
                    categories=categories,
                    is_fallback=is_fallback
                )
            cls._profiles.append(profile)
            logger.debug(f'[REGISTRY] Register {name}')
            return cls_or_func
        return decorator

    @classmethod
    def get_profile_for_node(cls, node: 'VfsNode') -> Optional[FormatProfile]:
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
    def get_handler_class_for(cls, target: Union['VfsNode', Path]) -> Type['BaseHandler'] | None:
        if isinstance(target, Path): # is path
            return cls._get_handler_for_physical_file(target)
        # is node
        profile = cls.get_profile_for_node(target)
        if profile:
            return profile.handler_class

        logger.warning(f'No explicit handler for {target.name}...')
        return None

    @classmethod
    def _get_handler_for_physical_file(cls, path: Path) -> Type['BaseHandler'] | None:
        '''Identifies physical files, used to get ISO (root)'''
        suffix = path.suffix.lower()
        with open(path, 'rb') as f:
            header = f.read(32)

        for profile in cls._profiles:
            if suffix in profile.extensions or any(header.startswith(m) for m in profile.magics):
                return profile.handler_class

        return None

    @classmethod
    def get_editor_for(cls, node: 'VfsNode') -> Type['BaseEditorWidget']:
        profile = cls.get_profile_for_node(node)
        if profile and profile.editor_class:
            return profile.editor_class

        from plugins.hex_editor import HexEditorWidget
        return HexEditorWidget

