'''Complex UI mapping handler'''
from __future__ import annotations

from PyQt6.QtCore import QAbstractItemModel, QModelIndex, Qt, QSortFilterProxyModel
from typing import TYPE_CHECKING
from core.node import VfsManager
from utilities import human_size
if TYPE_CHECKING:
    from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

class VfsTreeModel(QAbstractItemModel):
    '''Responsible for all tree gui data/graphics'''
    def __init__(self, vfs_manager: VfsManager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vfs_manager = vfs_manager
        self.root_node = self.vfs_manager.root
        self.columns = ["ID", "File Name", "Size"]

        # Catch VfsManager signals for updating tree
        self.vfs_manager.insert_start.connect(self._on_insert_start)
        self.vfs_manager.insert_finished.connect(self._on_insert_finished)

    ###---------------------------------------- Qt API --------------------------------------###

    def columnCount(self, parent=QModelIndex()) -> int:
        '''Draw the columns'''
        return len(self.columns)

    def rowCount(self, parent=QModelIndex()) -> int:
        """Returns the number of children under a specific parent."""
        if parent.column() > 0:
            return 0

        parent_node = self.get_node(parent)
        return len(parent_node.children)

    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
        """Creates a QModelIndex pointing to a specific child."""
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        parent_node = self.get_node(parent)
        if row >= len(parent_node.children):
            return QModelIndex()
        
        child_node = parent_node.children[row]
        return self.createIndex(row, column, child_node) if child_node else QModelIndex()

    def parent(self, index: QModelIndex) -> QModelIndex:
        """Finds the parent of a given index."""
        if not index.isValid():
            return QModelIndex()

        child_node: VfsNode = index.internalPointer()
        parent_node: VfsNode | None = child_node.parent

        # If the parent is the root node, we return an empty index (top level)
        if parent_node == self.root_node or parent_node is None:
            return QModelIndex()

        # Otherwise, create an index for the parent based on ITS row
        return self.createIndex(parent_node.row(), 0, parent_node)

    # Draw data
    def get_node(self, index: QModelIndex) -> VfsNode:
        """Helper to extract a VfsNode from a Qt index."""
        if index.isValid():
            node = index.internalPointer()
            if node:
                return node
        return self.root_node

    def data(self, index: QModelIndex, role: Qt.ItemDataRole):
        if not index.isValid():
            return None

        node: VfsNode = index.internalPointer()

        # What text to display in the UI cells
        if role == Qt.ItemDataRole.DisplayRole:
            col = index.column()
            if col == 0:
                return node.hierarchical_id_str
            if col == 1: 
                return node.name
            if col == 2: 
                return human_size(node.size)
            
        if role == Qt.ItemDataRole.UserRole:
            return node

        return None
    
    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        '''Enable selection and interaction with tree'''
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    
    def headerData(self, section: int, orientation: Qt.Orientation, role: Qt.ItemDataRole):
        """Draws the column headers at the top of the TreeView."""
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section]
        return None

    def set_root(self, new_root_node: VfsNode):
        '''Build the entire tree'''
        self.beginResetModel()
        self.root_node = new_root_node
        self.endResetModel()

    def _on_insert_start(self, parent: VfsNode, first: int, last: int) -> None:
        parent_index = self.index_for_node(parent)
        self.beginInsertRows(parent_index, first, last)

    def _on_insert_finished(self):
        self.endInsertRows()

    def index_for_node(self, target_node: VfsNode) -> QModelIndex:
        '''Get the QModelIndex for a node'''
        if target_node is None or target_node == self.root_node:
            return QModelIndex()
        return self.createIndex(target_node.row(), 0, target_node)

###---------------------------------------------- Category Proxy -------------------------------------------###

class TreeProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.active_category: str = 'All'
        self.show_hidden  = False
        self.search_query = ''
        self._descriptors = {}
        # self.setDynamicSortFilter(True)
        self.setRecursiveFilteringEnabled(True)
        self.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        '''Custom sorting logic for columns'''
        left_node: VfsNode = self.sourceModel().data(left, Qt.ItemDataRole.UserRole)
        right_node: VfsNode = self.sourceModel().data(right, Qt.ItemDataRole.UserRole)

        if not left_node or not right_node:
            return super().lessThan(left, right)
        
        col = left.column()

        if col == 0:
            try:
                left_parts = list(left_node.hierarchical_id)
                right_parts = list(right_node.hierarchical_id)
                return left_parts < right_parts
            except (ValueError, AttributeError):
                return left_node.hierarchical_id < right_node.hierarchical_id
        
        if col == 2:
            return left_node.size < right_node.size
        
        return super().lessThan(left, right)

    def set_category(self, category: str):
        self.active_category = category
        self.invalidateFilter()

    def set_show_hidden(self, show: bool):
        '''refreshes view with the hidden toggle'''
        self.show_hidden = show
        self.invalidateFilter()

    def set_search_query(self, query: str):
        self.search_query = query.lower().strip()
        self.invalidateFilter()

    def set_descriptors(self, data: dict):
        self._descriptors = data

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        index = self.sourceModel().index(source_row, 0 , source_parent)
        node = index.data(Qt.ItemDataRole.UserRole)

        if not node:
            return False

        if not self.show_hidden and node.is_hidden:
            return False

        if not self.search_query:
            return True

        query = self.search_query.lower()

        node_name = getattr(node, 'name', '')
        if query in node_name.lower(): # Search for name
             return True
        if query in node.hierarchical_id_str.lower(): # Search for ID
            return True
        from ui.ui_core import _DESCRIPTORS
        descriptor = _DESCRIPTORS.get(node.hierarchical_id_str, {})
        tags = descriptor.get('tags', [])

        if query.startswith('tag:'): # Seach only tags/clicked tag
            target_tag = query[4:].strip()
            return any(target_tag in t.lower() for t in tags)
        
        for tag in tags: # Search for tag
            if query in tag.lower():
                return True
        return False

### -------------------------------

class FlatSearchModel(QAbstractItemModel):
    '''A model that provides a falt list of the VFS nodes'''
    def __init__(self, vfs_manager, descriptors, parent=None):
        super().__init__(parent)
        self.vfs = vfs_manager
        self.descriptors = descriptors
        self.matches = []
        self._query = ''

    def set_query(self, query: str):
        self._query = query.lower()
        self.refresh()

    def refresh(self):
        self.beginResetModel()
        self.matches = []
        if self._query:
            self._search_recursive(self.vfs.root)
        self.endResetModel()
    
    def _search_recursive(self, node: VfsNode):
        match = self._query in node.name.lower()
        if not match:
            node_data = self.descriptors.get(node.hierarchical_id_str, {})
            tags = node_data.get('tags', [])
            match = any(self._query in tag.lower() for tag in tags)

        if match:
            self.matches.append(node)

        for child in node.children:
            self._search_recursive(child)

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self.matches)
    
    def data(self, index, role):
        if not index.isValid():
            return None
        node = self.matches[index.row()]

        if role == Qt.ItemDataRole.DisplayRole:
            return f'{node.name} ({node.hierarchical_id_str})'
        if role == Qt.ItemDataRole.UserRole:
            return node
        return None
    
    def columnCount(self, parent: QModelIndex) -> int:
        return 1