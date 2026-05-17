from __future__ import annotations

import threading
from pathlib import Path
from core.registry import Registry
from core.node import VfsManager, ModTracker, VfsNode
from core.workers import TaskCoordinator, ActionStatus, ActionResult, Actions, ActionType
from core.navigator import VfsNavigator
from PyQt6.QtCore import pyqtSignal, QObject, Qt
from typing import TYPE_CHECKING, Any

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
    # IO 
    io_progress = pyqtSignal(int, str)       # completion %
    io_complete = pyqtSignal(bool, object)   # (success, result)

    def __init__(self) -> None:
        super().__init__()
        self.vfs: VfsManager | None = None
        self.tracker = ModTracker()
        self.active_handler: BaseHandler | None = None
        self.task_coordinator = TaskCoordinator()
        self.nav: VfsNavigator | None = None
        self._setup_connections()

    def _setup_connections(self) -> None:
        '''Relay tracker signals to UI'''
        self.tracker.node_modified.connect(self.node_changed.emit)
        self.tracker.node_reverted.connect(self.node_changed.emit)
        self.tracker.rebuild_initiated.connect(self.rebuild_requested.emit)
        self.tracker.state_changed.connect(self._relay_tracking_state)

        self.expand_requested.connect(self._handle_expand_request, Qt.ConnectionType.QueuedConnection)

    def _relay_tracking_state(self):
        '''Emit counts so UI doesn't need to recalc'''
        self.tracking_update.emit(len(self.tracker.modified_nodes), len(self.tracker.rebuild_queue))

    def __str__(self) -> str:
        return f"Dispatcher(active_handler={self.active_handler})"

    ###----------------------------------- Public ----------------------------------------###

    def load_source(self, source: Path | VfsNode) -> list[VfsNode]:
        if isinstance(source, Path):
            handler_class = Registry.get_handler(source)
            if not handler_class:
                logger.warning(f'No handler for {source.name}')
                return []
            return self._load_physical(handler_class, source)
        
        if source.children or source.expansion_pending:
            return source.children or []
        
        profile = Registry.get_profile(source)
        if not profile:
            logger.warning(f'No profile or {source.name}, cannot expand.')
            return []
        
        action_def = profile.primary_expand_action()
        if not action_def:
            logger.debug(f'{source.name} has no TREE_EXPAND action')
            return []
        
        source.expansion_pending = True
        signals = self.task_coordinator.start_task(
            Actions.dispatch,
            action_def,
            source,
            self.nav,
        )
        signals.log_message.connect(self.rebuild_log.emit)
        signals.finished.connect(self._on_action_complete)
        return [] # signal populates the tree ^^^
    
    def get_node_data(self, node: VfsNode) -> bytes:
        '''Return the raw bytes of the requested node, unwrapping virutal to the physical source'''
        if node.pending_data is not None:
            return node.pending_data
        if node.is_physical:
            if not self.active_handler:
                logger.error(f'No physical handler for node: {node.hierarchical_id_str}')
                return b''
            return self.active_handler.get_raw_node(node)
        if not self.nav:
            logger.error(f'No VfsNavigator initialized. Cannot resolve node: {node.hierarchical_id_str}')
            return b''
        return self.nav.unwrap_chain(node)

    def apply_edit(self, node: VfsNode, data: Any, editor: BaseEditor | None = None) -> None:
        '''Pushes changes to the tracker.
        If data is not bytes, dispatches to a background worker to decode the payload
        Notifies the editor when finished'''
        if isinstance(data, bytes):
            self.tracker.mark_modified(node, data)
            logger.info(f'Edit applied directly: {node.name}')
            if editor:
                editor.confirm_changes_applied()
            return

        if not editor:
            return
        handler_class = Registry.get_handler(node)
        if not handler_class:
            error_msg = f'No handler registered for {node.name} to compile payload.'
            logger.error(error_msg)
            if editor:
                editor.reject_changes_applied(error_msg)
            return
        signals = self.task_coordinator.start_task(
            Actions.decode_editor_data,
            handler_class,
            node,
            data
        )
        signals.log_message.connect(self.rebuild_log.emit)
        logger.info('calling finished... _on_decode_done')
        signals.finished.connect(
            lambda success, result: self._on_decode_done(success, result, node, editor))

    def open_editor(self, node: VfsNode, editor: 'BaseEditor') -> Any:
        '''
        Start background data preparation for an editor
        
        Returns a TaskSignal of either:
            Success   - EditorPayload(node, data)
            Exception - (False, str)
        '''
        if not self.nav:
            logger.error('Navigator not initialised')
            return
        handler_class = Registry.get_handler(node)
        if not handler_class:
            logger.warning(f'No handler for {node.name} -- Falling back to generic handler, returning bytes')
            from core.handlers.generic_binary_handler import GenericBinaryHandler
            handler_class = GenericBinaryHandler

        signals = self.task_coordinator.start_task(
            Actions.prepare_editor,
            handler_class,
            node,
            self.nav,
        )
        signals.log_message.connect(self.rebuild_log.emit)
        return signals

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
        signals = self.task_coordinator.start_task(
            Actions.dispatch,
            action_def,
            node, 
            self.nav,
            **kwargs
            )
        signals.log_message.connect(self.rebuild_log.emit)
        signals.finished.connect(self._on_action_complete)

    def start_iso_rebuild(self, output_path: Path) -> None:
        if not self.active_handler or not self.vfs or not self.nav:
            self.rebuild_complete.emit(False, 'No ISO Loaded.')
            return
        
        staged_nodes = list(self.tracker.rebuild_queue)
        self.rebuild_log.emit(f'Preparing to build {len(staged_nodes)} staged file(s)...')

        signals = self.task_coordinator.start_task(
            Actions.rebuild_iso,
            self.active_handler,
            self.vfs.root,
            self.nav,
            staged_nodes,
            output_path,
        )
        signals.progress.connect(lambda pct, _msg: self.rebuild_progress.emit(pct))
        signals.log_message.connect(self.rebuild_log.emit)
        signals.finished.connect(self._on_rebuild_finished)

    def close(self) -> None:
        '''For exiting the dispatch'''
        if self.task_coordinator:
            self.task_coordinator.shutdown()
        if self.active_handler:
            self.active_handler.close()
        self.vfs            = None
        self.active_handler = None
        self.nav            = None
        self.tracker.clear()
        logger.debug('- File System Reset -')

    ###------------------------------ Helpers --------------------------------###

    def _load_physical(self, handler_class: type, path: Path) -> list[VfsNode]:
        '''helper for loading physical files'''
        if self.active_handler:
            self.active_handler.close()

        handler = handler_class(path, None)
        self.active_handler = handler
        root = handler.get_file_tree()
        self.vfs = VfsManager(root)
        self.nav = VfsNavigator(self.vfs, self.get_node_data, self._expand_node)
        logger.info(f'Workspace initialized with Root: {handler_class.__name__}')

        signals = self.task_coordinator.start_task(Actions.verify_iso, handler)
        signals.log_message.connect(self.rebuild_log.emit)
        signals.finished.connect(self._on_iso_verified)

        return [root]

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
        
        profile = Registry.get_profile(node)
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
        signals = self.task_coordinator.start_task(
            Actions.dispatch,
            action_def,
            node,
            self.nav,
        )
        signals.log_message.connect(self.rebuild_log.emit)
        signals.finished.connect(_on_expand_done)
        
    def _on_rebuild_finished(self, success: bool, result: Any) -> None:
        '''Verify type of result and pack signal'''
        if isinstance(result, ActionResult):
            msg = result.message or ('Rebuld succeeded.' if success else 'Rebuild failed.')
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

    def _on_decode_done(self, success: bool, result: Any, node: VfsNode, editor: BaseEditor) -> None:
        '''Callback for when handler finishes decoding'''
        logger.info('In _on_decode_done')
        if success and isinstance(result, bytes):
            self.tracker.mark_modified(node, result)
            logger.info(f'Edit applied after background compilation: {node.name}')
            if editor:
                editor.confirm_changes_applied()
        else:
            error_msg = str(result) if not success else f'Compilation returned {type(result).__name__}, expected bytes'
            logger.error(f'Payload compilation failed: {error_msg}')
            if editor:
                editor.reject_changes_applied(error_msg)
