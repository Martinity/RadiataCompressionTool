from pathlib import Path
from core.registry import Registry
from core.node import VfsManager
from typing import TYPE_CHECKING, Type, Optional

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

    def __str__(self) -> str:
        return f"Dispatcher(active_handler={self.active_handler})"
    
    def load_source(self, path: Path) -> tuple[Type['VfsNode']|None, str|None]:
        '''Load Format handler from predefined formats in class FormatProfile'''
        logger.info(f"Loading {path.name}")

        if self.active_handler: # if there is an active handler close it
            logger.debug(f'Closing previous handler: {self.active_handler}')
            self.active_handler.close()

        handler_class = Registry.get_handler_class_for(path)
        if handler_class is None:
            logger.warning(f"No handler for {path.suffix}")
            return None, None
        
        self.active_handler = handler_class(path)

        root_node = self.active_handler.get_file_tree()
        identity = self.active_handler.get_identity()

        logger.info(f"Loaded {identity} — {len(root_node.children)} top-level nodes")
        self.vfs = VfsManager(root_node)
        return root_node, identity

    def read_node_bytes(self, node: 'VfsNode') -> bytes:
        '''return bytes of node'''
        if node.pending_data and node.is_dirty: # Return the edited version if applicable
            logger.debug(f"Returning pending (edited) data for '{node.name}'")
            return node.pending_data
        
        if not self.active_handler or not self.vfs:
            logger.warning(f"No handler or vfs for node '{node.name}'")
            return b''
        
        abs_offset = self.vfs.get_absolute_offset(node)
        logger.debug(f"Asking {self.active_handler.__class__.__name__} for '{node.name}'")
        data = self.active_handler.read_file_data(node, abs_offset)

        logger.debug(f"Received {len(data):,} bytes")
        return data
    
