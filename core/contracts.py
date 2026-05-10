'''Contracts for handlers and editors.
- Basehandler: format logic (ISO, SLZ, Kods... etc)
- BaseEditorWidget: file editors (hex... etc)
- Utility widgets (logger, properties) inherit QWidget
'''
from __future__ import annotations
from typing import TYPE_CHECKING, Any, Callable

from pathlib import Path
import io
import abc
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import pyqtSignal

if TYPE_CHECKING:
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
        self.handler: io.BufferedIOBase | None = None
        self.owns_handle: bool = False

    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_value, exc_traceback) -> None:
        self.close()

    def close(self):
        '''Close the stream if owned by this instance'''
        if self.owns_handle and self.handle:
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
        node: VfsNode, 
        action_name: str, 
        progress_callback: Callable[[int, str], None],
        log_callback: Callable[[str], None],
        **kwargs,
    ) -> Any:
        '''Execute custom actions registered with the Registry'''
        pass

    @abc.abstractmethod
    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode]) -> bytes:
        '''Pack children back into parent container using pending edits if any'''
        pass

###------------------------------------------ Specialized Handler Contracts ----------------------------------------###

class PhysicalHandler(BaseHandler, abc.ABC):
    '''Used exclusively for physical disk archives (e.g. ISO)
    - Source must be a physical Path
    - Manages file-handles directly
    - Scanning and maintaining top-level tree structures
    '''
    def __init__(self, source: Path, parent_node: VfsNode | None = None) -> None:
        super().__init__(parent_node)
        if not isinstance(source, Path):
            raise TypeError(f'PysicalHandler expects a Path object, got: {type(source)}')
        self.path = source
        self.handle = open(source, 'rb')
        self.owns_handle = True

    @abc.abstractmethod
    def get_file_tree(self) -> VfsNode:
        '''Interfaces with the physical disk to return mapped virtual VfsNodes'''
        pass

class ContainerHandler(BaseHandler, abc.ABC):
    '''Used for virtual archives (e.g. SLZ, Kods)
    - Source must be memory-based (bytes or stream)
    - Maintains relationships and alignments between nodes
    '''
    def __init__(self, source: bytes | io.BufferedIOBase, parent_node: VfsNode | None = None) -> None:
        super().__init__(parent_node)
        if isinstance(source, bytes):
            self.handle = io.BytesIO(source)
        elif isinstance(source, io.BufferedIOBase):
            self.handle = source
        else:
            raise TypeError(f'ContainerHandler expects bytes or stream, got: {type(source)}')
        
    @abc.abstractmethod
    def get_file_tree(self) -> VfsNode:
        '''Generate metadata entries and map them to child VfsNodes'''
        pass

class LeafHandler(BaseHandler, abc.ABC):
    '''Used for individual, isolated file objects (e.g. IO)
    - Source must be memory-based (bytes or stream)
    - Never sees any relational data
    - Purely used for tasks where the node is the input for specific logic (e.g. parsing a JPG)
    '''
    def __init__(self, source: bytes | io.BufferedIOBase, parent_node: VfsNode | None = None) -> None:
        super().__init__(parent_node)
        if isinstance(source, bytes):
            self.handler = io.BytesIO(source)
        elif isinstance(source, io.BufferedIOBase):
            self.handle = source
        else:
            raise TypeError(f'LeafHandler expects bytes or stream, got: {type(source)}')

    def get_file_tree(self) -> VfsNode:
        '''Leaf nodes do not contain children'''
        return VfsNode(name='raw_data')
    

###---------------------------------------------- Widget contract ----------------------------------------------###

class _ABCMetaQtMeta(type(QWidget), abc.ABCMeta): # type: ignore 
    '''Merge PyQt6 widget metaclass with ABC metaclass'''
    pass

class BaseEditor(QWidget, metaclass=_ABCMetaQtMeta):
    '''
    Abstract Base Class for editors. All editor widgets inherit from this class and must implement:\n
    @abstractmethod load_node\n
    @abstractmethod get_modified_data
    '''
    apply_requested = pyqtSignal(object, bytes) # (VfsNode, new raw data for node)
    dataChanged = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.current_node: VfsNode | None = None
        self._is_dirty: bool = False

        # self._check_abstract_methods()

    def __repr__(self) -> str:
        node_name = self.current_node.name if self.current_node else "None"
        return f"<{self.__class__.__name__} node='{node_name}' dirty={self.is_dirty()}>"

    def __str__(self) -> str:
        return f"{self.__class__.__name__} for {self.current_node or 'no node'}"
    
    # Previous hack for instantiation, revert to this if _ABCMetaQtMeta is failing
    # def _check_abstract_methods(self):
    #     '''Instantiate ABC and PyQt for BaseWidget class. 
    #     Manual instantiation required to get around the hierarchy clash'''
    #     for method_name in ['load_node', 'get_modified_data']:
    #         method = getattr(self, method_name, None)
    #         if not method or getattr(method, '__isabstractmethod__', False):
    #             raise TypeError(f"Can't instantiate {self.__class__.__name__} without implementing '{method_name}'")

    @abc.abstractmethod
    def load_node(self, node: VfsNode, data: bytes):
        '''Populate the widget with data from the node'''
        self.current_node = node
        self.set_dirty(False)

    @abc.abstractmethod
    def get_modified_data(self) -> bytes:
        '''Return the current state of loaded node'''
        pass

    def set_dirty(self, state: bool):
        '''Track node changes'''
        self._is_dirty = state
        self.dataChanged.emit(state)

    def is_dirty(self) -> bool:
        '''Get node status'''
        return self._is_dirty

    def clear(self):
        '''Reset the widget'''
        self.current_node = None
        self.set_dirty(False)
        