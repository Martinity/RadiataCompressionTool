'''
Dispatcher handles most of the coordination work between the UI and logic
Functions as a signal proxy
'''
from __future__ import annotations
from pkgutil import extend_path
from wsgiref.util import request_uri

import functools
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from PyQt6.QtCore import pyqtSignal, QObject, Qt, QTimer

from core.registry import Registry
from core.node import VfsManager, ModTracker, VfsNode
from core.workers import TaskCoordinator, ActionStatus, ActionResult, Actions, ActionType, TaskHandle
from core.navigator import VfsNavigator
from core.metadata_manager import NodeMetadataStore
from core.extension_overrides import lookup_extension
if TYPE_CHECKING:
    from core.contracts import BaseHandler, BaseEditor

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------------------------------- Dispatch -------------------------------------------------###

class Dispatcher(QObject):
    '''
    Bridge between UI and logic

    Node actions pass to Actions.dispatch which routes by ActionDef.action_type.
    Dispatcher does not need to now what an action needs to execute.
    '''
    # Tree / Tracker
    iso_loaded        = pyqtSignal(object)           # [root] | None (failed)
    expand_requested  = pyqtSignal(object, object)   # (VfsNode, wait_event)
    node_changed      = pyqtSignal(VfsNode)          # Update for TreeView
    tracking_update   = pyqtSignal(int, int)         # (modified_count, staged_count)
    # Page transitions
    rebuild_requested = pyqtSignal(list)             # For MainWindow page swap
    # Rebuild Task
    rebuild_progress = pyqtSignal(int)               # completion %
    rebuild_log      = pyqtSignal(str)               # log for rebuild page
    rebuild_complete = pyqtSignal(bool, str)         # (success, message)
    # ISO verification
    iso_verified = pyqtSignal(str)                   # Build string
    # Generic node actions
    action_complete  = pyqtSignal(object)            # ActionResult
    file_browser_log = pyqtSignal(str)               # Testing usefulness of log signalling
    # IO
    io_progress = pyqtSignal(int, str)               # completion %
    io_complete = pyqtSignal(bool, object)           # (success, result)

    def __init__(self) -> None:
        super().__init__()
        self._main_thread_id = threading.get_ident()
        self.vfs:                    VfsManager | None = None
        self.active_handler:        BaseHandler | None = None
        self.nav:                  VfsNavigator | None = None
        self._metadata_store: NodeMetadataStore | None = None
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

        self.expand_requested.connect(self._handle_expand_request, Qt.ConnectionType.QueuedConnection) # type: ignore this is valid

    def set_metadata_store(self, store: NodeMetadataStore) -> None:
        self._metadata_store = store

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
        task_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.file_browser_log.emit)
        task_handle.finished.connect(self._on_action_complete)
        return []  # signal populates the tree ^^^

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
        editor: BaseEditor | None = None,
        on_success: Callable[[], None]    | None = None,
        on_failure: Callable[[str], None] | None = None,
    ) -> None | TaskHandle:
        '''Pushes changes to the tracker.
        If data is not bytes, dispatches to a background worker to decode the payload
        Notifies the editor when finished'''
        if isinstance(data, bytes): # Bytes type payload passthrough
            original = self.get_node_data(node) if node not in self.tracker._originals else b''
            self.tracker.mark_modified(node, data, original)
            logger.info(f'Modified: "{node.name}" added to rebuild queue.')
            if on_success:
                on_success()
            return

        handler_class = (
            Registry.get_handler_for_editor(editor) if editor is not None else
            Registry.get_handler(node)
        )
        if not handler_class:
            error_msg = f'No handler registered for "{node.name}" to compile payload.'
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
            self.rebuild_log.emit if self._rebuild_active else self.file_browser_log.emit
        )
        task_handle.finished.connect(
            functools.partial(self._on_decode_done, node, on_success, on_failure)
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
        task_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.file_browser_log.emit)
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
        task_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.file_browser_log.emit)
        task_handle.finished.connect(self._on_action_complete)

    def start_iso_rebuild(self, output_path: Path) -> TaskHandle | None:
        if not self.active_handler or not self.vfs or not self.nav:
            self.rebuild_complete.emit(False, 'No ISO Loaded.')
            return
        self._rebuild_active = True

        staged_nodes = list(self.tracker.rebuild_queue)
        self.rebuild_log.emit(f'Preparing to build {len(staged_nodes)} staged file(s)...')

        # Use start_dedicated_task (not start_task) so the rebuild runs on a
        # dedicated thread outside the bounded QThreadPool.  The rebuild blocks
        # waiting for expansion tasks that must themselves run on the pool; if
        # the rebuild occupied a pool slot it could exhaust the pool and deadlock.
        task_handle = self.task_coordinator.start_dedicated_task(
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

    def resolve_ghost_node(self, target_hid: tuple[int, ...], on_success: Callable[[VfsNode], None]) -> None:
        '''Asynchronously unpack the VFS until the target hid is reached'''
        if not self.vfs or not self.nav:
            return
        self._drill_down_to(target_hid, on_success)

    def _drill_down_to(self, target_hid: tuple[int, ...], on_success: Callable[[VfsNode], None]) -> None:
        '''Recursively expand the VFS until target_hid becomes reachable.
        Called on the main thread; each async layer connects back via _on_layer_done.'''
        if not self.vfs:
            return
        node = self.vfs.get_vfs_node_by_id(target_hid)
        if node:
            on_success(node)
            return

        ancestor = self.vfs.find_nearest_ancestor(target_hid)
        if not ancestor:
            logger.error(f'Cannot resolve {target_hid}: No ancestor exists.')
            return
        if not hasattr(self, '_pending_drills'):
            self._pending_drills: dict[tuple[int, ...], list[tuple[tuple[int, ...], Callable]]] = {}
        if getattr(ancestor, 'expansion_pending', False):
            logger.debug(f'Expansion already running for {ancestor.name}. Queuing {target_hid}')
            self._pending_drills[ancestor.hierarchical_id].append((target_hid, on_success))
            return
        profile = Registry.get_handler_profile(ancestor)
        action_def = profile.primary_expand_action() if profile else None
        if not action_def:
            logger.error(f'Cannot expand {ancestor.name}: No TREE_EXPAND action registered')
            return
        ancestor.expansion_pending = True
        self._pending_drills[ancestor.hierarchical_id] = [(target_hid, on_success)]
        logger.debug(f'Drilling down to {ancestor.name} ({ancestor.hierarchical_id_str})')
        handle = self.task_coordinator.start_task(
            Actions.dispatch,
            action_def,
            ancestor,
            self.nav,
        )
        handle.log_message.connect(self.file_browser_log.emit)
        handle.finished.connect(functools.partial(self._on_layer_done, ancestor.hierarchical_id))

    def _on_layer_done(
        self,
        ancestor_hid: tuple[int, ...],
        success: bool,
        result: Any,
    ) -> None:
        '''Promoted slot for resolve_ghost_node layer completion.
        Context params (target_hid, on_success) bound by functools.partial;
        signal params (success, result) appended by Qt.'''
        if threading.get_ident() != self._main_thread_id:
            logger.error("_on_layer_done ran off the main thread")
        self._on_action_complete(success, result)
        if not self.vfs:
            return
        ancestor = self.vfs.get_vfs_node_by_id(ancestor_hid)
        if ancestor:
            ancestor.expansion_pending = False
        queued_expansions = self._pending_drills.pop(ancestor_hid, [])
        if success:
            for target_hid, on_success in queued_expansions:
                self._drill_down_to(target_hid, on_success)
        else:
            logger.error('Failed to drill down to target node...')

    def _on_expand_done(
        self,
        node: VfsNode,
        wait_event: threading.Event,
        success: bool,
        result: Any,
    ) -> None:
        '''Promoted slot for _handle_expand_request expansion task.
        Context params (node, wait_event) bound by functools.partial;
        signal params (success, result) appended by Qt.'''
        if threading.get_ident() != self._main_thread_id:
            logger.error("_on_expand_done ran off the main thread")
        self._on_action_complete(success, result)
        node.finish_expansion()
        wait_event.set()

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
        if not self._metadata_store:
            logger.debug(f'No file metadata loaded... {self._metadata_store}')

        task_handle = self.task_coordinator.start_task(
            Actions.load_iso,
            handler_class,
            path
        )
        task_handle.log_message.connect(self.file_browser_log.emit)
        task_handle.finished.connect(self._on_iso_loaded)
        return task_handle

    def _migrate_targets_if_needed(self) -> None:
        store = self._metadata_store
        if store is None:
            return
        has_targets = any(meta.target_hid is not None for meta in store._db.values())
        if has_targets:
            return
        logger.info('No target entries found - running DatacenterTargets migration.')
        count = store.ingest_datacenter_targets()
        count += store.ingest_metadata()
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

        # Dedup: a task is already in-flight for this node (set below when we start one).
        # The running task will call finish_expansion() which sets node._expansion_event
        # (== wait_event after the begin_expansion fix), so the caller is unblocked without
        # us starting a duplicate task.
        if node._expansion_task_active:
            return

        profile = Registry.get_handler_profile(node)
        action_def = profile.primary_expand_action() if profile else None
        if not action_def or not self.nav:
            logger.warning(f'Cannot expand node: {node.hierarchical_id_str}, missing expand actions or navigator.')
            wait_event.set()
            return

        node.expansion_pending = True
        node._expansion_task_active = True  # Guard against duplicate tasks (cleared in finish_expansion)

        logger.debug('Starting expansion background task')
        task_handle = self.task_coordinator.start_task(
            Actions.dispatch,
            action_def,
            node,
            self.nav,
        )
        task_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.file_browser_log.emit)
        task_handle.finished.connect(functools.partial(self._on_expand_done, node, wait_event))

    def _handle_extension_request(self, node: VfsNode, auto_save: bool = False) -> None:
        '''
        Fulfill extension request from the VFS when metadata return '.bin' (default extension)
        Wastefully reads the entire node into memory before returning,
        shouldn't matter too much since this only is triggered on nodes that had no extension enrichment
        '''
        if node.extension != '.bin':
            return

        def _check_pk(header: bytes) -> str:
            offset_header = int.from_bytes(header[0x10:0x14], 'little')
            pk3_magic = 0x004E000
            if offset_header % pk3_magic == 0:  # header is pk3 divisible
                return '.pk3'  # pk3 header
            return '.bin'

        header: bytes = self.get_node_data(node)[:0x30]
        ext = lookup_extension(header, _check_pk(header))
        node.extension = ext
        if auto_save and self._metadata_store is not None:
            self._metadata_store.register(node.hierarchical_id_str, extension=ext)
        logger.debug(f'Extension request fulfilled: {node.name} -> {ext}')

    def _on_iso_loaded(self, success: bool, result: object) -> None:
        '''Takes the ISO's root+children nodes and intializes:
        VfsManager -> VfsNavigator -> metadata -> and signals'''
        if threading.get_ident() != self._main_thread_id:
            logger.error("_on_iso_loaded ran off the main thread")
        from core.workers import LoadIsoResult
        if not isinstance(result, LoadIsoResult) or not success:
            msg = result.error if isinstance(result, LoadIsoResult) else str(result)
            logger.error(f'ISO load failed: {msg}')
            self.iso_loaded.emit(None)
            return
        handler, root = result.handler, result.root
        if not root:
            logger.error('ISO load succeeded but no root node was returned')
            self.iso_loaded.emit(None)
            return
        self.active_handler = handler
        self.vfs = VfsManager(
            root,
            root.children[-1],
            node_enricher=(self._metadata_store.enrich if self._metadata_store else None)
        )
        self.vfs.request_extension.connect(self._handle_extension_request)
        self.vfs.enrich_initial_tree() # This populates node names/categories/extensions from metadata, thus is now crucial
        self.nav = VfsNavigator(self.vfs, self.get_node_data, self._expand_node)
        if handler is not None:
            # I think doing this on mainthread is fine since when this fires it is not possible for there to be any node registration
            build = handler.get_build(root)  # type: ignore
            QTimer.singleShot(0, lambda: self.iso_verified.emit(build))
        # self._migrate_targets_if_needed()   # Uncomment for building metadata from scratch

        # TODO: move verify action to new menu_bar
        # verify_handle = self.task_coordinator.start_task(Actions.verify_iso, handler)
        # verify_handle.log_message.connect(self.rebuild_log.emit if self._rebuild_active else self.file_browser_log.emit)
        # verify_handle.finished.connect(self._on_iso_verified)
        self.iso_loaded.emit([root])

    def _on_rebuild_finished(self, success: bool, result: Any) -> None:
        '''Verify type of result and pack signal'''
        self._rebuild_active = False
        if isinstance(result, ActionResult):
            msg = result.message or ('Rebuild succeeded.' if success else 'Rebuild failed.')
        else:
            msg = str(result)
        self.rebuild_complete.emit(success, msg)
        if self.nav:
            self.nav.clear_rollup_pending()
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
                # edits get applied via fallback handler, prevents silent failing
                self.apply_edit(result.node, result.payload, editor=None)
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
                        logger.debug(f'Inserted {len(new_nodes)} nodes into: {result.node.hierarchical_id_str}')
            case _:
                pass # PROCESS / DIALOG / EXPORT -- UI handled via action_complete

        self.action_complete.emit(result)

    def _on_decode_done(
        self,
        node: VfsNode,
        on_success: Callable[[], None]    | None,
        on_failure: Callable[[str], None] | None,
        success: bool,
        result: Any,
    ) -> None:
        '''Callback for when handler finishes decoding.
        Context params (node, on_success, on_failure) are bound via functools.partial;
        signal params (success, result) are appended by Qt when emitted.'''
        if threading.get_ident() != self._main_thread_id:
            logger.error("_on_decode_done ran off the main thread")
        if success and isinstance(result, bytes):
            original = self.get_node_data(node) if node not in self.tracker._originals else b''
            self.tracker.mark_modified(node, result, original)
            logger.info(f'Modified: "{node.name}" added to rebuild queue.')
            if on_success:
                on_success()
        else:
            error_msg = str(result) if not success else f'decode returned {type(result).__name__}'
            logger.error(f'Decode failed: {error_msg}')
            if on_failure:
                on_failure(error_msg)
