from pathlib import Path
from core.registry import Registry
from core.node import VfsManager
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from core.node import VfsNode
    from core.contracts import BaseHandler

import logging
logger = logging.getLogger(f'radiata.{__name__}')


###----------------------------------------------------- Dispatch -------------------------------------------------###

class Dispatcher:
    '''Bridge between UI and logic'''
    def __init__(self):
        self.vfs: Optional[VfsManager] = None
        self.active_handler: Optional['BaseHandler'] = None
        # cache for virtual files
        self._buffer_cache: dict[str, bytes] = {} # Format: [hid, bytes]
        self.cache_limit = 6

    def __str__(self) -> str:
        return f"Dispatcher(active_handler={self.active_handler})"

    def load_source(self, source: Union[Path, 'VfsNode']) -> tuple[Optional['VfsNode'], Optional[str]]:
        '''Load Format handler from predefined formats in class FormatProfile'''
        if isinstance(source, Path): # Mount ISO
            data_source = source
            parent_node = None
            logger.info(f'Mounting Root: {source.name}')
        else: # Expand container
            data_source = self.read_node_bytes(source)
            parent_node = source
            logger.info(f'Expanding nested archive: {source.name}')

        handler_class = Registry.get_handler_class_for(source)
        if not handler_class:
            return None, None
        
        handler = handler_class(data_source, parent_node)
        root_node = handler.get_file_tree()
        identity = handler.get_identity()

        if isinstance(source, Path): # New root initialize vfs manager
            if self.active_handler:
                self.active_handler.close()
            self.active_handler = handler
            self.vfs = VfsManager(root_node)
            logger.info(f'Workspace reset. Root: {identity}')
        else: # Expand the vfs
            for child in root_node.children:
                source.append_child(child)
                self.vfs.register_node(child, child.offset, child.is_physical)
            logger.info(f'Inserted {len(root_node.children)} nodes into {source.name}')

        return root_node, identity

    def get_node_data(self, node: 'VfsNode') -> bytes:
        # Get edits
        if node.pending_data:
            return node.pending_data

        # Has physical address
        return self.active_handler.process_node(node, node.offset)
        

    def execute_node_action(self, node: 'VfsNode', action_name: str):
        '''Route action to format handler'''
        handler_class = Registry.get_handler_class_for(node)

        if handler_class:
            logger.debug(f'Routing "{action_name}" to {handler_class.__name__}')
            node_bytes = self.get_node_data(node)
            with handler_class(node_bytes, node.parent) as temp_handler:
                if hasattr(temp_handler, 'execute_action'):
                    return temp_handler.execute_action(node, action_name)
                else:
                    logger.warning(f'{handler_class.__name__} is missing execute_action')
            return

        logger.warning(f'No handler found for action: {action_name}')


        # # ISO level Action
        # if node.parent == self.vfs.root or node.parent is None or node.is_physical:
        #     if hasattr(self.active_handler, 'execute_action'):
        #         self.active_handler.execute_action(node, action_name)
        #     return
        
        # # Virtual node action
        # provider = node if node.is_physical else node.parent
        # while provider.parent and provider.parent.is_physical:
        #     provider = provider.parent

        # handler_class = Registry.get_handler_class_for(provider)
        # if not handler_class:
        #     logger.warning(f'No registered handler class for {provider.name}')
        #     return
        
        # provider_buffer = self.get_node_data(provider)

        # with handler_class(provider_buffer, provider.parent) as temp_handler:
        #     if hasattr(temp_handler, 'execute_action'):
        #         temp_handler.execute_action(node, action_name)
        #     else:
        #         logger.warning(f'{handler_class.__name__} cannot')

        # if self.active_handler and hasattr(self.active_handler, 'execute_action'):
        #     self.active_handler.execute_action(node, action_name)
        # else:
        #     logger.warning(f'No handler for action: {action_name}')