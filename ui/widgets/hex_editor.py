from __future__ import annotations

import struct
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox,
    QTableView, QHeaderView, QWidget, QMenu, QApplication,
    QLineEdit, QFrame,
)
from PyQt6.QtCore import pyqtSignal, Qt, QAbstractTableModel, QModelIndex, QItemSelection
from PyQt6.QtGui import QFont, QShortcut, QKeySequence, QColor, QBrush, QAction

from core.contracts import BaseEditor
from core.node import VfsNode
from core.registry import Registry

import logging
logger = logging.getLogger(f'radiata.{__name__}')


@Registry.register(name='Hex Editor', is_fallback=True)
class HexEditorWidget(BaseEditor):

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.current_node = None
        self.model: HexTableModel | None = None
        self._setup_ui()
        self._setup_shortcuts()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_toolbar())
        layout.addWidget(self._build_search_bar())

        self.table_view = QTableView()
        self.table_view.setObjectName('HexView')
        self.table_view.setFont(QFont('Courier New', 10))
        self.table_view.horizontalHeader().setVisible(False)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.setShowGrid(False)
        self.table_view.setSelectionMode(QTableView.SelectionMode.ContiguousSelection)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)
        self.table_view.setTabKeyNavigation(True)
        self.table_view.setWordWrap(False)
        layout.addWidget(self.table_view)

        layout.addWidget(self._build_inspector())

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('EditorToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 5, 10, 5)

        self.info_label = QLabel('Hex View')
        self.btn_apply  = QPushButton('Apply Changes')
        self.btn_apply.setFixedWidth(120)
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._handle_apply)

        lay.addWidget(self.info_label)
        lay.addStretch()
        lay.addWidget(self.btn_apply)
        return bar

    def _build_search_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('EditorToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 3, 10, 3)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('Search hex bytes (e.g.  4A 2F  or  4a2f)...')
        self.search_input.setFixedHeight(24)
        self.search_input.returnPressed.connect(self._search_next)

        self.search_status = QLabel('')
        self.search_status.setFixedWidth(140)

        btn_prev = QPushButton('◀')
        btn_next = QPushButton('▶')
        btn_prev.setFixedSize(24, 24)
        btn_next.setFixedSize(24, 24)
        btn_prev.clicked.connect(self._search_prev)
        btn_next.clicked.connect(self._search_next)

        lay.addWidget(QLabel('Find:'))
        lay.addWidget(self.search_input)
        lay.addWidget(btn_prev)
        lay.addWidget(btn_next)
        lay.addWidget(self.search_status)
        return bar

    def _build_inspector(self) -> QWidget:
        '''Status bar showing byte interpretations at the current cursor position.'''
        frame = QFrame()
        frame.setObjectName('EditorToolbar')
        frame.setFixedHeight(28)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 2, 10, 2)
        lay.setSpacing(16)

        self._insp_offset  = QLabel('Offset: —')
        self._insp_u8      = QLabel('u8: —')
        self._insp_i8      = QLabel('i8: —')
        self._insp_u16_le  = QLabel('u16 LE: —')
        self._insp_u32_le  = QLabel('u32 LE: —')
        self._insp_sel     = QLabel('Sel: —')

        for lbl in (self._insp_offset, self._insp_u8, self._insp_i8,
                    self._insp_u16_le, self._insp_u32_le, self._insp_sel):
            lay.addWidget(lbl)

        lay.addStretch()
        return frame

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence('Ctrl+S'), self).activated.connect(self._handle_apply)
        QShortcut(QKeySequence('Ctrl+C'), self).activated.connect(lambda: self._copy('hex'))
        QShortcut(QKeySequence('Ctrl+F'), self).activated.connect(self.search_input.setFocus)

    # ------------------------------------------------------------------
    # BaseEditor implementation
    # ------------------------------------------------------------------

    def load_node(self, node, data: bytes) -> None:
        super().load_node(node, data)
        self.current_node = node
        self.btn_apply.setEnabled(True)
        self.info_label.setText(
            f'Editing: {node.name}  —  {_human_size(len(data))}'
        )
        self.model = HexTableModel(data)
        self.table_view.setModel(self.model)

        self.model.dataChanged.connect(lambda *_: self.set_dirty(True))

        # Connect selection changes to inspector
        sm = self.table_view.selectionModel()
        if sm:
            sm.currentChanged.connect(self._on_cursor_changed)
            sm.selectionChanged.connect(self._on_selection_changed)

        # Column widths
        h = self.table_view.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table_view.setColumnWidth(0, 85)        # offset
        for col in range(1, 17):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table_view.setColumnWidth(col, 26)  # hex byte

        h.setSectionResizeMode(17, QHeaderView.ResizeMode.Stretch)  # ASCII
        self.table_view.setColumnWidth(17, 140)

        v = self.table_view.verticalHeader()
        v.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        v.setDefaultSectionSize(22)

        self._reset_inspector()
        logger.debug(f'Loaded {node.name} ({len(data)} bytes) into Hex Editor.')

    def get_modified_data(self) -> bytes:
        return self.model.get_bytes() if self.model else b''

    # ------------------------------------------------------------------
    # Apply
    # ------------------------------------------------------------------

    def _handle_apply(self) -> None:
        if not self.current_node or not self.model:
            return
        new_bytes = self.model.get_bytes()
        self.apply_requested.emit(self.current_node, new_bytes)
        self.set_dirty(False)
        QMessageBox.information(self, 'Applied', f'Changes applied to {self.current_node.name}')

    # ------------------------------------------------------------------
    # Copy operations
    # ------------------------------------------------------------------

    def _selected_bytes(self) -> bytes:
        '''Return the bytes corresponding to the current hex-column selection.'''
        if not self.model:
            return b''
        sm      = self.table_view.selectionModel()
        indexes = sorted(
            (idx for idx in sm.selectedIndexes() if 1 <= idx.column() <= 16),
            key=lambda i: (i.row(), i.column()),
        )
        data = self.model.get_bytes()
        out  = bytearray()
        for idx in indexes:
            byte_pos = idx.row() * 16 + (idx.column() - 1)
            if byte_pos < len(data):
                out.append(data[byte_pos])
        return bytes(out)

    def _copy(self, fmt: str) -> None:
        raw = self._selected_bytes()
        if not raw:
            return

        if fmt == 'hex':
            text = ' '.join(f'{b:02X}' for b in raw)
        elif fmt == 'python':
            text = 'b"' + ''.join(f'\\x{b:02x}' for b in raw) + '"'
        elif fmt == 'c':
            text = ', '.join(f'0x{b:02X}' for b in raw)
        elif fmt == 'ascii':
            text = ''.join(chr(b) if 32 <= b <= 126 else '.' for b in raw)
        else:
            return

        QApplication.clipboard().setText(text)
        logger.debug(f'Copied {len(raw)} bytes as {fmt}')

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    def _show_context_menu(self, pos) -> None:
        if not self.model:
            return
        menu = QMenu(self)

        copy_menu = menu.addMenu('Copy')
        for label, fmt in (
            ('As hex bytes  (4A 2F …)',    'hex'),
            ('As Python literal  (b"\\x…")', 'python'),
            ('As C array  (0x4A, 0x2F …)', 'c'),
            ('As ASCII text',              'ascii'),
        ):
            act = QAction(label, self)
            act.triggered.connect(lambda checked=False, f=fmt: self._copy(f))
            copy_menu.addAction(act)

        menu.addSeparator()
        paste_act = QAction('Paste hex bytes', self)
        paste_act.triggered.connect(self._paste_hex)
        menu.addAction(paste_act)

        menu.addSeparator()
        fill_act = QAction('Fill selection with 00', self)
        fill_act.triggered.connect(self._fill_zero)
        menu.addAction(fill_act)

        menu.exec(self.table_view.viewport().mapToGlobal(pos))

    # ------------------------------------------------------------------
    # Paste & fill
    # ------------------------------------------------------------------

    def _paste_hex(self) -> None:
        if not self.model:
            return
        text = QApplication.clipboard().text()
        try:
            raw = bytes.fromhex(text.replace(' ', '').replace('\n', ''))
        except ValueError:
            QMessageBox.warning(self, 'Paste error', 'Clipboard does not contain valid hex bytes.')
            return

        sm = self.table_view.selectionModel()
        hex_cells = sorted(
            (idx for idx in sm.selectedIndexes() if 1 <= idx.column() <= 16),
            key=lambda i: (i.row(), i.column()),
        )
        if not hex_cells:
            return

        data = self.model.get_bytes_mutable()
        for i, idx in enumerate(hex_cells):
            if i >= len(raw):
                break
            byte_pos = idx.row() * 16 + (idx.column() - 1)
            if byte_pos < len(data):
                data[byte_pos] = raw[i]

        self.model.notify_all_changed()
        self.set_dirty(True)

    def _fill_zero(self) -> None:
        if not self.model:
            return
        sm = self.table_view.selectionModel()
        data = self.model.get_bytes_mutable()
        for idx in sm.selectedIndexes():
            if 1 <= idx.column() <= 16:
                pos = idx.row() * 16 + (idx.column() - 1)
                if pos < len(data):
                    data[pos] = 0
        self.model.notify_all_changed()
        self.set_dirty(True)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search_next(self, reverse: bool = False) -> None:
        if not self.model:
            return
        needle = self._parse_search_input()
        if not needle:
            self.search_status.setText('Invalid pattern')
            return

        data  = self.model.get_bytes()
        start = self._current_byte_offset() + (0 if reverse else 1)
        pos   = self._find_bytes(data, needle, start, reverse)

        if pos == -1:
            # Wrap
            pos = self._find_bytes(data, needle, 0 if not reverse else len(data) - 1, reverse)

        if pos == -1:
            self.search_status.setText('Not found')
            return

        self._select_byte_range(pos, len(needle))
        self.search_status.setText(f'@ {hex(pos)}')

    def _search_prev(self) -> None:
        self._search_next(reverse=True)

    def _parse_search_input(self) -> bytes | None:
        text = self.search_input.text().replace(' ', '')
        try:
            return bytes.fromhex(text) if text else None
        except ValueError:
            return None

    def _find_bytes(self, data: bytes, needle: bytes, start: int, reverse: bool) -> int:
        if not reverse:
            idx = data.find(needle, start)
        else:
            idx = data.rfind(needle, 0, start)
        return idx

    def _select_byte_range(self, byte_pos: int, length: int) -> None:
        if not self.model:
            return
        sm = self.table_view.selectionModel()
        sm.clearSelection()
        selection = QItemSelection()
        for i in range(length):
            pos = byte_pos + i
            row = pos // 16
            col = (pos % 16) + 1
            idx = self.model.index(row, col)
            selection.select(idx, idx)
        sm.select(selection, sm.SelectionFlag.Select)
        # Scroll to first match
        first_row = byte_pos // 16
        first_col = (byte_pos % 16) + 1
        self.table_view.scrollTo(self.model.index(first_row, first_col))

    # ------------------------------------------------------------------
    # Inspector
    # ------------------------------------------------------------------

    def _on_cursor_changed(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if not self.model or not current.isValid():
            return
        col = current.column()
        if col < 1 or col > 16:
            return
        pos  = current.row() * 16 + (col - 1)
        data = self.model.get_bytes()
        self._update_inspector(pos, data)

    def _on_selection_changed(self, *_) -> None:
        if not self.model:
            return
        raw = self._selected_bytes()
        if raw:
            self._insp_sel.setText(f'Sel: {len(raw)} byte{"s" if len(raw) != 1 else ""}')
        else:
            self._insp_sel.setText('Sel: —')

    def _update_inspector(self, pos: int, data: bytes) -> None:
        self._insp_offset.setText(f'Offset: {hex(pos)}')

        if pos < len(data):
            b = data[pos]
            self._insp_u8.setText(f'u8: {b}')
            self._insp_i8.setText(f'i8: {struct.unpack_from("b", data, pos)[0]}')
        else:
            self._insp_u8.setText('u8: —')
            self._insp_i8.setText('i8: —')

        def safe_unpack(fmt: str, size: int) -> str:
            if pos + size <= len(data):
                val = struct.unpack_from(fmt, data, pos)[0]
                return str(val)
            return '—'

        self._insp_u16_le.setText(f'u16 LE: {safe_unpack("<H", 2)}')
        self._insp_u32_le.setText(f'u32 LE: {safe_unpack("<I", 4)}')

    def _reset_inspector(self) -> None:
        for lbl in (self._insp_offset, self._insp_u8, self._insp_i8, self._insp_sel):
            text = lbl.text().split(':')[0]
            lbl.setText(f'{text}: —')

    def _current_byte_offset(self) -> int:
        idx = self.table_view.currentIndex()
        if idx.isValid() and 1 <= idx.column() <= 16:
            return idx.row() * 16 + (idx.column() - 1)
        return 0


###---------------------------------------- Model -------------------------------------###

class HexTableModel(QAbstractTableModel):
    '''
    18 columns:
      col 0       — offset label (read-only)
      cols 1-16   — hex byte cells (editable)
      col 17      — ASCII dump (read-only)
    '''
    _COLUMNS = 18

    # Colours for modified bytes
    _MODIFIED_FG = QColor('#E2A96B')
    _MODIFIED_BG = QColor('#2A2218')

    def __init__(self, data: bytes, parent=None) -> None:
        super().__init__(parent)
        self._data:     bytearray       = bytearray(data)
        self._original: bytes           = bytes(data)
        self._modified: set[int]        = set()

    # ------------------------------------------------------------------
    # Qt API
    # ------------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return (len(self._data) + 15) // 16

    def columnCount(self, parent=QModelIndex()) -> int:
        return self._COLUMNS

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if 1 <= index.column() <= 16:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row, col = index.row(), index.column()
        pos      = row * 16 + (col - 1)

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == 0:
                return f'{row * 16:08X}'
            if 1 <= col <= 16:
                return f'{self._data[pos]:02X}' if pos < len(self._data) else ''
            if col == 17:
                chunk = self._data[row * 16: (row + 1) * 16]
                return ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)

        if role == Qt.ItemDataRole.ForegroundRole and 1 <= col <= 16:
            if pos in self._modified:
                return QBrush(self._MODIFIED_FG)

        if role == Qt.ItemDataRole.BackgroundRole and 1 <= col <= 16:
            if pos in self._modified:
                return QBrush(self._MODIFIED_BG)

        return None

    def setData(self, index: QModelIndex, value: str, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not (1 <= index.column() <= 16):
            return False

        pos = index.row() * 16 + (index.column() - 1)
        if pos >= len(self._data):
            return False

        clean = value.strip()
        if len(clean) > 2:
            return False
        try:
            new_byte = int(clean, 16)
        except ValueError:
            return False

        self._data[pos] = new_byte

        if new_byte != self._original[pos]:
            self._modified.add(pos)
        else:
            self._modified.discard(pos)

        self.dataChanged.emit(index, index)
        ascii_idx = self.index(index.row(), 17)
        self.dataChanged.emit(ascii_idx, ascii_idx)
        return True

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        return None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def get_bytes(self) -> bytes:
        return bytes(self._data)

    def get_bytes_mutable(self) -> bytearray:
        '''Return the internal bytearray directly — callers must call notify_all_changed after.'''
        return self._data

    def notify_all_changed(self) -> None:
        '''Emit dataChanged for the entire model after bulk mutations.'''
        top_left     = self.index(0, 0)
        bottom_right = self.index(self.rowCount() - 1, self._COLUMNS - 1)
        self.dataChanged.emit(top_left, bottom_right)
        # Recompute modified set
        self._modified = {
            i for i, (a, b) in enumerate(zip(self._data, self._original)) if a != b
        }


###---------------------------------------- Utility -----------------------------------###

def _human_size(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024
    return f'{n:.1f} TB'