"""StagingPage contents including; hexdiff, stats, staging queue files."""

from __future__ import annotations

from core.node import ModTracker, VfsNode
from core.workers import IsoRebuildFlags
from ui.settings import Shortcut, Shortcuts

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from ui.hex_model import EDITABLE_COLUMNS, HexGridModelBase, HexTableView
from utilities import hline, human_size, vline

_COL_CHANGED_FG = QColor('#E2A96B')
_COL_CHANGED_BG = QColor('#2A2218')
_COL_ADDED_FG = QColor('#7EC8A0')
_COL_ADDED_BG = QColor('#345342')
_COL_REMOVED_FG = QColor('#E06C75')
_COL_REMOVED_BG = QColor('#2A1A1C')

###--------------------------------------- Hex Model ----------------------------------------------###


class HexDiffModel(HexGridModelBase):
    """Read-only hex model with diff-based byte coloring"""

    def __init__(self, data: bytes, diff_mask: list[str], parent=None) -> None:
        super().__init__(data, parent)
        self._mask = diff_mask

    def _cell_color(self, pos: int, col: int, role: int) -> QBrush | None:
        if col not in EDITABLE_COLUMNS or pos >= len(self._mask):
            return None
        kind = self._mask[pos]
        pair = {
            'changed': (_COL_CHANGED_FG, _COL_CHANGED_BG),
            'added': (_COL_ADDED_FG, _COL_ADDED_BG),
            'removed': (_COL_REMOVED_FG, _COL_REMOVED_BG),
        }.get(kind)
        if not pair:
            return None
        fg, bg = pair
        return QBrush(fg if role == Qt.ItemDataRole.ForegroundRole else bg)

    ###--------------------------------- Diff Mask -------------------------------------------------###

    @staticmethod
    def build_masks(new_data: bytes, orig_data: bytes) -> tuple[list[str], list[str]]:
        """Produce diffs per byte"""
        n, o = len(new_data), len(orig_data)
        common = min(n, o)
        new_mask = ['same'] * n
        orig_mask = ['same'] * o

        for i in range(common):
            if new_data[i] != orig_data[i]:
                new_mask[i] = 'changed'
                orig_mask[i] = 'changed'

        for i in range(common, n):
            new_mask[i] = 'added'

        for i in range(common, o):
            orig_mask[i] = 'removed'

        return new_mask, orig_mask


def _make_hex_view(model: HexDiffModel) -> QTableView:
    view = HexTableView()
    view.setModel(model)
    view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
    return view


class HexDiffPanel(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._setup_ui()
        self.clear()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 0, 0, 0)
        root.setSpacing(0)

        stats_bar = QWidget()
        stats_bar.setObjectName('SurfaceToolbar')
        stats_layout = QHBoxLayout(stats_bar)
        stats_layout.setContentsMargins(4, 4, 4, 4)
        stats_layout.setSpacing(12)

        self._node_label = QLabel('Select a modified file')
        self._node_label.setObjectName('TextHeader')
        self._stats_label = QLabel('')

        legend = QHBoxLayout()
        legend.setSpacing(4)
        for colour, text in (
            (_COL_CHANGED_FG, 'Changed'),
            (_COL_ADDED_FG, 'Added'),
            (_COL_REMOVED_FG, 'Removed'),
        ):
            dot = QLabel('■')
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
        stats_layout.setContentsMargins(6, 6, 6, 6)
        stats_layout.setVerticalSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)
        root.addWidget(stats_bar)

        # Hex view headers
        headers = QWidget()
        header_layout = QHBoxLayout(headers)
        header_layout.setContentsMargins(8, 8, 8, 8)
        self._new_header = QLabel('New (modified)')
        self._orig_header = QLabel('Original')
        self._new_header.setObjectName('TextHeader')
        self._orig_header.setObjectName('TextHeader')
        header_layout.addWidget(self._new_header, stretch=1)
        header_layout.addWidget(vline())
        header_layout.addWidget(self._orig_header, stretch=1)
        header_layout.setContentsMargins(6, 6, 6, 6)
        header_layout.setVerticalSizeConstraint(QHBoxLayout.SizeConstraint.SetFixedSize)
        root.addWidget(headers)
        root.addWidget(hline())

        ### Hex diff view
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(2)

        self._new_model = HexDiffModel(b'', [])
        self._orig_model = HexDiffModel(b'', [])
        self._new_view = _make_hex_view(self._new_model)
        self._orig_view = _make_hex_view(self._orig_model)

        splitter.addWidget(self._new_view)
        splitter.addWidget(self._orig_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter)

        self._new_view.verticalScrollBar().valueChanged.connect(self._orig_view.verticalScrollBar().setValue)
        self._orig_view.verticalScrollBar().valueChanged.connect(self._new_view.verticalScrollBar().setValue)

    ###-------------------------------- Public -------------------------------------###

    def load_diff(self, node_name: str, new_data: bytes, orig_data: bytes) -> None:
        new_mask, orig_mask = HexDiffModel.build_masks(new_data, orig_data)

        changed = sum(1 for m in new_mask if m == 'changed')
        added = sum(1 for m in new_mask if m == 'added')
        removed = sum(1 for m in orig_mask if m == 'removed')

        self._new_model = HexDiffModel(new_data, new_mask)
        self._orig_model = HexDiffModel(orig_data, orig_mask)
        self._new_view.setModel(self._new_model)
        self._orig_view.setModel(self._orig_model)

        self._node_label.setText(node_name)
        self._new_header.setText(f'New ({human_size(len(new_data))})')
        self._orig_header.setText(f'Original ({human_size(len(orig_data))})')

        parts = []
        if changed:
            parts.append(f'{changed} byte(s) changed')
        if added:
            parts.append(f'{added} byte(s) added')
        if removed:
            parts.append(f'{removed} byte(s) removed')
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
    """UI for managing the filesystem vs Staging Area"""

    request_file_browser = pyqtSignal()

    def __init__(self, dispatcher, rebuild_coordinator, parent=None) -> None:
        super().__init__(parent)
        self.dispatcher = dispatcher
        self.rebuild_coordinator = rebuild_coordinator
        self.tracker: ModTracker = self.dispatcher.tracker
        self._selected_node: VfsNode | None = None
        self._build_flags = IsoRebuildFlags.NONE
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
        top_layout.setContentsMargins(6, 4, 4, 0)
        top_layout.setSpacing(6)

        lists_row = QHBoxLayout()
        lists_row.setSpacing(6)

        unstage_col = QVBoxLayout()
        unstage_lbl = QLabel('Unstaged Changes')
        unstage_lbl.setObjectName('TextHeader')
        self.unstaged_list = QListWidget()
        self.unstaged_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        unstage_col.addWidget(unstage_lbl)
        unstage_col.addWidget(self.unstaged_list)

        btn_col = QVBoxLayout()
        btn_col.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btn_col.setSpacing(6)

        self.btn_stage = QPushButton('Stage >')
        self.btn_stage_all = QPushButton('Stage All >>')
        self.btn_unstage = QPushButton('< Unstage')
        self.btn_unstage_all = QPushButton('<< Unstage All')
        self.btn_revert = QPushButton('Revert')
        self.btn_revert_all = QPushButton('Revert All')
        for btn in (
            self.btn_stage,
            self.btn_stage_all,
            self.btn_unstage,
            self.btn_unstage_all,
            self.btn_revert,
            self.btn_revert_all,
        ):
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
        staged_lbl.setObjectName('TextHeader')
        self.staged_list = QListWidget()
        self.staged_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        staged_col.addWidget(staged_lbl)
        staged_col.addWidget(self.staged_list)

        lists_row.addLayout(unstage_col, stretch=1)
        lists_row.addLayout(btn_col)
        lists_row.addLayout(staged_col, stretch=1)
        top_layout.addLayout(lists_row)

        action_bar = QHBoxLayout()
        action_bar.setContentsMargins(6, 6, 6, 6)
        self.btn_back = QPushButton('< Back')
        self.btn_back.setToolTip(Shortcuts.text(Shortcut.BACK))
        self.slim_toggle = QCheckBox('Slimmed Rebuild')
        self.slim_toggle.setToolTip(
            'Removes all non-essential disk data.\nMeant for digital use only.'
        )
        self.btn_confirm = QPushButton('Build New ISO')
        self.btn_confirm.setObjectName('BtnImportant')
        self.btn_confirm.setEnabled(False)
        action_bar.addWidget(self.btn_back)
        action_bar.addStretch()
        action_bar.addWidget(self.slim_toggle)
        action_bar.addSpacing(24)
        action_bar.addWidget(self.btn_confirm)
        top_layout.addLayout(action_bar)

        self.diff_panel = HexDiffPanel()

        v_split.addWidget(top)
        v_split.addWidget(self.diff_panel)
        v_split.setStretchFactor(0, 1)
        v_split.setStretchFactor(1, 4)
        root.addWidget(v_split)

    def _connect_signals(self) -> None:
        self.btn_back.clicked.connect(self.request_file_browser.emit)
        self.btn_stage.clicked.connect(self._on_stage)
        self.btn_stage_all.clicked.connect(self._on_stage_all)
        self.btn_unstage.clicked.connect(self._on_unstage)
        self.btn_unstage_all.clicked.connect(self._on_unstage_all)
        self.btn_revert.clicked.connect(self._on_revert)
        self.btn_revert_all.clicked.connect(self._on_revert_all)

        self.tracker.state_changed.connect(self.refresh_lists)
        self.btn_confirm.clicked.connect(self._on_confirm)
        self.slim_toggle.stateChanged.connect(self._on_slim_toggled)

        self.unstaged_list.currentItemChanged.connect(self._on_item_changed)
        self.staged_list.currentItemChanged.connect(self._on_item_changed)

    def _on_confirm(self) -> None:
        if not self.tracker.rebuild_queue:
            return
        self.rebuild_coordinator.request_rebuild(list(self.tracker.rebuild_queue), self._build_flags)

    def _on_slim_toggled(self) -> None:
        self.slimmed_requested = self.slim_toggle.isChecked()

    def _setup_shortcuts(self) -> None:
        QShortcut(Shortcuts.sequence(Shortcut.BACK), self).activated.connect(self.request_file_browser.emit)

    def refresh_lists(self) -> None:
        """Modifies the list of modified nodes"""
        selected_hid = self._selected_node.hierarchical_id_str if self._selected_node else None
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
        self.diff_panel.load_diff(f'{node.name}  ({node.hierarchical_id_str})', new_data, orig_data)

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
        items = self.unstaged_list.selectedItems() + self.staged_list.selectedItems()
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
