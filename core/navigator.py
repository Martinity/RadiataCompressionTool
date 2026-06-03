'''Logic for any VFS tree traversal'''
from __future__ import annotations

import threading
from typing import Callable
from core.node import VfsNode, VfsManager
from core.registry import Registry
from core.contracts import ContainerHandler

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------------- Navigator ----------------------------------------###

class VfsNavigator:
    '''Handles all tree traveling logic'''
    EXPANSION_TIMEOUT = 1.0
    def __init__(
            self, 
            vfs: VfsManager, 
            data_reader: Callable[[VfsNode], bytes], 
            expansion_callback: Callable[[VfsNode, threading.Event], None],
        ):
        self.vfs  = vfs
        self.read = data_reader                      # dispatcher.get_node_data
        self.expansion_callback = expansion_callback # dispatcher._expand_node

    ###--------------------- Public API -----------------------###

    def unwrap_chain(self, node: VfsNode) -> bytes:
        '''For traveling through the VFS from physical layer to depths'''
        chain = self._build_chain(node)
        if not chain:
            logger.warning(f'No physical source for node: {node.hierarchical_id_str}')
            return b''
        return self._walk_chain(chain)

    def resolve_data_from_hid(self, target: tuple[int, ...] | None) -> bytes | None:
        '''Resolve HID to raw bytes. snapshot -> expand missing -> re-read. In order of hids idx'''
        if not target:
            return None
        logger.debug(f'Resolving datacenter header ID:{target}.')
        previous_depth = -1

        while True:
            snapshot = self.vfs.snapshot_hids([target]) # Get VFS snapshot
            if not snapshot.unresolved: # expand first if needed
                break
            nearest = self.vfs.find_nearest_ancestor(target)
            current_depth = len(nearest.hierarchical_id) if nearest else 0
            if current_depth <= previous_depth:
                logger.error(f'Expansion stalled at {nearest.hierarchical_id_str}. Failed to reach target: {target}')
            previous_depth = current_depth
            self._expand_pending(snapshot.unresolved)

        node = self.vfs.get_node_by_id(target)
        if not node:
            logger.warning(f'Could not resolve datacenter header: {target}')
            return None
        return self.read(node)

    def rollup_nodes(self, staged_nodes: list[VfsNode], log_callback: Callable) -> list[VfsNode]:
        '''For Rebuilding the VFS from deepest layer to physical (children -> parent)'''
        from core.contracts import RebuildResult
        log_callback('Initiating virtual node roll-up...')
        current_queue: set[VfsNode] = set(staged_nodes)
        while not all(node.is_physical for node in current_queue): # deepest -> physical layer
            max_depth:     int = max(len(node.hierarchical_id) for node in current_queue)
            deepest_nodes: list[VfsNode] = [n for n in current_queue if len(n.hierarchical_id) == max_depth]
            parent_map:    dict[VfsNode, list[VfsNode]] = {}

            for node in deepest_nodes: # build the parent-child map
                if node.parent is None:
                    log_callback(f'Cannot roll up {node.name}; No parent node.')
                    continue
                parent_map.setdefault(node.parent, []).append(node)
            log_callback('Rebuild map built.')

            for parent, modified_children in parent_map.items(): # build the parents
                handler_class = Registry.get_handler(parent)
                if not handler_class:
                    log_callback(f'No handler found for {parent.name}')
                    continue
                if not issubclass(handler_class, ContainerHandler):
                    logger.error(f'Subcontract {handler_class.__name__} must be ContainerHandler for virtual tree navigation.')
                    continue
                parent_bytes = self.read(parent)
                header_bytes = self.resolve_data_from_hid(parent.target)
                with handler_class(parent_bytes, parent.parent) as handler:
                    if header_bytes is not None and hasattr(handler, 'datacenter_headers'):
                        handler.datacenter_headers = [header_bytes]
                    result = handler.rebuild_node(parent, modified_children, log_callback)
                    if isinstance(result, RebuildResult):
                        payload, target_data = result
                        parent.pending_data = payload
                        if target_data and parent.target:
                            target_node = self.vfs.get_node_by_id(parent.target)
                            if target_node:
                                target_node.pending_data = target_data
                                current_queue.add(target_node)
                                log_callback(f'Intercepted datacenter modification: {target_node.hierarchical_id}')
                            else:
                                log_callback(f'CRITICAL: Could not find target node {parent.target} in VFS')
                    else:
                        parent.pending_data = result
                    current_queue.add(parent)
            log_callback('Parents rebuilt')

            for node in deepest_nodes: # update the queue
                current_queue.discard(node)

        log_callback('Virtual node roll-up complete')
        return list(current_queue)

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

        for ancestor in ancestors_needed.values():
            if ancestor.expansion_pending and ancestor._expansion_event: # Expansion in progress
                logger.debug(f'Waiting for in-progress expansion of ID: {ancestor.hierarchical_id_str}')
                if not ancestor._expansion_event.wait(timeout=self.EXPANSION_TIMEOUT):
                    logger.error(f'Timeout waiting for expansion of ID: {ancestor.hierarchical_id_str}')
            elif ancestor.children: # Expanded but missing target
                logger.warning(f'{ancestor.hierarchical_id_str} has children but target HID not found')
                wait = ancestor.begin_expansion()
                self.expansion_callback(ancestor, wait)
                if not wait.wait(timeout=self.EXPANSION_TIMEOUT):
                    logger.error(f'Timeout re-expanding {ancestor.hierarchical_id_str}')
            else: # First time expansion
                logger.debug(f'Expanding {ancestor}')
                wait = ancestor.begin_expansion()
                self.expansion_callback(ancestor, wait)
                if not wait.wait(timeout=self.EXPANSION_TIMEOUT):
                    logger.error(f'Timeout expanding {ancestor.hierarchical_id_str}')

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

        logger.debug(f'Found a match {current_bytes[:64]}:64')
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
    