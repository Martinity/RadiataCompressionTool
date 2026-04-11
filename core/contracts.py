'''Contracts for handlers and editors.
- Basehandler: format logic (ISO, SLZ, Kods... etc)
- BaseEditorWidget: file editors (hex... etc)
- Utility widgets (logger, properties) inherit QWidget
'''
from __future__ import annotations
from typing import TYPE_CHECKING, Union

from pathlib import Path
import io
import abc
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import pyqtSignal
if TYPE_CHECKING:
    from core.node import VfsNode


###---------------------------------------------- Base Handler contracts ------------------------------------------------###

class BaseHandler(abc.ABC):
    '''Abstract Base Class for all handlers. Classes that inherit from this must implement:\n
    @abstractmethod read_file_tree\n
    @abstractmethod read_file_data\n
    @abstractmethod rebuild_file_data\n
    get_identity is suggested for debugging
    '''
    def __init__(self, source: Union[Path, io.BufferedIOBase, bytes]):
        '''Initialize the root handle'''
        self.path = source if isinstance(source, Path) else None

        if isinstance(source, Path):
            self.handle = open(source, 'rb')
        elif isinstance(source, bytes):
            self.handle = io.BytesIO(source)
        else:
            self.handle = source

    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} path='{self.path}' status='{self.get_identity()}'>"

    def __str__(self):
        return self.get_identity()
    
    @staticmethod
    def get_supported_actions() -> list[str]:
        '''Returns a list of format-specific actions.'''
        return []

    @abc.abstractmethod
    def get_file_tree(self) -> VfsNode:
        '''Returns list of virtual nodes representing internal files.'''
        pass

    @abc.abstractmethod
    def read_file_data(self, node: VfsNode, absolute_offset: int) -> bytes:
        '''Return the original node data using the absolute offset. Bypass pending edits'''
        pass

    @abc.abstractmethod
    def rebuild_file_data(self, output_path: Path, virtual_tree: VfsNode):
        '''Rebuild the container using pending edits'''
        pass

    def get_identity(self) -> str:
        '''Override for build name'''
        return 'Unknown format'

    def close(self):
        '''Close the handle'''
        if hasattr(self, 'handle') and not self.handle.closed:
            self.handle.close()

###---------------------------------------------- Widget contract ----------------------------------------------###

class _ABCMetaQtMeta(type(QWidget), abc.ABCMeta):
    '''Merge PyQt6 widget metaclass with ABC metaclass'''
    pass

class BaseEditorWidget(QWidget, metaclass=_ABCMetaQtMeta):
    '''
    Abstract Base Class for editors. All editor widgets inherit from this class and must implement:\n
    @abstractmethod load_node\n
    @abstractmethod get_modified_data
    '''
    dataChanged = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_node = None
        self._is_dirty = False

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
        