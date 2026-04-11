'''Complex UI mapping handler'''
from PyQt6.QtCore import QAbstractItemModel, QModelIndex, Qt, QSortFilterProxyModel, QStringListModel
from core.node import VfsNode
import logging
logger = logging.getLogger(f'radiata.{__name__}')

class VfsTreeModel(QAbstractItemModel):
    '''Responsible for all tree gui graphics'''
    def __init__(self, root_node, parent=None):
        super().__init__(parent)
        self.root_node = root_node
        self.columns = ["File Name", "LBA", "Size (Sectors)", "Extension"]

    # Build the grid for rendering
    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def rowCount(self, parent=QModelIndex()):
        """Returns the number of children under a specific parent."""
        if parent.column() > 0:
            return 0

        parent_node = self.get_node(parent)
        return len(parent_node.children)

    def index(self, row, column, parent=QModelIndex()):
        """Creates a QModelIndex pointing to a specific child."""
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        parent_node = self.get_node(parent)
        child_node = parent_node.children[row]

        # internalPointer binds our Python object to the C++ UI index
        if child_node:
            return self.createIndex(row, column, child_node)
        return QModelIndex()

    def parent(self, index):
        """Finds the parent of a given index."""
        if not index.isValid():
            return QModelIndex()

        child_node = index.internalPointer()
        parent_node = child_node.parent

        # If the parent is the root node, we return an empty index (top level)
        if parent_node == self.root_node or parent_node is None:
            return QModelIndex()

        # Otherwise, create an index for the parent based on ITS row
        return self.createIndex(parent_node.row(), 0, parent_node)

    # Draw data
    def get_node(self, index):
        """Helper to extract our VfsNode from a Qt index."""
        if index.isValid():
            node = index.internalPointer()
            if node:
                return node
        return self.root_node

    def data(self, index, role):
        if not index.isValid():
            return None

        node = index.internalPointer()

        # What text to display in the UI cells
        if role == Qt.ItemDataRole.DisplayRole:
            col = index.column()
            if col == 0: return node.name  # noqa: E701
            if col == 1: return str(node.offset)  # noqa: E701
            if col == 2: return str(node.size)  # noqa: E701
            if col == 3: return node.extension  # noqa: E701

        # Critical: Return the node itself if the context menu asks for it!
        if role == Qt.ItemDataRole.UserRole:
            return node

        return None

    def headerData(self, section, orientation, role):
        """Draws the column headers at the top of the TreeView."""
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section]
        return None

    # Handle redrawing of dynamic elements
    def add_children_to_node(self, parent_index, child_nodes: list['VfsNode']):
        """Expects pre-constructed VfsNodes from the Core logic."""
        parent_node = self.get_node(parent_index)
        logger.debug(f"Inserting {len(child_nodes)} children under {parent_node.name}")

        start_row = len(parent_node.children)
        end_row = start_row + len(child_nodes) - 1

        self.beginInsertRows(parent_index, start_row, end_row)

        for node in child_nodes:
            parent_node.append_child(node)

        self.endInsertRows()
        logger.info(f"Tree updated — node {parent_node.name} now has {len(parent_node.children)} children")

    def set_root(self, new_root_node):
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
        if self.active_category == 'All' or self.active_category is None:
            return True
        
        index = self.sourceModel().index(source_row, 0 , source_parent)
        node = index.data(Qt.ItemDataRole.UserRole)

        if isinstance(node, VfsNode):
            return node.category == self.active_category
        
        return False