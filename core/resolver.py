
from typing import TYPE_CHECKING
from core.registry import Registry

if TYPE_CHECKING:
    from core.node import VfsNode
###------------------------------------------ Resolvers --------------------------------------------------###

class ActionResolver:
    @staticmethod
    def get_supported_actions(node: 'VfsNode') -> list[str]:
        '''Return supported actions for node'''
        profile = Registry.get_profile_for_node(node)

        actions = ['Properties'] # Basic global actions

        if profile:
            if hasattr(profile.handler_class, 'get_supported_actions'):
                actions.extend(profile.handler_class.get_supported_actions())

        return list(set(actions))