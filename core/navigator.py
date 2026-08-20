'''Logic for any VFS tree traversal'''
from __future__ import annotations

import threading
from typing import Callable, TYPE_CHECKING
from core.node import VfsNode, VfsManager
from core.registry import Registry
from core.contracts import ContainerHandler, RebuildResult
if TYPE_CHECKING:
    from core.workers import TaskHandle

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------------- Exceptions ----------------------------------------###

class ExpansionTimeoutError(RuntimeError):
    """Raised when a VFS node expansion request times out.

    This is a hard error: it means the rebuild thread waited the full
    EXPANSION_TIMEOUT for an expansion task to complete and it never did.
    Letting the rebuild continue with stale/empty data would produce a
    silently corrupt output, so we raise instead.
    """
    pass

###----------------------------------- Navigator ----------------------------------------###

class VfsNavigator:
    '''Handles all tree traveling logic'''
    EXPANSION_TIMEOUT = 30.0  # seconds; raised from 1.0 — real disk+decompress can be slow
    def __init__(
            self,
            vfs: VfsManager,
            data_reader: Callable[[VfsNode], bytes],
            expansion_callback: Callable[[VfsNode, threading.Event], None],
        ):
        self.vfs  = vfs
        self.read = data_reader                      # dispatcher.get_node_data
        self.expansion_callback = expansion_callback # dispatcher._expand_node
        self._rollup_touched: set[VfsNode] = set()

    ###--------------------- Public API -----------------------###

    def unwrap_chain(self, node: VfsNode) -> bytes:
        '''For traveling through the VFS from physical layer to depths'''
        chain = self._build_chain(node)
        if not chain:
            logger.warning(f'No physical source for node: {node.hierarchical_id_str}')
            return b''
        return self._walk_chain(chain)

    def resolve_data_from_hid(self, target: tuple[int, ...] | None) -> bytes | None:
        '''
        Resolve HID to raw bytes. snapshot -> expand missing -> re-read. In order of hids indexes.
        Returning pending data first if exists.
        '''
        if not target:
            return None
        logger.debug(f'Resolving datacenter header ID:{target}.')
        previous_depth = -1
        current_depth = 0

        while current_depth < 20:
            snapshot = self.vfs.snapshot_hids([target]) # Get VFS snapshot
            if not snapshot.unresolved: # target is already expanded
                break
            nearest = self.vfs.find_nearest_ancestor(target)
            if not nearest:
                break
            current_depth = len(nearest.hierarchical_id)
            if current_depth <= previous_depth and nearest:
                logger.error(f'Expansion stalled at {nearest.hierarchical_id_str}. Failed to reach target: {target}')
                break
            previous_depth = current_depth
            self._expand_pending(snapshot.unresolved)

        node = self.vfs.get_vfs_node_by_id(target)
        if not node:
            logger.warning(f'Could not resolve datacenter header: {target}')
            return None
        return self.read(node)

    def rollup_nodes(self, staged_nodes: list[VfsNode], task_handle: 'TaskHandle') -> list[VfsNode]:
        '''For Rebuilding the VFS from deepest layer to physical (children -> parent)'''
        current_queue: set[VfsNode] = set(staged_nodes)
        while not all(node.is_physical for node in current_queue): # deepest -> physical layer
            task_handle.checkpoint()
            max_depth:     int = max(len(node.hierarchical_id) for node in current_queue)
            deepest_nodes: list[VfsNode] = [n for n in current_queue if len(n.hierarchical_id) == max_depth]
            parent_map:    dict[VfsNode, list[VfsNode]] = {}

            for node in deepest_nodes: # build the parent-child map
                if node.parent is None:
                    task_handle.log_message.emit(f'Cannot roll up {node.name}; No parent node.')
                    continue
                parent_map.setdefault(node.parent, []).append(node)

            for parent, modified_children in parent_map.items(): # build the parents
                if parent.pending_data and parent not in modified_children:
                    current_queue.add(parent)
                    task_handle.log_message.emit(f'{parent.hierarchical_id} has pre-computed payload - skipping rebuild')
                    continue
                handler_class = Registry.get_handler(parent)
                if not handler_class:
                    task_handle.log_message.emit(f'No handler found for {parent.hierarchical_id}')
                    continue
                if not issubclass(handler_class, ContainerHandler):
                    logger.error(f'Subcontract {handler_class.__name__} must be ContainerHandler for virtual tree navigation.')
                    continue
                parent_bytes = self.read(parent)
                header_bytes = None
                if parent.target:
                    target_node = self.vfs.get_vfs_node_by_id(parent.target)
                    if target_node is not None and target_node.pending_data:
                        header_bytes = target_node.pending_data
                    else:
                        header_bytes = self.resolve_data_from_hid(parent.target)
                with handler_class(parent_bytes, parent.parent) as handler:
                    handler.task_handle = task_handle
                    if header_bytes is not None and hasattr(handler, 'datacenter_header'):
                        handler.datacenter_header = header_bytes
                    result = handler.rebuild_node(parent, modified_children)
                    if isinstance(result, RebuildResult): # Check for complexe build results
                        payload, target_data = result
                    else:
                        payload, target_data = result, None
                    parent.pending_data = payload
                    self._rollup_touched.add(parent)
                    if target_data and parent.target: # Datacenter rebuild
                        self.resolve_data_from_hid(parent.target) # Ensure target is in VFS
                        target_node = self.vfs.get_vfs_node_by_id(parent.target)
                        if target_node:
                            target_node.pending_data = target_data
                            self._rollup_touched.add(target_node)
                            current_queue.add(target_node)
                            task_handle.log_message.emit(f'Datacenter modification queued: {parent.name} -> {parent.target}')
                        else:
                            task_handle.log_message.emit(f'CRITICAL: Could not find target node {parent.target} in VFS. Target may not exist')
                    current_queue.add(parent)

            for node in deepest_nodes: # update the queue
                current_queue.discard(node)

        task_handle.log_message.emit('Virtual node roll-up complete')
        return list(current_queue)

    def precompute_datacenter(self, staged_nodes: list[VfsNode], task_handle: 'TaskHandle') -> list[VfsNode]:
        '''Cache payload/headers that are not located sequentially on disk'''
        nonseq_nodes: set[VfsNode] = set()
        staged_sorted = sorted(staged_nodes, key=lambda node: node.hierarchical_id)
        task_handle.log_message.emit(f'{len(staged_sorted)} nodes to precompute and all their children.')
        for node in staged_sorted:
            current_node = node
            while current_node is not None:
                # task_handle.log_message.emit(f'{current_node.hierarchical_id} has target:{current_node.target}')
                if current_node.target and current_node.parent:
                    task_handle.log_message.emit(f'Sending {node.hierarchical_id} to cached roll-up')
                    nonseq_nodes.add(node)
                    break
                current_node = current_node.parent if current_node.is_physical is False else None

        if not nonseq_nodes:
            task_handle.log_message.emit('No nonsequential files. Nothing to precompute.')
            return []

        task_handle.log_message.emit(f'Sending {len(nonseq_nodes)} nodes to cached roll-up')
        return self.rollup_nodes(list(nonseq_nodes), task_handle)

    def clear_rollup_pending(self) -> None:
        '''Clear pending_data cached on parents/targets during the last rollup.'''
        for node in self._rollup_touched:
            node.clear_pending()
        self._rollup_touched.clear()

    ###---------------------- helpers -------------------------###

    def _expand_pending(self, unresolved: list[tuple[int,...]]) -> None:
        '''
        Find nearest ancestor and request expansion via callback.
        Blocks the thread until expansion completes or timesout.
        Lock never needs to be held here.
        '''
        ancestors_needed: dict[tuple[int,...], VfsNode] = {}
        for hid in unresolved: # Deduplicate
            ancestor = self.vfs.find_nearest_ancestor(hid)
            if not ancestor:
                logger.warning(f'No registered ancestor found for HID {hid}')
                continue
            key = ancestor.hierarchical_id
            if key not in ancestors_needed:
                ancestors_needed[key] = ancestor

        safety_count = 0
        for ancestor in ancestors_needed.values():
            if ancestor.expansion_pending and ancestor._expansion_event: # Expansion in progress
                logger.debug(f'Waiting for in-progress expansion of ID: {ancestor.hierarchical_id_str}')
                if not ancestor._expansion_event.wait(timeout=self.EXPANSION_TIMEOUT):
                    raise ExpansionTimeoutError(
                        f'Timeout ({self.EXPANSION_TIMEOUT}s) waiting for in-progress expansion of '
                        f'{ancestor.hierarchical_id_str}. Rebuild aborted to prevent stale-data corruption.'
                    )
            elif ancestor.children: # Expanded but missing target
                logger.warning(f'{ancestor.hierarchical_id_str} has children but target HID not found')
                wait = ancestor.begin_expansion()
                self.expansion_callback(ancestor, wait)
                if not wait.wait(timeout=self.EXPANSION_TIMEOUT):
                    raise ExpansionTimeoutError(
                        f'Timeout ({self.EXPANSION_TIMEOUT}s) re-expanding {ancestor.hierarchical_id_str}. '
                        f'Rebuild aborted to prevent stale-data corruption.'
                    )
            else: # First time expansion
                logger.debug(f'Expanding {ancestor}')
                wait = ancestor.begin_expansion()
                self.expansion_callback(ancestor, wait)
                if not wait.wait(timeout=self.EXPANSION_TIMEOUT):
                    raise ExpansionTimeoutError(
                        f'Timeout ({self.EXPANSION_TIMEOUT}s) expanding {ancestor.hierarchical_id_str}. '
                        f'Rebuild aborted to prevent stale-data corruption.'
                    )
            safety_count += 1
            if safety_count > 20:
                logger.warning(f'Failed to expand {len(unresolved)} node(s)')
                return

    ###-------------------- Chain Traversal --------------------###

    def _build_chain(self, node: VfsNode) -> list[VfsNode]:
        chain:   list[VfsNode]  = []
        current: VfsNode | None = node
        while current:
            chain.append(current)
            if current.is_physical:
                break
            current = current.parent
        if not chain or not chain[-1].is_physical:
            return []
        chain.reverse()
        return chain

    def _walk_chain(self, chain: list[VfsNode]) -> bytes:
        '''helper to walk the path from the physical source to virtual requested file'''
        current_bytes = self.read(chain[0])
        for i in range(1, len(chain)):
            container     = chain[i -1]
            target        = chain[i]
            handler_class = Registry.get_handler(container)
            logger.debug(f'Currently at {container.hierarchical_id} searching until {target.hierarchical_id}')
            if not handler_class:
                logger.warning(f'No handler for {container.name}')
                return b''
            if not issubclass(handler_class, ContainerHandler):
                logger.error(f'Subcontract {handler_class.__name__} must be ContainerHandler for virtual tree navigation.')
                continue
            with handler_class(current_bytes, container) as handler:
                current_bytes = handler.get_raw_node(target)
        return current_bytes

    ###---------------- Expansion Detection ---------------###

    def _needs_expansion(self, node: VfsNode) -> bool:
        '''True if node is a container that has not been expanded and registered'''
        if node.children:
            return False
        profile = Registry.get_handler_profile(node)
        if not profile:
            return False
        return issubclass(profile.handler_class, (ContainerHandler))
