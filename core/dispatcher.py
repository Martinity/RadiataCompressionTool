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

'''TODO The dispatcher must be the one to get the raw bytes of our files since it manages the handle'''


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
        if node.is_dirty and node.pending_data:
            return node.pending_data
        
        # Has physical address
        if node.parent == self.vfs.root or node.parent is None: 
            abs_offset = self.vfs.get_absolute_offset(node)
            return self.active_handler.read_file_data(node, abs_offset)
        
        # Has Virtual Address - Must find nearest physical address
        provider = node.parent
        while provider.parent and provider.parent.is_physical:
            provider = provider.parent

        if not provider:
            logger.error(f'No parent for {node.name}')
            return b''

        provider_hid = provider.hierarchical_id_str
        # Check buffer for virtual file
        if provider_hid in self._buffer_cache:
            parent_buffer = self._buffer_cache[provider_hid]
        else:
            parent_buffer = self.get_node_data(provider)

            if provider.is_decompressed or provider.is_unpacked:
                self._manage_cache(provider_hid, parent_buffer)
        # Create virtual file
        start = node.offset
        end = start + node.size
        return parent_buffer[start:end]
    
    def _manage_cache(self, hid: str, data: bytes):
        if len(self._buffer_cache) >= self.cache_limit:
            oldest = next(iter(self._buffer_cache))
            del self._buffer_cache[oldest]
        self._buffer_cache[hid] = data

    def execute_node_action(self, node: 'VfsNode', action_name: str):
        '''Route action to format handler'''
        # ISO level Action
        if node.parent == self.vfs.root or node.parent is None or node.is_physical:
            if hasattr(self.active_handler, 'execute_action'):
                self.active_handler.execute_action(node, action_name)
            return
        
        # Virtual node action
        provider = node if not node.is_physical else node.parent
        while provider.parent and provider.parent.is_physical:
            provider = provider.parent

        handler_class = Registry.get_handler_class_for(provider)
        if not handler_class:
            logger.warning(f'No registered handler class for {provider.name}')
            return
        
        provider_buffer = self.get_node_data(provider)

        with handler_class(provider_buffer, provider.parent) as temp_handler:
            if hasattr(temp_handler, 'execute_action'):
                temp_handler.execute_action(node, action_name)
            else:
                logger.warning(f'{handler_class.__name__} cannot')

        if self.active_handler and hasattr(self.active_handler, 'execute_action'):
            self.active_handler.execute_action(node, action_name)
        else:
            logger.warning(f'No handler for action: {action_name}')