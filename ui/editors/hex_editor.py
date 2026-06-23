'''BaseEditor Global editor for any format. Takes any raw byte stream as input. Shitty hex editor :"v'''
from __future__ import annotations

import struct
from typing import Any
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox,
    QTableView, QHeaderView, QWidget, QMenu, QApplication, QLineEdit, QFrame,
)
from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex, QItemSelection, pyqtSignal
from PyQt6.QtGui import QShortcut, QKeySequence, QColor, QBrush, QAction, QUndoCommand, QUndoStack

from core.contracts import BaseEditor
from core.handlers.generic_binary_leaf import GenericBinaryHandler
from core.registry import Registry
from core.node import VfsNode
from utilities import human_size

import logging
logger = logging.getLogger(f'radiata.{__name__}')


class HistoryManager(QUndoCommand):
    '''Stores and handles undo/redo for the hex editor'''
    def __init__(self, model: HexTableModel, changes: dict[int, int], description: str) -> None:
        super().__init__(description)
        self.model     = model
        self.new_bytes = changes
        self.old_bytes = {pos: model._data[pos] for pos in changes}

    def redo(self):
        self.model.apply_changes_dict(self.new_bytes)

    def undo(self):
        self.model.apply_changes_dict(self.old_bytes)

@Registry.register_editor(name='Hex Editor', extensions=(), handler=GenericBinaryHandler, is_fallback=True)
class HexEditorWidget(BaseEditor):
    '''Mutable global fallback editor'''
    undo_state_changed = pyqtSignal(bool, bool)  # (can_undo, can_redo)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.model: HexTableModel | None = None

        self.undo_stack = QUndoStack(self)
        self.undo_stack.canUndoChanged.connect(self._on_history_changed)
        self.undo_stack.canRedoChanged.connect(self._on_history_changed)
        self.undo_stack.cleanChanged.connect(lambda clean: self.set_dirty(not clean))

        self._setup_ui()
        self._setup_shortcuts()

    def _on_history_changed(self) -> None:
        self.undo_state_changed.emit(self.undo_stack.canUndo(), self.undo_stack.canRedo())

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header_bar())

        self.table_view = QTableView()
        self.table_view.setObjectName('HexView')
        self.table_view.horizontalHeader().setVisible(False)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.setShowGrid(False)
        self.table_view.setSelectionMode(QTableView.SelectionMode.ContiguousSelection)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)
        self.table_view.setTabKeyNavigation(True)
        layout.addWidget(self.table_view)

        layout.addWidget(self._build_inspector())

    def _build_header_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('EditorToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 5, 10, 5)

        self.info_label = QLabel('Hex View')
        self.info_label.setObjectName('SectionHeader') # Give it visual weight

        # Restrict search bar width so it doesn't stretch awkwardly across the screen
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('Search bytes (e.g. 4A 2F)...')
        self.search_input.setFixedWidth(250)
        self.search_input.setFixedHeight(24)
        self.search_input.returnPressed.connect(self._search_next)

        self.search_status = QLabel('')
        self.search_status.setFixedWidth(100)
        self.search_status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        btn_prev = QPushButton('◀')
        btn_next = QPushButton('▶')
        btn_prev.setFixedSize(24, 24)
        btn_next.setFixedSize(24, 24)
        btn_prev.clicked.connect(self._search_prev)
        btn_next.clicked.connect(self._search_next)

        lay.addWidget(self.info_label)
        lay.addStretch()
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
        QShortcut(QKeySequence('Ctrl+C'), self).activated.connect(lambda: self._copy('hex'))
        QShortcut(QKeySequence('Ctrl+F'), self).activated.connect(self.search_input.setFocus)

    def show_load_error(self, message: str) -> None:
        self.info_label.setText(f'Load failed: {message}')
        logger.error(f'HexEditor: {message}')

    ###---------------------------- Contractuals ---------------------------------###

    def begin_loading(self, node: VfsNode) -> None:
        '''Shows placeholder while the worker thread fetches data'''
        super().begin_loading(node)
        self.info_label.setText(f'Loading {node.name}...')
        if self.model:
            self.table_view.setModel(None)
            self.model = None
        self._reset_inspector()

    def _populate_ui(self, data: Any) -> None:
        '''
        Build the hex model from raw bytes.
        Called by BaseEditor.receive_data (bytes path) and by discard_changes.
        Clearing the undo stack here marks it clean
        '''
        if not isinstance(data, bytes):
            self.info_label.setText('Cannot display: expected bytes')
            return

        self.undo_stack.clear()
        self.model = HexTableModel(data, self.undo_stack)
        self.table_view.setModel(self.model)

        selection_model = self.table_view.selectionModel()
        if selection_model:
            selection_model.currentChanged.connect(self._on_cursor_changed)
            selection_model.selectionChanged.connect(self._on_selection_changed)
        
        # Column Widths
        header = self.table_view.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table_view.setColumnWidth(0, 85)
        for col in range(1, 17):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.table_view.setColumnWidth(col, 26)
        header.setSectionResizeMode(17, QHeaderView.ResizeMode.Stretch)

        size_str = human_size(len(data))
        self.info_label.setText(
            f'Editing: {self.current_node.name} {size_str}' if self.current_node
            else f'Hex View {size_str}'
        )
        logger.debug(f'HexEditor: populated {len(data)} bytes.')

    def show_error(self, message: str) -> None:
        '''Show error inline in the header bar.'''
        self.info_label.setText(f'Load failed: {message}')
        super().show_error(message)   # also logs

    def current_data(self) -> Any:
        '''Return current bytes including any in-progress edits.'''
        return self.model.get_bytes() if self.model else self._original_payload

    def confirm_changes_applied(self) -> None:
        self.undo_stack.setClean()
        super().confirm_changes_applied()

    def discard_changes(self) -> None:
        '''Revert the editor to the last saved state.'''
        if not self.is_dirty() or not self.current_node:
            return
        self._pending_data = None
        # _populate_ui clears the undo stack → cleanChanged(True) → set_dirty(False)
        self._populate_ui(self._original_payload)

    def undo(self) -> None:
        self.undo_stack.undo()

    def redo(self) -> None:
        self.undo_stack.redo()

    def cleanup(self) -> None:
        self.table_view.setModel(None)
        self.model = None
        super().cleanup()

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------

    def _selected_bytes(self) -> bytes:
        '''Return the bytes corresponding to the current hex-column selection.'''
        if not self.model:
            return b''
        selection_model = self.table_view.selectionModel()
        indexes = sorted(
            (idx for idx in selection_model.selectedIndexes() if 1 <= idx.column() <= 16),
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

    def _paste_hex(self) -> None:
        if not self.model:
            return
        text = QApplication.clipboard().text()
        try:
            raw = bytes.fromhex(text.replace(' ', '').replace('\n', ''))
        except ValueError:
            QMessageBox.warning(self, 'Paste error', 'Clipboard is not valid hex bytes.')
            return
        selection_model = self.table_view.selectionModel()
        cells = sorted(
            (idx for idx in selection_model.selectedIndexes() if 1 <= idx.column() <= 16),
            key=lambda i: (i.row(), i.column()),
        )
        changes = {}
        for i, idx in enumerate(cells):
            if i >= len(raw):
                break
            pos = idx.row() * 16 + (idx.column() - 1)
            if pos < len(self.model._data) and self.model._data[pos] != raw[i]:
                 changes[pos] = raw[i]

        if changes:
            self.undo_stack.push(HistoryManager(self.model, changes, 'Paste hex bytes'))

    def _fill_zero(self) -> None:
        if not self.model:
            return
        selection_model = self.table_view.selectionModel()
        changes = {}
        for idx in selection_model.selectedIndexes():
            if 1 <= idx.column() <= 16:
                pos = idx.row() * 16 + (idx.column() - 1)
                if pos < len(self.model._data) and self.model._data[pos] != 0:
                    changes[pos] = 0
        if changes:
            self.undo_stack.push(HistoryManager(self.model, changes, 'Fill zeros'))

    def _show_context_menu(self, pos) -> None:
        if not self.model:
            return
        menu = QMenu(self)

        copy_menu = menu.addMenu('Copy')
        for label, fmt in (
            ('As hex bytes  (4A 2F …)',      'hex'),
            ('As Python literal  (b"\\x…")', 'python'),
            ('As C array  (0x4A, 0x2F …)',   'c'),
            ('As ASCII text  (SLZ. …)',      'ascii'),
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

    def _search_next(self, reverse: bool = False) -> None:
        if not self.model:
            return
        needle = self._parse_search_input()
        if not needle:
            self.search_status.setText('Invalid pattern')
            return
        data  = self.model.get_bytes()
        start = self._current_byte_offset() + (0 if reverse else 1)
        pos   = data.rfind(needle, 0, start) if reverse else data.find(needle, start)
        if pos == -1: # Wrap
            pos = data.rfind(needle) if reverse else data.find(needle)
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

    def _select_byte_range(self, byte_pos: int, length: int) -> None:
        if not self.model:
            return
        selection_model = self.table_view.selectionModel()
        selection_model.clearSelection()
        selection = QItemSelection()
        for i in range(length):
            pos = byte_pos + i
            row = pos // 16
            col = (pos % 16) + 1
            idx = self.model.index(row, col)
            selection.select(idx, idx)
        selection_model.select(selection, selection_model.SelectionFlag.Select)
        self.table_view.scrollTo(self.model.index(byte_pos // 16, (byte_pos % 16) + 1))

    def _current_byte_offset(self) -> int:
        idx = self.table_view.currentIndex()
        if idx.isValid() and 1 <= idx.column() <= 16:
            return idx.row() * 16 + (idx.column() - 1)
        return 0

    ###-------------------------- Inspector ----------------------------------###

    def _on_cursor_changed(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if not self.model or not current.isValid() or not (1 <= current.column() <= 16):
            return
        pos  = current.row() * 16 + (current.column() - 1)
        data = self.model.get_bytes()

        self._insp_offset.setText(f'Offset: {hex(pos)}')
        self._insp_u8.setText(f'u8: {data[pos]}' if pos < len(data) else 'u8: -')
        self._insp_i8.setText(
            f'i8: {struct.unpack_from("b", data, pos)[0]}' if pos < len(data) else 'i8: -'
        )
        def safe_unpack(fmt: str, size: int) -> str:
            return str(struct.unpack_from(fmt, data, pos)[0]) if pos + size <= len(data) else '-'

        self._insp_u16_le.setText(f'u16 LE: {safe_unpack("<H", 2)}')
        self._insp_u32_le.setText(f'u32 LE: {safe_unpack("<I", 4)}')

    def _on_selection_changed(self, *_) -> None:
        raw = self._selected_bytes()
        n   = len(raw)
        self._insp_sel.setText(f'Sel: {n} byte{"s" if n != 1 else ""}' if raw else 'Sel: -')

    def _reset_inspector(self) -> None:
        for lbl in (self._insp_offset, self._insp_u8, self._insp_i8, self._insp_sel):
            text = lbl.text().split(':')[0]
            lbl.setText(f'{text}: —')


###---------------------------------------- Model -------------------------------------###

class HexTableModel(QAbstractTableModel):
    '''
    18 columns:
      col  0      — offset label (read-only)
      cols 1-16   — hex byte cells (editable)
      col  17     — ASCII dump (read-only)
    '''
    _COLUMNS = 18
    # Colours for modified bytes
    _MODIFIED_FG = QColor('#E2A96B')
    _MODIFIED_BG = QColor('#2A2218')

    def __init__(self, data: bytes, undo_stack: QUndoStack, parent=None) -> None:
        super().__init__(parent)
        self._data:     bytearray  = bytearray(data)
        self._original: bytes      = data
        self._modified: set[int]   = set()
        self.undo_stack            = undo_stack

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
        pos = row * 16 + (col - 1)

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            
            if col == 0:
                return f'{row * 16:08X}'
            if 1 <= col <= 16:
                return f'{self._data[pos]:02X}' if pos < len(self._data) else ''
            if col == 17:
                chunk = self._data[row * 16: (row + 1) * 16]
                return ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)

        if role == Qt.ItemDataRole.ForegroundRole:
            if 1 <= col <= 16:
                if pos in self._modified:
                    return QBrush(self._MODIFIED_FG)
            elif col == 0:
                # Mute the offset address column
                return QBrush(QColor('#777777')) 
            elif col == 17:
                # Mute the ASCII dump column
                return QBrush(QColor('#888888'))

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

        if self._data[pos] == new_byte:
            return False
        self.undo_stack.push(HistoryManager(self, {pos: new_byte}, f'Edit byte at {hex(pos)}'))
        return True

    def apply_changes_dict(self, changes: dict[int, int]) -> None:
        '''Called by undo/redo to mutate the model'''
        for pos, val in changes.items():
            self._data[pos] = val
            if val != self._original[pos]:
                self._modified.add(pos)
            else:
                self._modified.discard(pos)
            
            row, col = pos // 16, (pos % 16) + 1
            idx = self.index(row, col)
            self.dataChanged.emit(idx, idx)
            self.dataChanged.emit(self.index(row, 17), self.index(row, 17))

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        return None
    
    ###--------------------------------- Helpers -------------------------------------###

    def get_bytes(self) -> bytes:
        return bytes(self._data)

    def get_bytes_mutable(self) -> bytearray:
        '''Return the internal bytearray directly — callers must call notify_all_changed after.'''
        return self._data

