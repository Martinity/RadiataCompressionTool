'''Complex UI mapping handler'''
from __future__ import annotations

from PyQt6.QtCore import QAbstractItemModel, QModelIndex, Qt, QSortFilterProxyModel, QStringListModel
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

class VfsTreeModel(QAbstractItemModel):
    '''Responsible for all tree gui graphics'''
    def __init__(self, root_node, parent=None):
        super().__init__(parent)

        from core.node import VfsNode
        if not isinstance(root_node, VfsNode):
            raise TypeError(f"VfsTreeModel expected VfsNode, got {type(root_node).__name__}")
        
        self.root_node = root_node
        self.columns = ["ID", "File Name", "Size", "Extension"]

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
                return self._human_redable_size(node.size)
            if col == 3: 
                return node.extension

        # Critical: Return the node itself if the context menu asks for it!
        if role == Qt.ItemDataRole.UserRole:
            return node

        return None

    @staticmethod
    def _human_redable_size(size: int | None) -> str:
        '''Convert size to human readable strin'''
        if size is None or size < 0:
            return '-'
        if size == 0:
            return '0 B'
        
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        i = 0
        while size >= 1024 and i < len(units) -1:
            size /= 1024
            i+=1
        return f'{size:.1f} {units[i]}'
    
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

    ###----------------------------------------- Insertion ------------------------------------###

    def add_children_to_node(self, parent_index: QModelIndex, child_nodes: list[VfsNode]):
        """Insert new children under the parent index"""
        if not child_nodes:
            return
        
        parent_node = self.get_node(parent_index)
        logger.debug(f"Inserting {len(child_nodes)} children under {parent_node.name}")

        start_row = len(parent_node.children)
        end_row = start_row + len(child_nodes) - 1

        self.beginInsertRows(parent_index, start_row, end_row)
        for node in child_nodes:
            parent_node.append_child(node)
        self.endInsertRows()

        logger.info(f"Tree updated — node {parent_node.name} now has {len(parent_node.children)} children")

    def index_for_node(self, target_node: VfsNode, parent_index=QModelIndex()) -> QModelIndex:
        '''Find the QModelIndex (metadata) for a given VfsNode'''
        if target_node == self.root_node: # recursive bound check
            return QModelIndex()
        
        # Iterate through children
        rows = self.rowCount(parent_index)
        for r in range(rows):
            idx = self.index(r, 0, parent_index)
            node = self.get_node(idx)

            if node == target_node: # Match
                return idx
            
            if self.rowCount(idx) > 0:
                result = self.index_for_node(target_node, idx)
                if result.isValid():
                    return result
                
        return QModelIndex()

    def set_root(self, new_root_node: VfsNode):
        '''Build the entire tree'''
        self.beginResetModel()
        self.root_node = new_root_node
        self.endResetModel()

###--------------------------------------------- Category View ----------------------------------------------------###

class VfsCategoryModel(QStringListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.categories = ['All', 'System', 'FMV', 'Audio', 'Map', 
                           'Character', 'Monster', 'Prop', 'Equipment', 
                           'VFX', 'Scene Setup', 'Animation', 'Battle Animation']

###---------------------------------------------- Category Proxy -------------------------------------------###

class VfsCategoryProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.active_category: str | None = 'All'

    def set_category(self, category: str):
        self.active_category = category
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        index = self.sourceModel().index(source_row, 0 , source_parent)
        node = index.data(Qt.ItemDataRole.UserRole)

        from core.node import VfsNode
        if not isinstance(node, VfsNode):
            return False
        
        if getattr(node, 'is_hidden', False):
            return False

        if self.active_category == 'All' or self.active_category is None:
            return True
        
        return node.category == self.active_category