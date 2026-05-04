from __future__ import annotations

from pathlib import Path
from core.registry import Registry
from core.node import VfsManager, ModTracker, VfsNode
from core.workers import RebuildWorker
from typing import TYPE_CHECKING, Any
from PyQt6.QtCore import pyqtSignal, QObject

if TYPE_CHECKING:
    from core.contracts import BaseHandler

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------------------------------- Dispatch -------------------------------------------------###

class Dispatcher(QObject):
    '''Bridge between UI and logic'''
    node_changed = pyqtSignal(VfsNode) # Update for TreeView
    tracking_update = pyqtSignal(int, int) # (modified_count, staged_count)
    rebuild_requested = pyqtSignal(list) # For MainWindow page swap

    # Rebuild signals TODO invesigate using the logger to convey the messages pros/cons
    rebuild_progress = pyqtSignal(int) # % completion of rebuild
    rebuild_log = pyqtSignal(str) # logged rebuild information
    rebuild_complete = pyqtSignal(bool, str) # (Success/Fail, finish message)

    def __init__(self) -> None:
        super().__init__()
        self.vfs: VfsManager | None = None
        self.tracker = ModTracker()
        self.active_handler: BaseHandler | None = None
        self._rebuild_worker: RebuildWorker | None = None
        # cache for editors to keep nodes open TODO
        self.editor_cache: dict[str, bytes] = {} # Format: [hid, bytes]
        self._setup_proxy_connections()

    def _setup_proxy_connections(self) -> None:
        '''Relay tracker signals to UI'''
        self.tracker.node_modified.connect(self.node_changed.emit)
        self.tracker.node_reverted.connect(self.node_changed.emit)
        self.tracker.state_changed.connect(self.tracking_update.emit)
        self.tracker.rebuild_initiated.connect(self.rebuild_requested.emit)

        self.tracker.state_changed.connect(self._relay_tracking_state)

    def apply_edit(self, node: VfsNode, data: bytes):
        self.tracker.mark_modified(node, data)
        logger.info(f'System-wide update for node: {node.name}')

    def _relay_tracking_state(self):
        '''Emit counts so UI doesn't need to recalc'''
        self.tracking_update.emit(len(self.tracker.modified_nodes), len(self.tracker.rebuild_queue))

    def __str__(self) -> str:
        return f"Dispatcher(active_handler={self.active_handler})"

    def load_source(self, source: Path | VfsNode) -> list[VfsNode]:
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

    def get_node_data(self, node: VfsNode) -> bytes:
        '''Return the raw bytes of the requested node by unwrapping from the physical layer (to virtual node)'''
        if node.pending_data is not None:
            return node.pending_data

        chain = self._build_unwrap_chain(node)
        if not chain:
            logger.warning(f'No physical reference point for node {node.hierarchical_id_str}')
        logger.debug(f'Resolving data for {node.hierarchical_id_str}')
        return self._unwrap_chain(chain)
    
    def apply_node_mod(self, node: VfsNode, new_data: bytes) -> None:
        '''Editors call this when submitting changes'''
        self.tracker.mark_modified(node, new_data)

    def execute_node_action(self, node: VfsNode, action_name: str) -> Any:
        '''Route action to format handler'''
        handler_class = Registry.get_handler(node)
        if not handler_class:
            logger.warning(f'No handler found for action "{action_name}" on {node.name}')
            return None

        logger.debug(f'Routing "{action_name}" to {handler_class.__name__}')
        node_bytes = self.get_node_data(node)

        header_bytes = self._resolve_data_from_hid(getattr(node, 'target', None))

        with handler_class(node_bytes, node.parent) as temp_handler:
            if header_bytes and hasattr(temp_handler, 'datacenter_headers'):
                temp_handler.datacenter_headers = header_bytes
            if hasattr(temp_handler, 'execute_action'):
                return temp_handler.execute_action(node, action_name)
            else:
                logger.warning(f'{handler_class.__name__} is missing execute_action')
                return None

    def start_iso_rebuild(self, output_path: Path) -> None:
        if not self.active_handler or not self.vfs:
            self.rebuild_complete.emit(False, 'No Active ISO.')
            return
        
        staged_nodes = list(self.tracker.rebuild_queue)

        self.rebuild_log.emit(f'Preparing to build {len(self.tracker.rebuild_queue)} staged file(s)')

        try:
            physical_staged_nodes = self._rollup_virtual_nodes(staged_nodes)
        except Exception as e:
            logger.error(f'Roll-up failed: {e}', exc_info=True)
            self.rebuild_complete.emit(False, f'Virtual File Packing Failed: {e}')
            return

        self._rebuild_worker = RebuildWorker(self.active_handler, self.vfs.root, physical_staged_nodes, output_path)
        self._rebuild_worker.progress_updated.connect(self.rebuild_progress.emit)
        self._rebuild_worker.log_message.connect(self.rebuild_log.emit)
        self._rebuild_worker.rebuild_finished.connect(self._on_rebuild_finished)

        self._rebuild_worker.start()

    def _on_rebuild_finished(self, success: bool, message: str) -> None:
        self.rebuild_complete.emit(success, message)
        if self._rebuild_worker:
            self._rebuild_worker.deleteLater()
            self._rebuild_worker = None
        if success:
            self.tracker.clear()

    def close(self) -> None:
        '''For exiting the dispatch'''
        if self.active_handler:
            self.active_handler.close()
        self.editor_cache.clear()
        self.vfs = None
        self.active_handler = None
        self.tracker.clear()
        logger.debug('- Dispatcher and Tracker state reset -')

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
        if not self.vfs:
            logger.warning('No physical layer detected.')
            return []
        
        if node.children: # Prevent duplicate extractions
            logger.debug(f'Node {node.name} is already expanded.')
            return node.children
        
        container_bytes = self.get_node_data(node)
        header_bytes = self._resolve_data_from_hid(getattr(node, 'target', None))

        with handler_class(container_bytes, node) as handler:
            if header_bytes and hasattr(handler, 'datacenter_headers'):
                handler.datacenter_headers = header_bytes
            draft_root = handler.get_file_tree()
                
            identity = handler.get_identity()
            new_nodes = draft_root.children or [draft_root]

            self.vfs.register_node(node)
            self.vfs.insert_children(node, new_nodes)
            logger.info(f'Inserted {len(new_nodes)} nodes from {node.name} ({identity})')
            return new_nodes   
        
    def _rollup_virtual_nodes(self, staged_nodes: list[VfsNode]) -> list[VfsNode]:
        '''Repack children into parents'''
        logger.info('Initiating virutal node roll-up')
        current_queue = set(staged_nodes)
        while True:
            if all(getattr(node, 'is_physical', False) for node in current_queue):
                break
            max_depth = max(len(node.hierarchical_id) for node in current_queue)
            deepest_nodes = [n for n in current_queue if len(n.hierarchical_id) == max_depth]
            parent_map: dict[VfsNode, list[VfsNode]] = {}

            for node in deepest_nodes:
                if node.parent not in parent_map:
                    parent_map[node.parent] = []
                parent_map[node.parent].append(node)

            for parent, modified_children in parent_map.items():
                handler_class = Registry.get_handler(parent)
                if not handler_class:
                    logger.error(f'No handler found for {parent.name}')
                    continue
                logger.debug(f'Repacking {parent.name} using {handler_class.__name__}')

                parent_bytes = self.get_node_data(parent)
                header_bytes = self._resolve_data_from_hid(getattr(parent, 'target', None))
                
                with handler_class(parent_bytes, parent.parent) as handler:
                    if header_bytes and hasattr(handler, 'datacenter_headers'):
                        handler.datacenter_headers = header_bytes
                    new_parent_bytes = handler.rebuild_node(parent, modified_children)
                    parent.pending_data = new_parent_bytes
                    current_queue.add(parent)

            for node in deepest_nodes:
                current_queue.remove(node)
        logger.info('Virtual node roll-up complete')
        return list(current_queue)

    def _build_unwrap_chain(self, node: VfsNode) -> list[VfsNode]:
        '''helper for building the path to physical source'''
        chain: list[VfsNode] = []
        current: VfsNode | None = node

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
                hid = getattr(target, 'target', None)
                mapped_hid = hid[0] if hid else []
                if len(mapped_hid) > 2 and hasattr(handler, 'get_buffer_data'):
                    current_bytes = handler.get_buffer_data(target).tobytes()
                else:
                    current_bytes = handler.get_raw_node(target)

        return current_bytes
    
    def _resolve_data_from_hid(self, target_hids: list[tuple] | None) -> list[bytes]:
        '''Getter for mapped HIDs'''
        if not self.vfs or not target_hids:
            return []
        logger.debug(f'Resolving {len(target_hids)} datacenter headers.')
        header_nodes = self.vfs.resolve_nodes(target_hids, expansion_callback=self._expand_node)
        return [self.get_node_data(header) for header in header_nodes]
    
    ###------------------------ Callback -----------------------###
    def _expand_node(self, parent_node: VfsNode) -> None:
        '''Callback for VfsManager to expand missing nodes'''
        if not self.vfs:
            return
        self.load_source(parent_node)
