'''StagingPage has it's own file?! Yes, ui_core was getting to big for my liking.'''
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal, QAbstractTableModel, QModelIndex
from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget, QListWidgetItem, 
                            QSplitter, QTableView, QHeaderView, QFrame)
from PyQt6.QtGui import QFont, QBrush, QColor, QShortcut, QKeySequence
from core.node import VfsNode, ModTracker
from utilities import human_size

import logging
logger = logging.getLogger(f'radiata.{__name__}')

_COL_CHANGED_FG = QColor('#E2A96B')
_COL_CHANGED_BG = QColor('#2A2218')
_COL_ADDED_FG   = QColor('#7EC8A0')
_COL_ADDED_BG   = QColor("#345342")
_COL_REMOVED_FG = QColor('#E06C75')
_COL_REMOVED_BG = QColor('#2A1A1C')

class HexDiffModel(QAbstractTableModel):
    '''Read-only hex model'''
    _COLS = 18

    def __init__(self, data: bytes, diff_mask: list[str], parent=None) -> None:
        super().__init__(parent)
        self._data = data
        self._mask = diff_mask

    ###---------------------------------------------- QT API -----------------------------------------###

    def rowCount(self, parent=QModelIndex()) -> int:
        return max(1, (len(self._data) + 15) // 16)
    
    def columnCount(self, parent=QModelIndex()) -> int:
        return self._COLS
    
    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    
    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        pos = row * 16 + (col - 1)
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return f'{row * 16:08X}'
            if 1 <= col <= 16:
                return f'{self._data[pos]:02X}' if pos < len(self._data) else ''
            if col == 17:
                chunk = self._data[row * 16: row * 16 + 16]
                return ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)
        if role in (Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole):
            if 1 <= col <= 16 and pos < len(self._mask):
                kind = self._mask[pos]
                if kind == 'changed':
                    return QBrush(
                        _COL_CHANGED_FG if role == Qt.ItemDataRole.ForegroundRole
                        else _COL_CHANGED_BG
                    )
                if kind == 'added':
                    return QBrush(
                        _COL_ADDED_FG if role == Qt.ItemDataRole.ForegroundRole
                        else _COL_ADDED_BG
                    )
                if kind == 'removed':
                    return QBrush(
                        _COL_REMOVED_FG if role == Qt.ItemDataRole.ForegroundRole
                        else _COL_REMOVED_BG
                    )
        return None
    
    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        return None
    
    ###--------------------------------- Diff Mask -------------------------------------------------###

    @staticmethod
    def build_masks(new_data: bytes, orig_data: bytes) -> tuple[list[str], list[str]]:
        '''Produce diffs per byte'''
        n, o      = len(new_data), len(orig_data)
        common    = min(n, o)
        new_mask  = ['same'] * n
        orig_mask = ['same'] * o

        for i in range(common):
            if new_data[i] != orig_data[i]:
                new_mask[i]  = 'changed'
                orig_mask[i] = 'changed'
        
        for i in range(common, n):
            new_mask[i] = 'added'

        for i in range(common, o):
            orig_mask[i] = 'removed'

        return new_mask, orig_mask
    
def _make_hex_view(model: HexDiffModel) -> QTableView:
    view = QTableView()
    view.setModel(model)
    view.setFont(QFont('Courier New', 10))
    view.horizontalHeader().setVisible(False)
    view.verticalHeader().setVisible(False)
    view.setShowGrid(False)
    view.setSelectionMode(QTableView.SelectionMode.ContiguousSelection)
    view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)

    h = view.horizontalHeader()
    h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
    view.setColumnWidth(0, 85)
    for col in range(1, 17):
        h.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
        view.setColumnWidth(col, 26)
    h.setSectionResizeMode(17, QHeaderView.ResizeMode.Stretch)
    return view

class HexDiffPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()
        self.clear()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        stats_bar = QWidget()
        stats_bar.setObjectName('EditorToolbar')
        stats_layout = QHBoxLayout(stats_bar)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(12)

        self._node_label = QLabel('Select a modified file')
        self._node_label.setObjectName('SecitonHeader')
        self._stats_label = QLabel('')

        legend = QHBoxLayout()
        legend.setSpacing(4)
        for colour, text in (
            (_COL_CHANGED_FG, 'Changed'),
            (_COL_ADDED_FG,   'Added'),
            (_COL_REMOVED_FG, 'Removed')
        ):
            dot = QLabel('||')
            dot.setStyleSheet(f'color: {colour.name()}')
            lbl = QLabel(text)
            legend.addWidget(dot)
            legend.addWidget(lbl)
            legend.addSpacing(6)

        stats_layout.addWidget(self._node_label)
        stats_layout.addStretch()
        stats_layout.addWidget(self._stats_label)
        stats_layout.addStretch(12)
        stats_layout.addLayout(legend)
        stats_layout.setContentsMargins(0 ,0, 6, 6)
        stats_layout.setVerticalSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)
        root.addWidget(stats_bar)

        # Hex view headers
        headers = QWidget()
        header_layout = QHBoxLayout(headers)
        header_layout.setContentsMargins(4, 2, 4, 2)
        self._new_header = QLabel('New (modified)')
        self._orig_header = QLabel('Original')
        self._new_header.setObjectName('SectionHeader')
        self._orig_header.setObjectName('SectionHeader')
        header_layout.addWidget(self._new_header, stretch=1)
        header_layout.addWidget(_vline())
        header_layout.addWidget(self._orig_header, stretch=1)
        header_layout.setContentsMargins(0, 0, 6, 6)
        header_layout.setVerticalSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)
        root.addWidget(headers)
        root.addWidget(_hline())

        ### Hex diff view
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        self._new_model  = HexDiffModel(b'', [])
        self._orig_model = HexDiffModel(b'', [])
        self._new_view   = _make_hex_view(self._new_model)
        self._orig_view  = _make_hex_view(self._orig_model)

        splitter.addWidget(self._new_view)
        splitter.addWidget(self._orig_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter)

        self._new_view.verticalScrollBar().valueChanged.connect(
            self._orig_view.verticalScrollBar().setValue
        )
        self._orig_view.verticalScrollBar().valueChanged.connect(
            self._new_view.verticalScrollBar().setValue
        )

    ###-------------------------------- Public -------------------------------------###

    def load_diff(self, node_name: str, new_data: bytes, orig_data: bytes) -> None:
        new_mask, orig_mask = HexDiffModel.build_masks(new_data, orig_data)

        changed = sum(1 for m in new_mask  if m == 'changed')
        added   = sum(1 for m in new_mask  if m == 'added')
        removed = sum(1 for m in orig_data if m == 'removed')

        self._new_model = HexDiffModel(new_data, new_mask)
        self._orig_model = HexDiffModel(orig_data, orig_mask)
        self._new_view.setModel(self._new_model)
        self._orig_view.setModel(self._orig_model)

        self._node_label.setText(node_name)
        self._new_header.setText(f'New ({human_size(len(new_data))})')
        self._orig_header.setText(f'Original ({human_size(len(orig_data))})')

        parts = []
        if changed: parts.append(f'{changed} byte(s) changed')
        if added:   parts.append(f'{added} byte(s) added')
        if removed: parts.append(f'{removed} byte(s) removed')
        self._stats_label.setText(', '.join(parts) if parts else 'No differences')

    def clear(self) -> None:
        self._new_model = HexDiffModel(b'', [])
        self._orig_model = HexDiffModel(b'', [])
        self._new_view.setModel(self._new_model)
        self._orig_view.setModel(self._orig_model)
        self._node_label.setText('Select a modified file to view diff')
        self._new_header.setText('New')
        self._orig_header.setText('Original')
        self._stats_label.setText('')


###------------------------------------- Staging Page --------------------------------------###

class StagingPage(QWidget):
    '''UI for managing the filesystem vs Staging Area'''
    request_workspace = pyqtSignal()

    def __init__(self, dispatcher, parent=None) -> None:
        super().__init__(parent)
        self.dispatcher = dispatcher
        self.tracker: ModTracker = self.dispatcher.tracker
        self._selected_node: VfsNode | None = None
        self._setup_ui()
        self._connect_signals()
        self._setup_shortcuts()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        v_split = QSplitter(Qt.Orientation.Vertical)
        v_split.setHandleWidth(4)

        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)

        lists_row = QHBoxLayout()
        lists_row.setSpacing(6)

        unstage_col = QVBoxLayout()
        unstage_lbl = QLabel('Unstaged Changes')
        unstage_lbl.setObjectName('SectionHeader')
        self.unstaged_list = QListWidget()
        self.unstaged_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        unstage_col.addWidget(unstage_lbl)
        unstage_col.addWidget(self.unstaged_list)

        btn_col = QVBoxLayout()
        btn_col.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_col.setSpacing(6)
        
        self.btn_stage       = QPushButton('Stage >')
        self.btn_stage_all   = QPushButton('Stage All >>')
        self.btn_unstage     = QPushButton('< Unstage')
        self.btn_unstage_all = QPushButton('<< Unstage All')
        self.btn_revert      = QPushButton('Revert')
        self.btn_revert_all  = QPushButton('Revert All')
        for btn in (self.btn_stage, self.btn_stage_all, self.btn_unstage, 
                    self.btn_unstage_all, self.btn_revert, self.btn_revert_all):
            btn.setFixedWidth(140)
        btn_col.addWidget(self.btn_stage)
        btn_col.addWidget(self.btn_stage_all)
        btn_col.addSpacing(12)
        btn_col.addWidget(self.btn_unstage)
        btn_col.addWidget(self.btn_unstage_all)
        btn_col.addStretch()
        btn_col.addWidget(self.btn_revert)
        btn_col.addWidget(self.btn_revert_all)

        staged_col = QVBoxLayout()
        staged_lbl = QLabel('Staged (ready to build)')
        staged_lbl.setObjectName('SectionHeader')
        self.staged_list = QListWidget()
        self.staged_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        staged_col.addWidget(staged_lbl)
        staged_col.addWidget(self.staged_list)

        lists_row.addLayout(unstage_col, stretch=1)
        lists_row.addLayout(btn_col)
        lists_row.addLayout(staged_col, stretch=1)
        top_layout.addLayout(lists_row)

        action_bar = QHBoxLayout()
        self.btn_back = QPushButton('< Back')
        self.btn_back.setObjectName('FloatClearButton')
        self.btn_confirm = QPushButton('Build New ISO')
        self.btn_confirm.setEnabled(False)
        action_bar.addWidget(self.btn_back)
        action_bar.addStretch()
        action_bar.addWidget(self.btn_confirm)
        top_layout.addLayout(action_bar)

        self.diff_panel = HexDiffPanel()

        v_split.addWidget(top)
        v_split.addWidget(self.diff_panel)
        v_split.setStretchFactor(0, 1)
        v_split.setStretchFactor(1, 4)
        root.addWidget(v_split)

    def _connect_signals(self) -> None:
        self.btn_back.clicked.connect(self.request_workspace.emit)
        self.btn_stage.clicked.connect(self._on_stage)
        self.btn_stage_all.clicked.connect(self._on_stage_all)
        self.btn_unstage.clicked.connect(self._on_unstage)
        self.btn_unstage_all.clicked.connect(self._on_unstage_all)
        self.btn_revert.clicked.connect(self._on_revert)
        self.btn_revert_all.clicked.connect(self._on_revert_all)

        self.tracker.state_changed.connect(self.refresh_lists)
        self.btn_confirm.clicked.connect(self.tracker.confirm_and_rebuild)

        self.unstaged_list.currentItemChanged.connect(self._on_item_changed)
        self.staged_list.currentItemChanged.connect(self._on_item_changed)

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence('Esc'), self).activated.connect(self.request_workspace.emit)
        
    def refresh_lists(self) -> None:
        '''Modifies the list of modified nodes'''
        selected_hid = (
            self._selected_node.hierarchical_id_str
            if self._selected_node else None
        )
        self.unstaged_list.clear()
        self.staged_list.clear()
        for node in sorted(self.tracker.modified_nodes, key=lambda n: n.name):
            item = _make_item(node)
            self.unstaged_list.addItem(item)
            if selected_hid and node.hierarchical_id_str == selected_hid:
                self.unstaged_list.setCurrentItem(item)
        for node in sorted(self.tracker.rebuild_queue, key=lambda n: n.name):
            item = _make_item(node)
            self.staged_list.addItem(item)
            if selected_hid and node.hierarchical_id_str == selected_hid:
                self.staged_list.setCurrentItem(item)
        self.btn_confirm.setEnabled(len(self.tracker.rebuild_queue) > 0)

    def _on_item_changed(self, current: QListWidgetItem, _prev) -> None:
        if current is None:
            self._selected_node = None
            self.diff_panel.clear()
            return
        node: VfsNode | None = current.data(Qt.ItemDataRole.UserRole)
        if node is None:
            self._selected_node = None
            self.diff_panel.clear()
            return
        self._selected_node = node
        new_data = node.pending_data or b''
        orig_data = self.tracker.get_original(node)
        self.diff_panel.load_diff(
            f'{node.name}  ({node.hierarchical_id_str})',
            new_data,
            orig_data
        )

    def _on_stage(self) -> None:
        for item in self.unstaged_list.selectedItems():
            self.tracker.stage_node(item.data(Qt.ItemDataRole.UserRole))

    def _on_stage_all(self) -> None:
        for node in list(self.tracker.modified_nodes):
            self.tracker.stage_node(node)

    def _on_unstage(self) -> None:
        for item in self.staged_list.selectedItems():
            self.tracker.unstage_node(item.data(Qt.ItemDataRole.UserRole))

    def _on_unstage_all(self) -> None:
        for node in list(self.tracker.rebuild_queue):
            self.tracker.unstage_node(node)

    def _on_revert(self) -> None:
        items = (
            self.unstaged_list.selectedItems()
            + self.staged_list.selectedItems()
        )
        for item in items:
            node = item.data(Qt.ItemDataRole.UserRole)
            self.tracker.revert_node(node)
            if node is self._selected_node:
                self._selected_node = None
                self.diff_panel.clear()

    def _on_revert_all(self) -> None:
        all_nodes = list(self.tracker.modified_nodes) + list(self.tracker.rebuild_queue)
        for node in all_nodes:
            self.tracker.revert_node(node)
            
        self._selected_node = None
        self.diff_panel.clear()

def _make_item(node: VfsNode) -> QListWidgetItem:
    item = QListWidgetItem(node.name)
    item.setData(Qt.ItemDataRole.UserRole, node)
    item.setToolTip(f'{node.hierarchical_id_str}\n{human_size(node.size)}')
    return item

def _hline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f

def _vline() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    f.setFixedWidth(2)
    return f
