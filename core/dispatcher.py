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
        # cache for editors to keep nodes open TODO
        self.editor_cache: dict[str, bytes] = {} # Format: [hid, bytes]

    def __str__(self) -> str:
        return f"Dispatcher(active_handler={self.active_handler})"

    def load_source(self, source: Union[Path, VfsNode]) -> list[VfsNode]:
        '''Get handler class -> VfsNode(s) -> VfsManager'''
        # Handler
        handler_class = Registry.get_handler(source)
        if not handler_class:
            logger.warning(f'No handler for {source.name}')
            return []

        if isinstance(source, Path): # Physical node
            return self._load_physical(handler_class, source)
        else: # Virtual node
            return self._load_virtual(handler_class, source)

    def get_node_data(self, node: 'VfsNode') -> bytes:
        '''Return the raw bytes of the requested node by unwrapping from the physical layer (to virtual node)'''
        if node.pending_data is not None:
            return node.pending_data

        chain = self._build_unwrap_chain(node)
        if not chain:
            logger.warning(f'No physical reference point for node {node.hierarchical_id_str}')
        logger.debug(f'Resolving data for {node.hierarchical_id_str}')
        return self._unwrap_chain(chain)

    def execute_node_action(self, node: VfsNode, action_name: str) -> None:
        '''Route action to format handler'''
        handler_class = Registry.get_handler(node)
        if not handler_class:
            logger.warning(f'No handler found for action "{action_name}" on {node.name}')
            return

        logger.debug(f'Routing "{action_name}" to {handler_class.__name__}')
        node_bytes = self.get_node_data(node)

        header_bytes = []
        if getattr(node, 'target', None) and self.vfs:
            logger.debug(f'Action requires datacenter headers, resolving {len(node.target)} headers')
            header_nodes = self.vfs.resolve_nodes(node.target, expansion_callback=self._expand_node)
            header_bytes = [self.get_node_data(h) for h in header_nodes]

        with handler_class(node_bytes, node.parent) as temp_handler:
            if header_bytes and hasattr(temp_handler, 'datacenter_headers'):
                temp_handler.datacenter_headers = header_bytes
            if hasattr(temp_handler, 'execute_action'):
                temp_handler.execute_action(node, action_name)
            else:
                logger.warning(f'{handler_class.__name__} is missing execute_action')

    def close(self) -> None:
        '''For exiting the dispatch'''
        if self.active_handler:
            self.active_handler.close()
        self.editor_cache.clear()
        self.vfs = None
        self.active_handler = None
        logger.debug('- Dispatcher state reset -')

    ###------------------------------ Helpers --------------------------------###

    def _load_physical(self, handler_class, path: Path) -> list[VfsNode]:
        '''helper for loading physical files'''
        if self.active_handler:
            self.active_handler.close()

        handler = handler_class(path, None)
        self.active_handler = handler

        root = handler.get_file_tree()
        identity = handler.get_identity()

        self.vfs = VfsManager(root)
        logger.info(f'Workspace initialized with Root: {identity}')

        return [root]

    def _load_virtual(self, handler_class, node: VfsNode) -> list[VfsNode]:
        '''helper for loading virtual files, these files need to have passed through a physical handler first'''
        container_bytes = self.get_node_data(node)

        with handler_class(container_bytes, node) as handler:
            if getattr(node, 'target', None) and hasattr(handler, 'unpack_with_headers') and self.vfs:
                logger.info(f'Datacenter unpack for {node.name} - resolving {len(node.target)} headers HID:{node.target}')
                header_nodes = self.vfs.resolve_nodes(node.target, expansion_callback=self._expand_node)
                header_bytes = [self.get_node_data(h) for h in header_nodes]

                if node.hierarchical_id == (5, 31) or node.parent.hierarchical_id in [(5, 2, 0), (5, 3, 0), (5, 4, 0), (5, 5, 0)]: # slot 32 (final slot) 64-byte alignement
                    alignement = len(header_bytes[0]) % 64
                    header_bytes[0] = header_bytes[0] + (b'0'*alignement)
                    logger.warning(f'Adjusted alignment for raw {node.hierarchical_id_str}')

                draft_root = handler.unpack_with_headers(node, header_bytes)
            else:
                draft_root = handler.get_file_tree()
                
            identity = handler.get_identity()
            new_nodes = draft_root.children or [draft_root]

            if self.vfs:
                logger.debug(f'Registering {node.hierarchical_id_str} with VfsManager')
                self.vfs.register_node(node)
                self.vfs.insert_children(node, new_nodes)
            logger.info(f'Inserted {len(new_nodes)} nodes from {node.name} ({identity})')
            return new_nodes   

    def _build_unwrap_chain(self, node: VfsNode) -> list[VfsNode]:
        '''helper for building the path to physical source'''
        chain: list[VfsNode] = []
        current: Optional[VfsNode] = node

        while current:
            chain.append(current)
            if getattr(current, 'is_physical', False):
                break
            current = current.parent
        
        if not chain or not getattr(chain[-1], 'is_physical', False):
            return []
        
        chain.reverse()
        return chain
    
    def _unwrap_chain(self, chain: list[VfsNode]) -> bytes:
        '''helper to walk the path from the physical source to virtual requested file'''
        if self.active_handler is None:
            logger.warning('No Physical handler found')
            return b''

        current_bytes = self.active_handler.get_raw_node(chain[0])

        for i in range(1, len(chain)):
            container = chain[i -1]
            target = chain[i]

            handler_class = Registry.get_handler(container)
            if not handler_class:
                logger.warning(f'No handler for {container.name}')
                return b''
            with handler_class(current_bytes, container) as handler:
                logger.debug(f'Unwrapping {target.name} from {container.name} via {handler_class.__name__}')
                current_bytes = handler.get_raw_node(target)

        return current_bytes

    def _handler_datacenter_unpack(self, node: VfsNode) -> None:
        '''For unpacking nodes that need headers from datacenter'''
        if not self.vfs or not node.target:
            logger.warning(f'Cannot unpack datacenter headers for {node.name}')
            return
        
        logger.info(f'Datacenter unpack requested for {node.name} - resolving {len(node.target)} headers')
        header_nodes = self.vfs.resolve_nodes(node.target, expansion_callback=self._expand_node)
        header_bytes = [self.get_node_data(h) for h in header_nodes]
        for h in header_bytes:
            logger.warning(f'{h}')
        handler_class = Registry.get_handler(node)
        if not handler_class:
            return
        container_bytes = self.get_node_data(node)
        with handler_class(container_bytes, node.parent) as handler:
            if hasattr(handler, 'unpack_with_headers'):
                if node.hierarchical_id == (5, 31): # 64 byte alignment for tail entry (32nd slot)
                    header_bytes = header_bytes[:len(header_bytes) - (len(header_bytes) % 64)]
                new_tree = handler.unpack_with_headers(node, header_bytes)
            else:
                logger.warning(f'{handler_class.__name__} missing unpack_with_headers')
        
        if self.vfs and new_tree.children:
            for n in new_tree.children:
                self.vfs.register_node(n, n.offset)
    ###------------------------ Callback -----------------------###
    def _expand_node(self, parent_node: VfsNode) -> None:
        '''Callback for VfsManager to expand missing nodes'''
        if not self.vfs:
            return
        self.load_source(parent_node)