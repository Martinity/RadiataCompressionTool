'''
Dispatcher handles most of the coordination work between the UI and logic
Functions as a signal proxy
'''
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from PyQt6.QtCore import pyqtSignal, QObject, Qt

from core.registry import Registry
from core.node import VfsManager, ModTracker, VfsNode
from core.workers import TaskCoordinator, ActionStatus, ActionResult, Actions, ActionType, TaskHandle
from core.navigator import VfsNavigator
from core.descriptor_manager import NodeDescriptorStore
if TYPE_CHECKING:
    from core.contracts import BaseHandler, BaseEditor

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------------------------------- Dispatch -------------------------------------------------###

class Dispatcher(QObject):
    '''Bridge between UI and logic

    Node actions pass to Actions.dispatch which routes by ActionDef.action_type.
    Dispatcher does not need to now what an action needs to execute.
    '''
    # Tree / Tracker
    iso_loaded        = pyqtSignal(list)     # [root]
    expand_requested  = pyqtSignal(object, object)   # (VfsNode, wait_event)
    node_changed      = pyqtSignal(VfsNode)  # Update for TreeView
    tracking_update   = pyqtSignal(int, int) # (modified_count, staged_count)
    # Page transitions
    rebuild_requested = pyqtSignal(list)     # For MainWindow page swap
    # Rebuild Task
    rebuild_progress = pyqtSignal(int)       # completion %
    rebuild_log      = pyqtSignal(str)       # log for rebuild page
    rebuild_complete = pyqtSignal(bool, str) # (success, message)
    # ISO verification
    iso_verified = pyqtSignal(str)           # Build string
    # Generic node actions
    action_complete = pyqtSignal(object)     # ActionResult 
    workspace_log   = pyqtSignal(str)        # Testing usefulness of log signalling
    # IO 
    io_progress = pyqtSignal(int, str)       # completion %
    io_complete = pyqtSignal(bool, object)   # (success, result)

    def __init__(self) -> None:
        super().__init__()
        self.vfs:                        VfsManager | None = None
        self.active_handler:            BaseHandler | None = None
        self.nav:                      VfsNavigator | None = None
        self._descriptor_store: NodeDescriptorStore | None = None
        self.tracker          = ModTracker()
        self.task_coordinator = TaskCoordinator()
        self._setup_connections()

        self._rebuild_active: bool = False

    def _setup_connections(self) -> None:
        '''Relay tracker signals to UI'''
        self.tracker.node_modified.connect(self.node_changed.emit)
        self.tracker.node_reverted.connect(self.node_changed.emit)
        self.tracker.rebuild_initiated.connect(self.rebuild_requested.emit)
        self.tracker.state_changed.connect(self._relay_tracking_state)

        self.expand_requested.connect(self._handle_expand_request, Qt.ConnectionType.QueuedConnection) # pyrefly: ignore this is valid despite stub

    def set_descriptor_store(self, store: NodeDescriptorStore) -> None:
        self._descriptor_store = store

    def _relay_tracking_state(self):
        '''Emit counts so UI doesn't need to recalc'''
        self.tracking_update.emit(len(self.tracker.modified_nodes), len(self.tracker.rebuild_queue))

    def __str__(self) -> str:
        return f"Dispatcher(active_handler={self.active_handler})"

    ###----------------------------------- Public ----------------------------------------###

    def load_source(self, source: Path | VfsNode) -> TaskHandle | list[VfsNode]:
        if isinstance(source, Path):
            handler_class = Registry.get_handler(source)
            if not handler_class:
                logger.warning(f'No handler for {source.name}')
                return []
            return self._load_physical(handler_class, source)
        
        if source.children or source.expansion_pending:
            return source.children or []

        profile = Registry.get_handler_profile(source)
        if not profile:
            logger.warning(f'No profile or {source.name}, cannot expand.')
            return []
        
        action_def = profile.primary_expand_action()
        if not action_def:
            logger.debug(f'{source.name} has no TREE_EXPAND action')
            return []
        
        source.expansion_pending = True
        task_handle = self.task_coordinator.start_task(
            Actions.dispatch,
            action_def,
            source,
            self.nav,
        )
        task_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.workspace_log.emit)
        task_handle.finished.connect(self._on_action_complete)
        return [] # signal populates the tree ^^^
    
    def get_node_data(self, node: VfsNode) -> bytes:
        '''Return the raw bytes of the requested node, unwrapping virtual to the physical source'''
        pending = node.pending_data
        if pending is not None:
            return pending
        if node.is_physical:
            if not self.active_handler:
                logger.error(f'No physical handler for node: {node.hierarchical_id_str}')
                return b''
            return self.active_handler.get_raw_node(node)
        if not self.nav:
            logger.error(f'No VfsNavigator initialized. Cannot resolve node: {node.hierarchical_id_str}')
            return b''
        return self.nav.unwrap_chain(node)

    def apply_edit(
        self, 
        node: VfsNode, 
        data: Any, 
        on_success: Callable[[], None]    | None = None,
        on_failure: Callable[[str], None] | None = None,
    ) -> None | TaskHandle:
        '''Pushes changes to the tracker.
        If data is not bytes, dispatches to a background worker to decode the payload
        Notifies the editor when finished'''
        if isinstance(data, bytes): # Bytes type payload passthrough
            original = self.get_node_data(node) if node not in self.tracker._originals else b''
            self.tracker.mark_modified(node, data, original)
            logger.info(f'Edit applied directly: {node.name}')
            if on_success:
                on_success()
            return

        handler_class = Registry.get_handler(node)
        if not handler_class:
            error_msg = f'No handler registered for {node.name} to compile payload.'
            logger.error(error_msg)
            if on_failure:
                on_failure(error_msg)
            return
        # Start task
        task_handle = self.task_coordinator.start_task(
            Actions.decode_editor_data,
            handler_class,
            node,
            data
        )
        task_handle.log_message.connect(
            self.rebuild_log.emit if self._rebuild_active else self.workspace_log.emit
        )
        task_handle.finished.connect(
            lambda success, result: self._on_decode_done(
                success, result, node, on_success, on_failure
            )
        )
        return task_handle

    def open_editor(self, node: VfsNode, editor: BaseEditor) -> TaskHandle | None:
        '''
        Start background data preparation for an editor
        
        Returns a TaskSignal of either:
            Success   - EditorPayload(node, data)
            Exception - (False, str)
        '''
        if not self.nav:
            logger.error('Navigator not initialised')
            return None
        handler_class = (
            Registry.get_handler_for_editor(editor)
            or Registry.get_handler(node)
        )
        if not handler_class:
            logger.warning(f'No handler for {node.name} - Cannot prepare editor data')
            return None
        logger.debug(
            f'open_editor: node={node.name}'
            f'editor={editor.__class__.__name__}'
            f'handler={handler_class.__name__}'
        )
        # Start task
        task_handle = self.task_coordinator.start_task(
            Actions.prepare_editor,
            handler_class,
            node,
            self.nav,
        )
        # Connect signals
        task_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.workspace_log.emit)
        return task_handle

    def execute_node_action(self, node: VfsNode, action_name: str, **kwargs) -> None:
        '''Route action through Actions.dispatch. '''
        if not self.nav:
            logger.error('Navigator not initialised')
            return
        action_def = Registry.get_action(node, action_name)
        if not action_def:
            self.action_complete.emit(ActionResult(
                action_name=action_name,
                node=node,
                status=ActionStatus.FAILURE,
                message=f'No ActionDef registered for "{action_name}" on node: {node.hierarchical_id_str}'
            ))
            return
        if action_def.action_type is ActionType.TREE_EXPAND and node.children: # Dedup expansions
            self.action_complete.emit(ActionResult(
                action_name=action_name,
                node=node,
                status=ActionStatus.FAILURE,
                message=f'{node.name} has already been expanded previously. Duplicate expansion cancelled.'
            ))
            return
        task_handle = self.task_coordinator.start_task(
            Actions.dispatch,
            action_def,
            node, 
            self.nav,
            **kwargs
            )
        task_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.workspace_log.emit)
        task_handle.finished.connect(self._on_action_complete)

    def start_iso_rebuild(self, output_path: Path) -> TaskHandle | None:
        if not self.active_handler or not self.vfs or not self.nav:
            self.rebuild_complete.emit(False, 'No ISO Loaded.')
            return
        self._rebuild_active = True
        
        staged_nodes = list(self.tracker.rebuild_queue)
        self.rebuild_log.emit(f'Preparing to build {len(staged_nodes)} staged file(s)...')

        task_handle = self.task_coordinator.start_task(
            Actions.rebuild_iso,
            self.active_handler,
            self.vfs.root,
            self.nav,
            staged_nodes,
            output_path,
        )
        task_handle.progress.connect(lambda pct, _msg: self.rebuild_progress.emit(pct))
        task_handle.log_message.connect(self.rebuild_log.emit)
        task_handle.finished.connect(self._on_rebuild_finished)

        return task_handle

    def resolve_ghost_node(self, target_hid: tuple[int, ...], on_succes: Callable[[VfsNode], None]) -> None:
        '''Asynchronously unpack the VFS until the target hid is reached'''
        if not self.vfs or not self.nav:
            return
        
        def _drill_down() -> None:
            node = self.vfs.get_node_by_id(target_hid)
            if node:
                on_succes(node)
                return
            
            ancestor = self.vfs.find_nearest_ancestor(target_hid)
            if not ancestor:
                logger.error(f'Cannot resolve {target_hid}: No ancestor exists.')
                return
            profile = Registry.get_handler_profile(ancestor)
            action_def = profile.primary_expand_action() if profile else None
            if not action_def:
                logger.error(f'Cannot expand {ancestor.name}: No TREE_EXPAND action registered')
                return
            ancestor.expansion_pending = True
            logger.debug(f'Drilling down to {ancestor.name} ({ancestor.hierarchical_id_str})')
            handle = self.task_coordinator.start_task(
                Actions.dispatch,
                action_def,
                ancestor,
                self.nav
            )
            def _on_layer_done(success: bool, result: Any) -> None:
                self._on_action_complete(success, result)
                if success:
                    _drill_down()
                else:
                    logger.error('Failed to drill down to target node...')
            
            handle.log_message.connect(self.workspace_log.emit)
            handle.finished.connect(_on_layer_done)
        _drill_down()

    def close(self) -> None:
        '''For exiting the dispatch'''
        self.task_coordinator.shutdown()
        if self.active_handler:
            self.active_handler.close()
        self.vfs            = None
        self.active_handler = None
        self.nav            = None
        self.tracker.clear()
        logger.debug('- File System Reset -')

    ###------------------------------ Helpers --------------------------------###

    def _load_physical(self, handler_class: type, path: Path) -> TaskHandle:
        '''Send ISO loading to a worker thread'''
        if self.active_handler:
            self.active_handler.close()
        if not self._descriptor_store:
            logger.debug(f'No file metadata loaded... {self._descriptor_store}')

        task_handle = self.task_coordinator.start_task(
            Actions.load_iso,
            handler_class,
            path
        )
        task_handle.log_message.connect(self.workspace_log.emit)
        task_handle.finished.connect(lambda ok, result: self._on_iso_loaded(ok, result))
        return task_handle

    def _migrate_targets_if_needed(self) -> None:
        store = self._descriptor_store
        if store is None:
            return
        has_targets = any(meta.target_hid is not None for meta in store._db.values())
        if has_targets:
            return
        logger.info('No target entries found - running DatacenterTargets migration.')
        count = store.migrate_datacenter_targets()
        store.save()
        logger.info(f'Migration complete: {count} target entries written to {store._path.name}')
        logger.info('Re-enrichment pass complete - datacenter nodes have .kods extension.')

    ###------------------------ Callback -----------------------###
    def _expand_node(self, parent: VfsNode, wait_event: threading.Event) -> None:
        '''Callback by VfsNavigator on a background thread to expand missing nodes.
        Coordinates expansion on the main thread via QueuedConnection, blocking on wait_event '''
        self.expand_requested.emit(parent, wait_event)

    def _handle_expand_request(self, node: VfsNode, wait_event: threading.Event) -> None:
        '''Starts a worker task with TREE_EXPAND and sets wait_event on completion'''
        if not node.expansion_pending or node.children:
            wait_event.set()
            return
        
        profile = Registry.get_handler_profile(node)
        action_def = profile.primary_expand_action() if profile else None
        if not action_def or not self.nav:
            logger.warning(f'Cannot expand node: {node.hierarchical_id_str}, missing expand actions or navigator.')
            wait_event.set()
            return
        
        node.expansion_pending = True

        def _on_expand_done(success: bool, result: Any) -> None:
            self._on_action_complete(success, result)
            node.finish_expansion()
            wait_event.set()
        
        logger.debug('Starting expansion background task')
        task_handle = self.task_coordinator.start_task(
            Actions.dispatch,
            action_def,
            node,
            self.nav,
        )
        task_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.workspace_log.emit)
        task_handle.finished.connect(_on_expand_done)

    def _on_iso_loaded(self, success: bool, result: object) -> None:
        '''Takes the ISO's root+children nodes and intializes:
        VfsManager -> VfsNavigator -> metadata -> and signals'''
        from core.workers import LoadIsoResult
        if not success:
            msg = str(result) if not isinstance(result, LoadIsoResult) else 'Unknown error'
            logger.error(f'ISO failed: {msg}')
            return
        if not isinstance(result, LoadIsoResult):
            logger.error(f'_on_iso_loaded: unexpected result type {type(result)}')
            return
        handler, root = result.handler, result.root
        self.active_handler = handler
        self.vfs = VfsManager(
            root,
            node_enricher=(
                self._descriptor_store.enrich
                if self._descriptor_store else None
            )
        )
        self.vfs.enrich_initial_tree()
        logger.info(
            f'Workspace initialised: {handler.__class__.__name__}'
            f'({len(self.vfs.nodes_by_id)} nodes)'
        )
        self.nav = VfsNavigator(self.vfs, self.get_node_data, self._expand_node)
        # self._migrate_targets_if_needed()   #Uncomment for building metadata from scratch

        verify_handle = self.task_coordinator.start_task(Actions.verify_iso, handler)
        verify_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.workspace_log.emit)
        verify_handle.finished.connect(self._on_iso_verified)
        self.iso_loaded.emit([root])
        
    def _on_rebuild_finished(self, success: bool, result: Any) -> None:
        '''Verify type of result and pack signal'''
        self._rebuild_active
        if isinstance(result, ActionResult):
            msg = result.message or ('Rebuild succeeded.' if success else 'Rebuild failed.')
        else:
            msg = str(result)
        self.rebuild_complete.emit(success, msg)
        if success:
            self.tracker.clear()

    def _on_iso_verified(self, success: bool, result: Any) -> None:
        if success and isinstance(result, str):
            self.iso_verified.emit(result)

    def _on_action_complete(self, success: bool, result: Any) -> None:
        '''Result handler for Actions.dispatch tasks'''
        if not success or not isinstance(result, ActionResult):
            logger.error(f'Action task failed unexpectedly: {result}')
            return
        
        if result.status == ActionStatus.FAILURE:
            logger.error(f'Action "{result.action_name}" failed: {result.message}')
            self.action_complete.emit(result)
            return
        
        action_def = Registry.get_action(result.node, result.action_name)
        if not action_def:
            self.action_complete.emit(result)
            return
        
        match action_def.action_type:
            case ActionType.IMPORT:
                if isinstance(result.payload, bytes):
                    self.apply_edit(result.node, result.payload)
                    self.rebuild_log.emit(f'Import applied to {result.node.name}')
            case ActionType.TREE_EXPAND:
                if self.vfs:
                    new_nodes: list[VfsNode] = []
                    if isinstance(result.payload, VfsNode):
                        new_nodes = result.payload.children or [result.payload]
                    elif isinstance(result.payload, list):
                        new_nodes = result.payload
                    else:
                        logger.warning(f'TREE_EXPAND action returned unexpected payload type: {type(result.payload)}')
                    if new_nodes:
                        self.vfs.insert_children(result.node, new_nodes)
                        result.node.expansion_pending = False
                        logger.info(f'Inserted {len(new_nodes)} nodes into: {result.node.hierarchical_id_str}')
            case _:
                pass # PROCESS / DIALOG / EXPORT -- UI handled via action_complete

        self.action_complete.emit(result)

    def _on_decode_done(
        self, 
        success: bool, 
        result: Any, 
        node: VfsNode, 
        on_success: Callable[[], None]    | None,
        on_failure: Callable[[str], None] | None
    ) -> None:
        '''Callback for when handler finishes decoding'''
        if success and isinstance(result, bytes):
            original = self.get_node_data(node) if node not in self.tracker._originals else b''
            self.tracker.mark_modified(node, result, original)
            logger.info(f'Edit applied after decode: {node.name}')
            if on_success:
                on_success()
        else:
            error_msg = str(result) if not success else f'decode returned {type(result).__name__}'
            logger.error(f'Decode failed: {error_msg}')
            if on_failure:
                on_failure()
