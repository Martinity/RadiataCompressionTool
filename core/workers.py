'''
Contains all background task defining logic. 

- ActionType and ActionDef are used when registering profiles as well as for routing logic to distinct UI outcomes

- Tasks section manages threading and associated signals

- Actions routes background tasks to the appropriate logic.

'''
from __future__ import annotations

import threading
from pathlib import Path
from enum import auto, Enum
from dataclasses import dataclass
from typing import Callable, Any, TYPE_CHECKING, NamedTuple
from PyQt6.QtCore import pyqtSignal, QObject, pyqtSlot, QRunnable, QThreadPool
from core.contracts import LeafHandler, ContainerHandler
if TYPE_CHECKING:
    from core.node import VfsNode
    from core.contracts import BaseHandler, PhysicalHandler
    from core.handlers.iso_container import IsoHandler
    from core.navigator import VfsNavigator

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------------- Action Types -------------------------------------###

class ActionType(Enum):
    '''
    Defines how the disatcher and Action.dispatch handle results

    TREE_EXPAND  - execute_action returns a VfsNode — dispatcher inserts children
    PROCESS      - execute_action returns node data in Any format — stored as payload
    DIALOG       - execute_action returns a display string — shown in descriptor panel or dialog
    EXPORT       - write node data to disk
    IMPORT       - read file from disk into node
    '''
    TREE_EXPAND = 'tree_expand'
    PROCESS     = 'process'
    DIALOG      = 'dialog'
    EXPORT      = 'export'
    IMPORT      = 'import'

@dataclass(frozen=True)
class ActionDef:
    '''Describes a single action.
    name is the key passed to execute_action and the context menu label'''
    name:        str
    action_type: ActionType

###---------------------------------------- Results ----------------------------------------###

class ActionStatus(Enum):
    SUCCESS = auto()
    FAILURE = auto()

@dataclass
class ActionResult:
    action_name: str
    node:        VfsNode
    status:      ActionStatus
    payload:     Any = None  # result of the action (bytes, str... etc) depending on ActionType
    message:     str = ''

@dataclass 
class EditorPayload:
    '''Result structured for editors. Carries node, data'''
    node: 'VfsNode'
    data: Any

class LoadIsoResult(NamedTuple):
    '''Payload for _on_iso_loaded'''
    handler: PhysicalHandler
    root:    VfsNode

###---------------------------------------- Tasks ------------------------------------------###

class GenericTask(QRunnable):
    '''Generic background worker'''
    def __init__(self, handle: TaskHandle, function: Callable, *args, **kwargs) -> None:
        super().__init__()
        self.handle  = handle
        self.fn      = function
        self.args    = args
        self.kwargs  = dict(kwargs)

    @pyqtSlot()
    def run(self) -> None:
        '''Execute the function, catch errors, and advance the state machine'''
        self.handle.mark_running()
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.handle.complete(result)
        except InterruptedError as e:
            logger.warning(f'Task aborted: {e}')
            self.handle.fail('Cancelled by user')
        except Exception as e:
            logger.error('Background task failed', exc_info=True)
            self.handle.fail(e)

class TaskCoordinator(QObject):
    def __init__(self):
        super().__init__()
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(4)

    def start_task(self, function: Callable, *args, **kwargs) -> TaskHandle:
        '''Spin up thread. Link handle'''
        task_name = function.__name__
        handle = TaskHandle(task_name)

        kwargs['task_handle'] = handle

        worker = GenericTask(handle, function, *args, **kwargs)
        self.thread_pool.start(worker)
        return handle
    
    def shutdown(self):
        logger.info('TaskCoordiantor: Canceling pending tasks...')
        self.thread_pool.clear()
        if not self.thread_pool.waitForDone(2000):
            logger.warning('TaskCoordinator: Some threads did not finish in time.')

class TaskHandle(QObject):
    '''State machine and handle for background tasks, thread-safe'''
    state_changed = pyqtSignal(str)           # State name
    progress      = pyqtSignal(int, str)      # (percentage, message)
    finished      = pyqtSignal(bool, object)  # (success, payload)
    log_message   = pyqtSignal(str)           # log output

    _VALID_TRANSITIONS: dict[str, set[str]] = {
        'pending':    {'running', 'cancelled'},
        'running':    {'cancelling', 'completed', 'failed'},
        'cancelling': {'cancelled'},
        'completed':  set(),
        'failed':     set(),
        'cancelled':  set()
    }

    def __init__(self, task_name: str):
        super().__init__()
        self.task_name  = task_name
        self._state     = 'pending'
        self._lock      = threading.Lock()
        self._cancel_token = threading.Event()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def _transition(self, target: str) -> None:
        with self._lock:
            valid = self._VALID_TRANSITIONS.get(self._state, set())
            if target not in valid:
                logger.warning(f'Task {self.task_name}: invalid transition {self._state}->{target}')
                return
            self._state = target
        self.state_changed.emit(target)
        logger.debug(f'Task [{self.task_name}]: -> {target}')

    ### from main thread
    def cancel(self) -> None:
        '''Cancel the current task from the main thread'''
        with self._lock:
            if self._state not in ('pending', 'running'):
                return
            target = 'cancelled' if self._state == 'pending' else 'cancelling'
            self._cancel_token.set()
            self._state = target
        self.state_changed.emit(target)
        logger.debug(f'Task [{self.task_name}]: -> {target}')

    ### from background thread
    def is_cancelling(self) -> bool:
        return self._cancel_token.is_set()

    def checkpoint(self) -> None:
        if self.is_cancelling():
            raise InterruptedError(f'Task [{self.task_name}] cancelled at checkpoint.')

    def mark_running(self) -> None:
        self._transition('running')

    def complete(self, result: object) -> None:
        if self.is_cancelling():
            self._transition('cancelled')
            self.finished.emit(False, 'Task cancelled by user.')
        else:
            self._transition('completed')
            self.finished.emit(True, result)
    
    def fail(self, error: Exception | str) -> None:
        self._transition('failed')
        self.finished.emit(False, str(error))

###---------------------------------------- Actions --------------------------------------###

class Actions:
    '''All background task logic in one place

    log_callback: user-facing messages (LoggingWindow)
    logger.*:     system/debug messages (LoggingWindow w/ Log Level Filtering)
    progress_callback(%, label): progress bar
    '''

    ### Editor
    @staticmethod
    def prepare_editor(
        handler_class:     type['BaseHandler'],
        node:             'VfsNode',
        navigator:        'VfsNavigator',
        task_handle:       TaskHandle
    ) -> EditorPayload:
        '''
        Unwraps the node and calls handler.prepare_editor_data() on a background thread.

        Returns EditorPayload(node, data) 
        '''
        task_handle.log_message.emit(f'Preparing editor data for "{node.name}"...')
        raw_bytes = node.pending_data or navigator.unwrap_chain(node)
        if not raw_bytes:
            raise ValueError(f'unwrap_chain returned empty bytes for "{node.name}"')
        header_bytes = navigator.resolve_data_from_hid(node.target)
        if not issubclass(handler_class, (ContainerHandler, LeafHandler)):
            raise TypeError(
                f'{handler_class.__name__} must be ContainerHandler or LeafHandler.'
            )
        with handler_class(raw_bytes, node.parent) as handler:
            handler.task_handle = task_handle
            setattr(handler, 'datacenter_header', header_bytes)
            result = handler.prepare_editor_data(node, raw_bytes)
        logger.debug(f'prepare_editor: {node.name} -> {type(result).__name__} from {handler_class.__name__}')
        return EditorPayload(node=node, data=result)
    
    @staticmethod
    def decode_editor_data(
        handler_class:     type['BaseHandler'],
        node:             'VfsNode',
        payload:           Any,
        task_handle:       TaskHandle
    ) -> bytes:
        '''
        Takes a complex UI payload and routes it through the node's handler
        converting it back to raw bytes on a background thread
        '''
        task_handle.log_message.emit(f'Decoding data for {node.name}')
        if not issubclass(handler_class, (ContainerHandler, LeafHandler)):
            raise TypeError(f'{handler_class.__name__} must be ContainerHandler or LeafHandler.')
        with handler_class(b'', node.parent) as handler:
            handler.task_handle = task_handle
            result = handler.decode_editor_data(node, payload)
        if not isinstance(result, bytes):
            raise TypeError(f'Handler {handler_class.__name__} returned {type(result).__name__}, expected bytes')
        task_handle.log_message.emit(f'Editor payload for {node.name} encoded to bytes')
        return result

    ### Entry point for all node actions
    @staticmethod
    def dispatch(
        action_def: 'ActionDef',
        node:       'VfsNode',
        navigator:  'VfsNavigator',
        task_handle:       TaskHandle,
        **kwargs,
    ) -> ActionResult:
        '''
        Route an action based on ActionDef.action_type.
        Dispatcher calls this for node actions. It should never need to know what to execute
        '''
        from core.registry import Registry
        match action_def.action_type:
            case ActionType.TREE_EXPAND | ActionType.PROCESS | ActionType.DIALOG:
                handler_class = Registry.get_handler(node)
                if not handler_class:
                    return ActionResult(
                        action_name=action_def.name, 
                        node=node, 
                        status=ActionStatus.FAILURE, 
                        message=f'No handler registered for {node.name}'
                    )
                return Actions.run_handler_action(
                    handler_class, node, action_def.name, navigator, 
                    task_handle, **kwargs
                )
            case ActionType.EXPORT:
                file_path: Path | None = kwargs.get('file_path')
                if not file_path:
                    return ActionResult(
                        action_name=action_def.name, node=node,
                        status=ActionStatus.FAILURE, message='No output path provided'
                    )
                handler_class = Registry.get_handler(node)
                profile = Registry.get_handler_profile(node)
                has_custom_export = (handler_class and profile and profile.get_action(action_def.name) is not None)
                if has_custom_export: # Custom Export - PNG, WAV... etc
                    return Actions.run_handler_action(
                        handler_class,
                        node,
                        action_def.name,
                        navigator,
                        task_handle,
                        **kwargs
                    )
                # Fallback - Raw Bytes
                return Actions.export_node(
                    node, file_path, navigator, task_handle
                )
            case ActionType.IMPORT:
                file_path = kwargs.get('file_path')
                if not file_path:
                    return ActionResult(
                        action_name=action_def.name, node=node,
                        status=ActionStatus.FAILURE, message='No import path provided'
                    )
                return Actions.import_node(
                    node, file_path, task_handle
                )
            case _:
                return ActionResult(
                    action_name=action_def.name, node=node,
                    status=ActionStatus.FAILURE,
                    message=f'Unknown ActionType: {action_def.action_type}'
                )
            
    ### ISO Specific actions
    @staticmethod
    def load_iso(
        handler_class: type,
        path:          Path,
        task_handle:   TaskHandle,
    ) -> tuple:
        '''Read the TOC and split the disk into it's physical files'''
        task_handle.checkpoint()
        handler = handler_class(path, None)
        task_handle.checkpoint()
        root = handler.get_file_tree()
        handler.release_handle()
        return LoadIsoResult(handler, root)

    @staticmethod
    def rebuild_iso(
        handler:      'IsoHandler',
        root_node:    'VfsNode',
        navigator:    'VfsNavigator',
        staged_nodes: list['VfsNode'],
        output_path:  Path,
        task_handle:       TaskHandle,
    ) -> ActionResult:
        try:
            task_handle.log_message.emit('Starting ISO build sequence...')
            task_handle.progress.emit(0, 'Starting Pass 1...')
            task_handle.log_message.emit('Precomputing Datacenter...')

            extra_targets: list[VfsNode] = navigator.precompute_datacenter(staged_nodes, task_handle)
            all_staged:    list[VfsNode] = list(staged_nodes) + extra_targets
            task_handle.log_message.emit(f'Pass 1 complete - {len(extra_targets)} datacenter target(s) cached and queued')

            task_handle.progress.emit(0, 'Starting Pass2...')
            task_handle.log_message.emit('Performing VFS rollup...')

            physical_staged_nodes = navigator.rollup_nodes(all_staged, task_handle)
            task_handle.progress.emit(0, 'Writing sectors to disk...')
            success = handler.rebuild_node(root_node, physical_staged_nodes, output_path, task_handle)

            if success:
                task_handle.log_message.emit('ISO Build Successful.')
                return ActionResult(
                    action_name='Build ISO',
                    node=root_node, 
                    status=ActionStatus.SUCCESS,
                )
            raise ValueError('handler.rebuild_node returned false')
        except Exception as e:
            logger.error(f'Rebuild failed: {e}', exc_info=True)
            return ActionResult(
                action_name='Build ISO',
                node=root_node,
                status=ActionStatus.FAILURE,
                message=str(e)
            )
        
    @staticmethod
    def verify_iso(
        handler: IsoHandler,
        task_handle:       TaskHandle,
    ) -> str:
        task_handle.log_message.emit('Verifying ISO...')
        result = handler.verify_iso_integrity(task_handle)
        task_handle.log_message.emit(f'ISO verified, build: {result}')
        return result
    
    ### IO specific actions
    @staticmethod
    def export_node(
        node:        'VfsNode',
        output_path:  Path, 
        navigator:   'VfsNavigator',
        task_handle:  TaskHandle,
    ) -> ActionResult:
        try:
            task_handle.log_message.emit(f'Resolving data chain for {node.hierarchical_id_str}')
            data = navigator.unwrap_chain(node)
            if not data:
                raise ValueError('Resolved data is empty')
            total_size = len(data)
            chunk_size = 1024 * 1024 * 10
            with open(output_path, 'wb') as f:
                for i in range(0, total_size, chunk_size):
                    task_handle.checkpoint()
                    f.write(data[i : i + chunk_size])

            return ActionResult(
                action_name='Export',
                node=node,
                status=ActionStatus.SUCCESS,
                message=f'Saved to {output_path.name}'
            )
        except Exception as e:
            logger.error(f'Export failed: {e}', exc_info=True)
            return ActionResult(
                action_name='Export',
                node=node,
                status=ActionStatus.FAILURE,
                message=str(e)
            )
        
    @staticmethod
    def import_node(
            node:       'VfsNode',
            import_path: Path,
            task_handle:       TaskHandle
    ) -> ActionResult:
        try:
            task_handle.log_message.emit(f'Importing {import_path.name}...')
            data = import_path.read_bytes()
            task_handle.log_message.emit(f'Loaded {len(data)} bytes from {import_path.name}')
            return ActionResult(
                action_name='Import and Replace',
                node=node,
                status=ActionStatus.SUCCESS,
                payload=data
            )
        except Exception as e:
            logger.error(f'Import failed: {e}', exc_info=True)
            return ActionResult(
                action_name='Import and Replace',
                node=node,
                status=ActionStatus.FAILURE, message=str(e)
            )
    
    ### Handler actions
    @staticmethod
    def run_handler_action(
        handler_class: type[BaseHandler],
        node:          VfsNode,
        action_name:   str,
        navigator:     VfsNavigator,
        task_handle:       TaskHandle,
        **kwargs,
    ) -> ActionResult:
        '''
        Find requested data, 
        open a handle, 
        inject handle with dependencies, 
        execute action with handle
        '''
        if action_name != 'Properties':
            task_handle.log_message.emit(f'Starting "{action_name}" on node: {node.name}...')
        try:
            node_bytes   = navigator.unwrap_chain(node)
            header_bytes = navigator.resolve_data_from_hid(node.target)
            if not issubclass(handler_class, (ContainerHandler, LeafHandler)):
                raise TypeError(f'{handler_class.__name__} must be ContainerHandler or LeafHandler.')
            with handler_class(node_bytes, node.parent) as handler:
                handler.task_handle = task_handle
                setattr(handler, 'datacenter_header', header_bytes)
                payload = handler.execute_action(node, action_name, **kwargs)
            if action_name != 'Properties':
                task_handle.log_message.emit(f'Finished "{action_name}" on node: {node.name}.')
            return ActionResult(
                action_name=action_name, 
                node=node, 
                status=ActionStatus.SUCCESS, 
                payload=payload
            )
        except Exception as e:
            logger.error(f'Action "{action_name}" failed on {node.name}: {e}', exc_info=True)
            return ActionResult(
                action_name=action_name, 
                node=node, 
                status=ActionStatus.FAILURE, 
                message=str(e)
            )