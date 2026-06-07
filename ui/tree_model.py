'''Holds all models for workspace. In other words takes nodes and converts their data into file browser like formats.
VfsTreeModel - QAbstractItemModel, the hierarchical tree
SearchModel - QAbstractListModel, the search list. List due to recursive hierarchical searching chugging UI
TreeProxyModel - QSortFilterProxyModel, only gets applied to the tree. Search is hardcoded to avoid hidden nodes'''
from __future__ import annotations


from dataclasses import dataclass
from typing import TYPE_CHECKING
from PyQt6.QtCore import QAbstractItemModel, QModelIndex, Qt, QSortFilterProxyModel, QAbstractListModel
from PyQt6.QtGui import QColor
from core.node import VfsManager
from utilities import human_size
if TYPE_CHECKING:
    from core.node import VfsNode
    from core.descriptor_manager import NodeDescriptorStore

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
                return node.name + node.extension
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
        self.show_hidden  = False
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
            return list(left_node.hierarchical_id) < list(right_node.hierarchical_id)
        if col == 2:
            return left_node.size < right_node.size
        return super().lessThan(left, right)

    def set_show_hidden(self, show: bool):
        '''refreshes view with the hidden toggle'''
        logger.debug(f'hidden toggle from proxy model:{show}')
        self.show_hidden = show
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        index = self.sourceModel().index(source_row, 0 , source_parent)
        node = index.data(Qt.ItemDataRole.UserRole)
        if not node:
            return False
        if not self.show_hidden and node.is_hidden:
            return False
        return True

### -------------------------------------- Flat Search Model -------------------------------------------###
    
_RANK_EXACT_NAME  = 100
_RANK_HID         = 90
_RANK_NAME_PREFIX = 80
_RANK_NAME        = 60
_RANK_TAG         = 40
_RANK_DESCRIPTION = 10

@dataclass
class _SearchEntry:
    '''Stores: node, name, hid, tokens, base_rank, tags. Calculates score.'''
    node:       VfsNode
    name_lower: str
    hid_str:    str
    desc_lower: str
    tags_lower: tuple[str, ...]
    tokens:     frozenset[str]
    base_rank:  int
    tags:       tuple[str, ...]

    def score(self, query: str) -> int:
        '''return score for query. Higher is more relevant'''
        if not query:
            return 0

        if ':' in query: # Filter Search
            prefix, _, val = query.partition(':')
            val = val.lower().strip()
            if not val:
                return 0
            if prefix in ('tag', 'tags'): # Tag filter
                return _RANK_TAG + self.base_rank if any(val in t for t in self.tags_lower) else 0
            if prefix in ('desc', 'description'): # Description filter
                return _RANK_DESCRIPTION + self.base_rank if val in self.desc_lower else 0
            if prefix == 'name': # Name filter
                if val == self.name_lower:
                    return _RANK_EXACT_NAME + self.base_rank
                if self.name_lower.startswith(val):
                    return _RANK_NAME_PREFIX + self.base_rank
                if val in self.name_lower:
                    return _RANK_NAME + self.base_rank
                return 0
            if prefix == 'hid': # HID filter
                if self.hid_str.startswith(val) or val == self.hid_str:
                    return _RANK_HID
                return 0

        # Global Search
        if query == self.name_lower: # Name exact
            return _RANK_EXACT_NAME + self.base_rank
        if self.name_lower.startswith(query): # Name startswith
            return _RANK_NAME_PREFIX + self.base_rank
        if query in self.name_lower: # Name contains
            return _RANK_NAME + self.base_rank
        if self.hid_str.startswith(query) or query == self.hid_str: # HID match
            return _RANK_HID
        for token in self.tokens:
            if query == token: # Exact token match
                return _RANK_TAG + self.base_rank if self.base_rank > 0 else _RANK_DESCRIPTION
            if token.startswith(query): # Patial token match
                return _RANK_DESCRIPTION + self.base_rank
        return 0

@dataclass
class _QueryResult:
    '''_SearchEntry, score'''
    entry: _SearchEntry
    score: int

class FlatSearchModel(QAbstractListModel):
    '''Flat list of search results. Built as nodes are inserted'''
    TagsRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self,  vfs: VfsManager, descriptor_store: NodeDescriptorStore, parent=None,) -> None:
        super().__init__(parent)
        self._store    = descriptor_store
        self._index:   list[_SearchEntry] = []
        self._results: list[_QueryResult] = []
        self._query    = ''

        self._index_subtree(vfs.root)
        vfs.insert_start.connect(self._on_insert_start)
        vfs.insert_finished.connect(self._on_insert_finished)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._results)
    
    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        '''Returns different type of data depending on requested Role'''
        if not index.isValid() or index.row() >= len(self._results):
            return None
        result = self._results[index.row()]
        entry = result.entry

        if role == Qt.ItemDataRole.DisplayRole: # Name
            return f'{entry.node.name}  ({entry.hid_str})'
        if role == Qt.ItemDataRole.UserRole: # Node
            return entry.node
        if role == self.TagsRole: # Tags
            return entry.tags
        if role == Qt.ItemDataRole.ToolTipRole: # ToolTip
            meta = self._store.get(entry.hid_str)
            desc = meta.description if meta and meta.description else ''
            return f'{entry.hid_str}\n{desc}' if desc else entry.hid_str
        if role == Qt.ItemDataRole.ForegroundRole:
            if result.score < _RANK_TAG:
                return QColor('#888888')
        return 
    
    def set_query(self, query: str) -> None:
        q = query.lower().strip()
        if q == self._query:
            return
        self._query = q
        self._recompute_results()

    def _recompute_results(self) -> None:
        self.beginResetModel()
        if not self._query:
            self._results = []
            self.endResetModel()
        else:
            scored = [
                _QueryResult(entry=e, score=e.score(self._query))
                for e in self._index
                if not e.node.is_hidden
            ]
            self._results = sorted(
                (r for r in scored if r.score > 0),
                key=lambda r: -r.score,
            )
            self.endResetModel()

    def result_count(self) -> int:
        return len(self._results)
    
    def _on_insert_start(self, parent: VfsNode, first: int, last: int) -> None:
        index = self.index_for_node(parent)
        self.beginInsertRows(index, first, last)
        self._on_children_inserted(parent, first, last)

    def _on_insert_finished(self):
        self.endInsertRows()

    def index_for_node(self, target_node: VfsNode) -> QModelIndex:
        '''Get the QModelIndex for a node'''
        if target_node is None:
            return QModelIndex()
        return self.createIndex(target_node.row(), 0, target_node)

    
    def _on_children_inserted(self, parent: VfsNode, first: int, last: int) -> None:
        '''Index newly inserted children'''
        new_children = parent.children[first : last + 1]
        new_entries = [self._build_entry(c) for c in new_children if not c.is_hidden]
        if not new_entries:
            return
        self._index.extend(new_entries)
        if self._query:
            self._recompute_results()

    def _index_subtree(self, node: VfsNode) -> None:
        '''Recursively index all non-hidden nodes'''
        for child in node.children:
            if not child.is_hidden:
                self._index.append(self._build_entry(child))
                if child.children:
                    self._index_subtree(child)

    def _build_entry(self, node: VfsNode) -> _SearchEntry:
        meta       = self._store.get(node.hierarchical_id_str)
        name_lower: str = node.name.lower()
        hid_str: str    = node.hierarchical_id_str
        tags:   tuple[str, ...] = ()
        desc_lower = ''
        tags_lower = ()
        base_rank  = 0

        tokens: set[str] = set(name_lower.split())
        tokens.add(hid_str)
        tokens.update(c.lower() for c in node.category)

        if meta:
            if meta.title:
                tokens.update(meta.title.lower().split())
                base_rank += 20
            if meta.tags:
                tags = meta.tags
                tags_lower = tuple(t.lower() for t in meta.tags)
                tokens.update(tags_lower)
                base_rank += 10 * len(meta.tags)
            if meta.description:
                desc_lower = meta.description.lower()
                tokens.update(
                    w for w in desc_lower.split()
                    if len(w) > 2
                )
        return _SearchEntry(
            node=node,
            name_lower=name_lower,
            hid_str=hid_str,
            desc_lower=desc_lower,
            tags_lower=tags_lower,
            tokens=frozenset(tokens),
            base_rank=base_rank,
            tags=tags
        )