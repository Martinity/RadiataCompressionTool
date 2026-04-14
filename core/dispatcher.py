from __future__ import annotations

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
    def __init__(self) -> None:
        self.vfs: Optional[VfsManager] = None
        self.active_handler: Optional[BaseHandler] = None
        # cache for virtual files
        self._buffer_cache: dict[str, bytes] = {} # Format: [hid, bytes]
        self.cache_limit = 6

    def __str__(self) -> str:
        return f"Dispatcher(active_handler={self.active_handler})"

    def load_source(self, source: Union[Path, VfsNode]) -> list[VfsNode]:
        '''Get handler class -> VfsNode(s) -> VfsManager'''
        # Handler
        handler_class = Registry.get_handler(source)
        if not handler_class:
            logger.warning(f'No handler for {source.name}')
            return []
        
        # Setup for Handler instance
        if isinstance(source, Path):
            data_source = source
            parent_node = None
        else:
            data_source = self.get_node_data(source)
            parent_node = source

        # Physical node
        if isinstance(source, Path):
            if self.active_handler:
                self.active_handler.close()
            
            self.active_handler = handler_class(data_source, parent_node)
            root_node = self.active_handler.get_file_tree()
            identity = self.active_handler.get_identity()

            # Create new VfsManager
            self.vfs = VfsManager(root_node)
            logger.info(f'Workspace initialized with Root: {identity}')
            return [root_node]
        
        # Virtual node
        else:
            with handler_class(data_source, parent_node) as temp_handler:
                draft_nodes = temp_handler.get_file_tree()
                identity = temp_handler.get_identity()
                new_nodes = draft_nodes.children if draft_nodes.children else [draft_nodes]
                # Register to existing VfsManager
                for node in new_nodes:
                    if self.vfs:
                        self.vfs.register_node(node, node.offset)
                
                logger.info(f'Inserted {len(new_nodes)} nodes from {source.name} ({identity})')
                return new_nodes

    def get_node_data(self, node: VfsNode) -> bytes:
        # Get edits
        if node.pending_data:
            return node.pending_data

        if self.active_handler is None:
            logger.error('Physical Handler not found. Either ISO has not yet been initialized or handler was closed preemptively.')
            return b''

        # Has physical address
        return self.active_handler.get_raw_node(node)
        
    def execute_node_action(self, node: VfsNode, action_name: str) -> None:
        '''Route action to format handler'''
        handler_class = Registry.get_handler(node)
        if not handler_class:
            logger.warning(f'No handler found for action "{action_name}" on {node.name}')
            return

        logger.debug(f'Routing "{action_name}" to {handler_class.__name__}')
        node_bytes = self.get_node_data(node)
        with handler_class(node_bytes, node.parent) as temp_handler:
            if hasattr(temp_handler, 'execute_action'):
                temp_handler.execute_action(node, action_name)
            else:
                logger.warning(f'{handler_class.__name__} is missing execute_action')
