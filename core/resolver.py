from __future__ import annotations

from typing import TYPE_CHECKING
from core.registry import Registry

if TYPE_CHECKING:
    from core.node import VfsNode
###------------------------------------------ Resolvers --------------------------------------------------###

class ActionResolver:
    '''Resolves the actions available for a node.'''
    @staticmethod
    def get_supported_actions(node: VfsNode) -> list[str]:
        '''Return supported actions for node'''
        profile = Registry.get_profile(node)

        if profile:
            return list(set(profile.supported_actions))
        
        return ['Properties'] # Global handler action.