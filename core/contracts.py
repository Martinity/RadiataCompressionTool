'''Contracts for handlers and editors'''
from __future__ import annotations

import io
import abc
from pathlib import Path
from typing import Any, Callable, NamedTuple, TYPE_CHECKING
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import pyqtSignal
from core.node import VfsNode
if TYPE_CHECKING:
    from core.workers import TaskHandle

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###-------------------------------------------- Special Return --------------------------------------------------###

class RebuildResult(NamedTuple):
    '''Structured return value for handlers that mutate linked nodes (kods w/datacenter)'''
    payload:     bytes
    target_data: bytes | None = None

###---------------------------------------------- Base Handler contract ------------------------------------------------###

class BaseHandler(abc.ABC):
    """
    The architectural blueprint for data interpretation.
    
    A Handler acts as a translator between raw binary data and the VfsNode 
    hierarchy. It is responsible for 'unpacking' a format's internal structure 
    and 'repacking' modifications back into a valid binary stream.
    
    Lifecycle:
        1. Instantiated by a Worker or Navigator with source bytes.
        2. get_file_tree() maps internal entries to VfsNodes.
        3. execute_action() performs specific logic (e.g., decompression).
    """ 
    def __init__(self, parent_node: VfsNode | None = None) -> None:
        '''Initialize the root handle and provide generic resource management'''
        self.parent_node = parent_node
        self.handle: io.IOBase | None = None
        self.owns_handle: bool = False
        self.task_handle: TaskHandle | None = None

    def __enter__(self):
        return self
    
    def __exit__(self, *args) -> None:
        self.close()

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} identity="{self.get_identity()}">'
    
    def get_identity(self) -> str:
        '''Reads the stamp from the registration'''
        return getattr(self.__class__, '_plugin_name', 'Unknown Handler')

    def close(self):
        '''Close the stream if owned by this instance'''
        if self.owns_handle and self.handle and not self.handle.closed:
            try:
                self.handle.close()
                logger.debug('Closed handler resources successfully.')
            except Exception as e:
                logger.error(f'Error while closing handler: {e}')
            finally:
                self.owns_handle = False
                self.handle = None

    def execute_action(
        self, 
        node:             VfsNode, 
        action_name:       str, 
        **kwargs,
    ) -> Any:
        '''Execute custom actions registered with the Registry'''
        if action_name == 'Properties':
            logger.info(f'Properties not implemented for {self.get_identity()}')
            return None
        logger.warning(f'{self.__class__.__name__} has not implemented action: {action_name}')
        return None
    
    def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> Any:
        '''Processed data return directly to the editor. 
        Use this for things such as audio decoding, swizzling...etc 
        to keep entensive data processing off Main thread and prevent UI from freezing.
        '''
        return raw_bytes
    
    def decode_editor_data(self, node: VfsNode, payload: Any, **kwargs) -> bytes:
        '''Process the modified node data back into raw bytes'''
        if isinstance(payload, bytes):
            return payload
        raise NotImplementedError(
            f'{self.__class__.__name__} must implement decode_editor_data to handle decoding non-bytes payloads'
        )

    @abc.abstractmethod
    def get_file_tree(self) -> VfsNode:
        '''Return the root VfsNode representing this format's internal structure.'''
 
    @abc.abstractmethod
    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode]) -> bytes | RebuildResult:
        '''Return rebuilt container bytes incorporating pending edits. + any dependant node bytes'''
 
    @abc.abstractmethod
    def get_raw_node(self, node: VfsNode) -> bytes:
        '''Return raw original bytes for a node, bypassing any pending edits.'''

###------------------------------------------ Specialized Handler Contracts ----------------------------------------###

class PhysicalHandler(BaseHandler):
    '''Used exclusively for physical disk archives (e.g. ISO)
    - Source must be a physical Path
    - Manages file-handles directly
    - Scanning and maintaining top-level tree structures
    '''
    def __init__(self, source_path: Path, parent_node: VfsNode | None = None) -> None:
        super().__init__(parent_node)
        self.path        = source_path
        self.handle      = open(source_path, 'rb')
        self.owns_handle = True

    def release_handle(self) -> None:
        '''
        Release the init handle after get_file_tree().
        Subsequent physical node access must open private handles with source_path
        '''
        if self.handle and not self.handle.closed:
            self.handle.close()
        self.handle = None
        self.owns_handle = False

    @abc.abstractmethod
    def rebuild_node(
        self, 
        node:         VfsNode, 
        staged_nodes: list[VfsNode], 
        output_path:  Path,
        task_handle:  TaskHandle,
        ) -> bool:
        '''Rebuild and write a collection of nodes to disk. Returns True on success'''

class ContainerHandler(BaseHandler):
    '''Used for virtual archives (e.g. SLZ, Kods)
    Class exists purely as a typing landmark so VfsNavigator can distinguish handler purposes
    without parsing extensions or magic
    - Source must be memory-based (bytes or stream)
    - Maintains relationships and alignments between nodes
    '''
    def __init__(self, source_data: bytes, parent_node: VfsNode | None = None) -> None:
        super().__init__(parent_node)
        self.handle      = io.BytesIO(source_data)
        self.owns_handle = True

class LeafHandler(BaseHandler):
    '''Used for individual, isolated file objects (e.g. IO)
    Provides minimal/passthrough stubs of all abstract methods so subclasses
    only need to implement execute_action for their specific logic.
    - Source must be memory-based (bytes or stream)
    - Never sees any relational data
    - Purely used for tasks where the node is the input for specific logic (e.g. parsing a JPG)
    '''
    def __init__(self, source_data: bytes, parent_node: VfsNode | None = None) -> None:
        super().__init__(parent_node)
        self.handle: io.BytesIO = io.BytesIO(source_data)
        self.owns_handle = True

    def get_file_tree(self) -> VfsNode:
        '''Leaf nodes do not contain children'''
        return VfsNode(name='raw_data')
    
    def get_raw_node(self, node: VfsNode) -> bytes:
        self.handle.seek(0)
        return self.handle.read()
    
    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode]) -> bytes:
        if node in staged_nodes and node.pending_data and self.task_handle:
            self.task_handle.log_message.emit(f'Node has been modified. Original size:{node.size} New size:{len(node.pending_data)}')
            return node.pending_data
        self.handle.seek(0)
        return self.handle.read()

###---------------------------------------------- Widget contract ----------------------------------------------###

class _ABCMetaQtMeta(type(QWidget), abc.ABCMeta): # type: ignore 
    '''Merge PyQt6 widget metaclass with ABC metaclass'''

class BaseEditor(QWidget, metaclass=_ABCMetaQtMeta):
    '''
    Minimum required implementation for a mutable editor:

        _populate_ui(data)            render the payload in your widget
        current_data() -> Any         return the current widget state for saving
                                        (needed for non-bytes types data)

    Minimum required implementation for a read-only editor:

        Inherit from BaseViewer instead
        _populate_ui(data)            render the payload in your widget

    Optional:
        undo() / redo()               implement and emit undo_state_changed
        confirm_changes_applied()     called on save success
        show_error(message)           override the error display for custom UIs
        cleanup()                     release resources (timers, sinks, handles)
    '''
    undo_state_changed = pyqtSignal(bool, bool) # (can_undo, can_redo)
    dataChanged = pyqtSignal(bool)              # Data changed bool
    is_mutable = True

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.current_node:      VfsNode | None = None
        self._is_dirty:         bool           = False
        self._original_payload: Any            = None
        self._pending_data:     Any            = None
        self._data_resolver:    Callable[['VfsNode'], bytes] | None = None

    def __repr__(self) -> str:
        node_name = self.current_node.name if self.current_node else "None"
        return f"<{self.__class__.__name__} node='{node_name}' dirty={self.is_dirty()}>"

    def __str__(self) -> str:
        return f"{self.__class__.__name__} for {self.current_node or 'no node'}"
    
    ### Data handling
    def begin_loading(self, node: VfsNode) -> None:
        '''Called when editor is open for data loading feedback, while waiting for BG thread'''
        self.current_node = node

    def receive_data(self, result: Any, data_resolver: Callable[[VfsNode], bytes] | None = None) -> None:
        '''
        Called when the BG thread is done data processing
        (default) if result is bytes, stores as original data and call _populate_ui(result). 
        Override for handlers that return non-bytes results.
        '''
        self._data_resolver = data_resolver
        self._original_payload = result
        if isinstance(result, bytes):
            self.set_dirty(False)
            self._populate_ui(result)
        else:
            self.show_error(
                f'Receive data got {type(result).__name__}, expected bytes.'
                f'Override receive_data or ensure handler.prepare_editor_data returns bytes.'
            )
            
    @abc.abstractmethod
    def _populate_ui(self, data: Any) -> None:
        '''Populate the editor with return from associated handler.prepare_editor_data'''

    def undo(self) -> None:
        pass

    def redo(self) -> None:
        pass

    ### Lifecycle
    def cleanup(self) -> None:
        '''Editor Destructor'''
        self.current_node      = None
        self._original_payload = None
        self._data_resolver    = None
        self.set_dirty(False)

    ### Data access
    def request_node_data(self, target_node: VfsNode) -> bytes:
        if self._data_resolver:
            return self._data_resolver(target_node)
        logger.warning(f'Data resolver not initialized. Cannot fetch data for {target_node.name}')
        return b''
    
    def current_data(self) -> Any:
        '''Return the live state'''
        return self._original_payload

    def snapshot(self) -> None:
        '''Freeze the state'''
        self._pending_data = self.current_data()
    
    ### Mutability management
    def confirm_changes_applied(self) -> None:
        '''Called by dispatcher after handler successfully applied decoded editor data to node'''
        if self._pending_data is not None:
            self._original_payload = self._pending_data
            self._pending_data  = None
        self.set_dirty(False)

    def reject_changes_applied(self, reason: str) -> None:
        '''Called by dispatcher when handler failed to decode editor data'''
        self._pending_data = None
        logger.error(f'{self.__class__.__name__} save rejected: {reason}')

    def discard_changes(self) -> None:
        '''Reverts the node data back to original state'''
        if self.is_dirty() and self.current_node:
            self._pending_data = None
            self._populate_ui(self._original_payload)
            self.set_dirty(False)

    def set_dirty(self, state: bool):
        '''Track node changes'''
        self._is_dirty = state
        self.dataChanged.emit(state)

    def is_dirty(self) -> bool:
        '''Get node status'''
        return self._is_dirty
    
    def show_error(self, message: str) -> None:
        '''Called when prepare_editor fails. Override for custom UI output.'''
        logger.error(f'{self.__class__.__name__}: {message}')

###----------------------------------- Read Only Editors ----------------------------------###

class BaseViewer(BaseEditor):
    '''Convenience base for Read Only Editors.
    provides no-ops for all mutable logic'''
    is_mutable = False

    def set_dirty(self, state: bool):
        pass
