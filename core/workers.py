from __future__ import annotations

from PyQt6.QtCore import pyqtSignal, QObject, pyqtSlot, QRunnable, QThreadPool
from typing import Callable, Any, TYPE_CHECKING
from dataclasses import dataclass
from enum import auto, Enum
from pathlib import Path
if TYPE_CHECKING:
    from core.node import VfsNode
    from core.contracts import BaseHandler
    from core.handlers.iso_handler import IsoHandler
    from core.navigator import VfsNavigator

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------------- Action Types -------------------------------------###

class ActionType(Enum):
    '''
    Defines how the disatcher and Action.dispatch handle results

    TREE_EXPAND  - execute_action returns a VfsNode — dispatcher inserts children\n
    PROCESS      - execute_action returns node data in Any format — stored as payload\n
    DIALOG       - execute_action returns a display string — shown in descriptor panel or dialog\n
    EXPORT       - write node data to disk\n
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

###---------------------------------------- Tasks ------------------------------------------###

class TaskSignals(QObject):
    '''Signal for task states of background tasks'''
    finished    = pyqtSignal(bool, object)  # (Success, result_message)
    log_message = pyqtSignal(str)           # log output
    progress    = pyqtSignal(int, str)      # (percentage, status_message)

class GenericTask(QRunnable):
    '''Generic background worker'''
    def __init__(self, function: Callable, *args, **kwargs) -> None:
        super().__init__()
        self.fn      = function
        self.args    = args
        self.signals = TaskSignals()
        self.kwargs  = dict(kwargs)
        self.kwargs.setdefault('progress_callback', self.signals.progress.emit)
        self.kwargs.setdefault('log_callback', self.signals.log_message.emit)

    @pyqtSlot()
    def run(self) -> None:
        '''Execute the function, catch errors'''
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.signals.finished.emit(True, result)
        except Exception as e:
            logger.error('Background task failed', exc_info=True)
            self.signals.finished.emit(False, str(e))

class TaskCoordinator(QObject):
    def __init__(self):
        super().__init__()
        self.thread_pool = QThreadPool()
        self.thread_pool.setMaxThreadCount(4)

    def start_task(self, function: Callable, *args, **kwargs) -> TaskSignals:
        '''Setup task and its signals'''
        worker = GenericTask(function, *args, **kwargs)
        self.thread_pool.start(worker)
        return worker.signals
    
    def shutdown(self):
        logger.info('TaskCoordiantor: Canceling pending tasks...')
        self.thread_pool.clear()
        if not self.thread_pool.waitForDone(2000):
            logger.warning('TaskCoordinator: Some threads did not finish in time.')

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
        progress_callback: Callable,
        log_callback:      Callable,
    ) -> EditorPayload:
        '''
        Unwraps the node and calls handler.prepare_editor_data() on a worker thread.

        Returns EditorPayload(node, data) 
        '''
        log_callback(f'Preparing editor data for {node.name}...')
        progress_callback(20, 'Unwrapping nodes...')
        if node.pending_data:
            raw_bytes = node.pending_data
        else:
            raw_bytes = navigator.unwrap_chain(node)
        if not raw_bytes:
            raise ValueError(f'unwrap_chain returned empty bytes for {node.name}')
        header_bytes = navigator.resolve_data_from_hid(getattr(node, 'target', None))
        with handler_class(raw_bytes, node.parent) as handler:
            if header_bytes and hasattr(handler, 'datacenter_headers'):
                handler.datacenter_headers = header_bytes
            result = handler.prepare_editor_data(node, raw_bytes)
        progress_callback(100, 'Done')
        logger.debug(f'prepare_editor: {node.name} -> {type(result).__name__} from {handler_class.__name__}')
        return EditorPayload(node=node, data=result)
    
    @staticmethod
    def decode_editor_data(
        handler_class: type['BaseHandler'],
        node: 'VfsNode',
        payload: Any,
        progress_callback: Callable,
        log_callback: Callable,
    ) -> bytes:
        '''
        Takes a complex UI payload and routes it through the node's handler
        converting it back to raw bytes on a background thread
        '''
        log_callback(f'Decoding data for {node.name}')
        progress_callback(50, 'Decoding payload...')
        from core.contracts import PhysicalHandler
        if issubclass(handler_class, PhysicalHandler):
            raise TypeError(
                f'{handler_class.__name__} is a PhysicalHandler. decode_editor_data '
                'requires a ContainerHandler or LeafHandler.'
            )
        with handler_class(b'', node.parent) as handler:
            result = handler.decode_editor_data(node, payload)
        if not isinstance(result, bytes):
            raise TypeError(f'Handler {handler_class.__name__} returned {type(result).__name__}, expected bytes')
            
        progress_callback(100, 'Decoding complete')
        logger.debug('Editor payload encoded to bytes')
        return result

    ### Entry point for all node actions
    @staticmethod
    def dispatch(
        action_def: 'ActionDef',
        node:       'VfsNode',
        navigator:  'VfsNavigator',
        progress_callback: Callable,
        log_callback:      Callable,
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
                    progress_callback, log_callback, **kwargs
                )
            case ActionType.EXPORT:
                file_path: Path | None = kwargs.get('file_path')
                if not file_path:
                    return ActionResult(
                        action_name=action_def.name, node=node,
                        status=ActionStatus.FAILURE, message='No output path provided'
                    )
                return Actions.export_node(
                    node, file_path, navigator, progress_callback, log_callback
                )
            case ActionType.IMPORT:
                file_path = kwargs.get('file_path')
                if not file_path:
                    return ActionResult(
                        action_name=action_def.name, node=node,
                        status=ActionStatus.FAILURE, message='No import path provided'
                    )
                return Actions.import_node(
                    node, file_path, progress_callback, log_callback
                )
            case _:
                return ActionResult(
                    action_name=action_def.name, node=node,
                    status=ActionStatus.FAILURE,
                    message=f'Unknown ActionType: {action_def.action_type}'
                )
            
    ### ISO Specific actions
    @staticmethod
    def rebuild_iso(
        handler:      'IsoHandler',
        root_node:    'VfsNode',
        navigator:    'VfsNavigator',
        staged_nodes: list['VfsNode'],
        output_path:  Path,
        progress_callback: Callable,
        log_callback:      Callable,
    ) -> ActionResult:
        try:
            log_callback('Starting ISO build sequence...')
            progress_callback(10, 'Performing VFS rollup...')

            physical_staged_nodes = navigator.rollup_nodes(staged_nodes)
            progress_callback(40, 'Writing sectors to disk...')
            success = handler.rebuild_node(root_node, physical_staged_nodes, output_path, progress_callback=progress_callback)

            if success:
                progress_callback(100, 'Complete')
                log_callback('ISO Build Successful.')
                return ActionResult(
                    action_name='Build ISO',
                    node=root_node, 
                    status=ActionStatus.SUCCESS,
                )
            raise ValueError('Build completed but failed to return success.')
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
        progress_callback: Callable,
        log_callback:      Callable,
    ) -> str:
        log_callback('Verifying ISO...')
        progress_callback(10, 'Hashing disk...')
        result = handler.verify_iso_integrity()
        progress_callback(100, 'Done')
        log_callback(f'Build identified: {result}')
        logger.info(f'ISO verified: {result}')
        return result
    
    ### IO specific actions
    @staticmethod
    def export_node(
        node:        'VfsNode',
        output_path: Path, 
        navigator:   'VfsNavigator',
        progress_callback: Callable,
        log_callback:      Callable,
    ) -> ActionResult:
        try:
            log_callback(f'Resolving data chain for {node.hierarchical_id_str}')
            progress_callback(20, 'Unwrapping...')
            data = navigator.unwrap_chain(node)
            if not data:
                raise ValueError('Resolved data is empty')
            progress_callback(70, f'Writing {len(data)} bytes to disk...')
            with open(output_path, 'wb') as f:
                f.write(data)

            progress_callback(100, 'Export complete')
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
            progress_callback: Callable,
            log_callback: Callable,
    ) -> ActionResult:
        try:
            log_callback(f'Importing {import_path.name}...')
            progress_callback(10, 'Reading file...')
            data = import_path.read_bytes()
            progress_callback(100, 'Import complete')
            log_callback(f'Loaded {len(data)} bytes from {import_path.name}')
            return ActionResult(
                action_name='Import',
                node=node,
                status=ActionStatus.SUCCESS,
                payload=data
            )
        except Exception as e:
            logger.error(f'Import failed: {e}', exc_info=True)
            return ActionResult(
                action_name='Import',
                node=node,
                status=ActionStatus.FAILURE, message=str(e)
            )
    
    ### Container actions
    @staticmethod
    def run_handler_action(
        handler_class: type['BaseHandler'],
        node:         'VfsNode',
        action_name:   str,
        navigator:    'VfsNavigator',
        progress_callback: Callable,
        log_callback:      Callable,
        **kwargs,
    ) -> ActionResult:
        '''Unwrap the node, resolve datacenter headers, instantiate the handler,
        and call execute_action.  All I/O happens on the worker thread.'''
        log_callback(f'{action_name} on node: {node.hierarchical_id_str}...')
        try:
            node_bytes   = navigator.unwrap_chain(node)
            if not node_bytes:
                raise ValueError(f'unwrap_chain returned empty bytes for {node.name}')
            header_bytes = navigator.resolve_data_from_hid(getattr(node, 'target', None))

            with handler_class(node_bytes, node.parent) as handler:
                if header_bytes and hasattr(handler, 'datacenter_headers'):
                    handler.datacenter_headers = header_bytes
                payload = handler.execute_action(node, action_name, progress_callback, log_callback, **kwargs)

            log_callback(f'{action_name} complete.')
            logger.debug(f'Action "{action_name}" succeeded for {node.name}')
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