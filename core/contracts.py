'''Contracts for handlers and editors.
- Basehandler: format logic (ISO, SLZ, Kods... etc)
- BaseEditorWidget: file editors (hex... etc)
- Utility widgets (logger, properties) inherit QWidget
'''
from __future__ import annotations

import io
import abc
from pathlib import Path
from typing import Any, Callable
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import pyqtSignal
from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')


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
        node:             'VfsNode', 
        action_name:       str, 
        progress_callback: Callable | None = None,
        log_callback:      Callable | None = None,
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
        Overwrite return type for complexe editors
        '''
        return raw_bytes

    @abc.abstractmethod
    def get_file_tree(self) -> 'VfsNode':
        '''Return the root VfsNode representing this format's internal structure.'''
 
    @abc.abstractmethod
    def rebuild_node(self, node: 'VfsNode', staged_nodes: list['VfsNode']) -> bytes:
        '''Return rebuilt container bytes incorporating pending edits.'''
 
    @abc.abstractmethod
    def get_raw_node(self, node: 'VfsNode') -> bytes:
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

    @abc.abstractmethod
    def rebuild_node(
        self, 
        node:         VfsNode, 
        staged_nodes: list[VfsNode], 
        output_path:  Path,
        progress_callback: Callable | None = None,
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

    def get_file_tree(self) -> 'VfsNode':
        '''Leaf nodes do not contain children'''
        return VfsNode(name='raw_data')
    
    def get_raw_node(self, node: 'VfsNode') -> bytes:
        self.handle.seek(0)
        return self.handle.read()
    
    def rebuild_node(self, node: 'VfsNode', stage_nodes: list['VfsNode']) -> bytes:
        self.handle.seek(0)
        return self.handle.read()
    

###---------------------------------------------- Widget contract ----------------------------------------------###

class _ABCMetaQtMeta(type(QWidget), abc.ABCMeta): # type: ignore 
    '''Merge PyQt6 widget metaclass with ABC metaclass'''

class BaseEditor(QWidget, metaclass=_ABCMetaQtMeta):
    '''
    Base class for all editor widgets.

    WorkspaceController calls editor.begin_loading(node) on the man thread
        the editor shows a placeholder until data is ready
    Actions.prepare_editor runs on a worker thread
        raw_bytes = navigator.unwrap_chain(node)
        result = handler.prepare_editor_data(node, raw_bytes)
    WorspaceController calls editor.receive_data(result) on the main thread.
        eidtor populates itself with the processed result.

    For simple editors (ex. HexEditorWidget) the handler returns raw bytes
    For complexe editors (ex. FisEditorWidget) the handler returns a processed result (QImage, FISInfo)

    Mutability
    BaseEditor, is_mutable = True (default) 
        Editors function as Editors
    BaseViewer, is_mutable = False
        Provides no-op for dirty/apply/discard. Editors function as Viewers.
    '''
    apply_requested = pyqtSignal(object, bytes) # (VfsNode, new raw data for node)
    dataChanged = pyqtSignal(bool)
    is_mutable = True

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.current_node:   VfsNode | None = None
        self._is_dirty:      bool           = False
        self._original_data: bytes          = b''
        self._data_resolver: Callable[['VfsNode'], bytes] | None = None

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
        if isinstance(result, bytes):
            self._original_data = result
            self.set_dirty(False)
            self._populate_ui(result)
        else:
            logger.error(
                f'{self.__class__.__name__} return {type(result).__name__}. Override receive_data for non-bytes results '
            )
            
    @abc.abstractmethod
    def _populate_ui(self, data: bytes) -> None:
        '''Populate the editor with data from the BG thread'''

    ### Lifecycle
    def cleanup(self) -> None:
        '''Editor Destructor'''
        self.current_node = None
        self._original_data = b''
        self._data_resolver = None
        self.set_dirty(False)

    ### Data access
    def request_node_data(self, target_node: VfsNode) -> bytes:
        if self._data_resolver:
            return self._data_resolver(target_node)
        logger.warning(f'Data resolver not initialized. Cannot fetch data for {target_node.name}')
        return b''
    
    def get_modified_data(self) -> bytes:
        '''Return the current state of loaded node'''
        return self._original_data
    
    ### Mutability management
    def apply_changes(self) -> None:
        '''Pushes changes to the Dispatcher/ModTracker'''
        if self.is_dirty() and self.current_node:
            new_data = self.get_modified_data()
            self.apply_requested.emit(self.current_node, new_data)
            self._original_data = new_data
            self.set_dirty(False)

    def discard_changes(self) -> None:
        '''Reverts the UI back to the original state'''
        if self.is_dirty() and self.current_node:
            self._populate_ui(self._original_data)
            self.set_dirty(False)

    def set_dirty(self, state: bool):
        '''Track node changes'''
        self._is_dirty = state
        self.dataChanged.emit(state)

    def is_dirty(self) -> bool:
        '''Get node status'''
        return self._is_dirty

###----------------------------------- Read Only Editors ----------------------------------###

class BaseViewer(BaseEditor):
    '''Convenience base for used for Read Only Editors.
    provides no-ops for all mutabiility functions'''
    is_mutable = False

    def set_dirty(self, state: bool):
        pass

    def apply_changes(self) -> None:
        pass

    def discard_changes(self) -> None:
        pass

    def get_modified_data(self) -> bytes:
        return self._original_data
    
