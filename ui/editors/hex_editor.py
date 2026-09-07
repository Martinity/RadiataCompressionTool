'''Global fallback editor for any node. Uses the hex_model to specify the specific rules.'''
from __future__ import annotations

import struct
from typing import Any
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox, QStackedLayout,
    QWidget, QMenu, QApplication, QLineEdit, QFrame
)
from PyQt6.QtCore import Qt, QModelIndex, pyqtSignal, QItemSelectionModel, QTimer
from PyQt6.QtGui import QShortcut, QColor, QBrush, QAction, QUndoCommand, QUndoStack, QClipboard

from ui.settings import Shortcut, Shortcuts
from core.contracts import BaseEditor
from core.handlers.generic_binary_leaf import GenericBinaryHandler
from core.registry import Registry
from core.node import VfsNode
from ui.hex_model import (
    HexTableView, BYTES_PER_ROW, EDITABLE_COLUMNS, COL_OFFSET,
    COL_BYTE_START, COL_ASCII, HexGridModelBase
)


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

    def __init__(self, parent: QWidget | None = None, data_resolver = None) -> None:
        super().__init__(parent)
        self._data_resolver = data_resolver
        self.model: HexTableModel | None = None

        self.undo_stack = QUndoStack(self)
        self.undo_stack.canUndoChanged.connect(self._on_history_changed)
        self.undo_stack.canRedoChanged.connect(self._on_history_changed)
        # Bandaid: get the state then push on the next event cycle to fix freeze
        # The source of the dirty state freeze needs more research
        self.undo_stack.cleanChanged.connect(
            lambda clean: QTimer.singleShot(0, lambda c=clean: self.set_dirty(not c))
        )
        self._setup_ui()
        self._setup_shortcuts()

    def _on_history_changed(self) -> None:
        self.undo_state_changed.emit(self.undo_stack.canUndo(), self.undo_stack.canRedo())

    def _setup_ui(self) -> None:
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self._status_label = QLabel()
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)

        editor_widget = QWidget()
        editor_layout = QVBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        editor_layout.addWidget(self._build_header_bar())

        self.table_view = HexTableView()
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._show_context_menu)
        self.table_view.setTabKeyNavigation(True)
        editor_layout.addWidget(self.table_view)

        editor_layout.addWidget(self._build_inspector())
        self._stack.addWidget(self._status_label)
        self._stack.addWidget(editor_widget)

    def _build_header_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('SurfaceToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 5, 10, 5)

        self.search_input = QLineEdit()
        self.search_input.setObjectName('TextSubtitle')
        self.search_input.setPlaceholderText('Search bytes (e.g. 4A 2F)...')
        self.search_input.returnPressed.connect(self._search_next)

        self.search_status = QLabel('')
        self.search_status.setFixedWidth(100)

        btn_prev = QPushButton('◀')
        btn_next = QPushButton('▶')
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
        frame.setObjectName('SurfaceToolbar')
        frame.setFixedHeight(28)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 2, 10, 2)
        lay.setSpacing(16)

        self._insp_labels: dict[str, QLabel] = {
            key: QLabel(f'{key}: -') for key in
            ('Offset', 'u8', 'i8', 'u16 LE', 'u32 LE', 'Sel')
        }
        for lbl in self._insp_labels.values():
            lay.addWidget(lbl)

        lay.addStretch()
        return frame

    def _setup_shortcuts(self) -> None:
        QShortcut(Shortcuts.sequence(Shortcut.COPY), self).activated.connect(lambda: self._copy('hex'))
        QShortcut(Shortcuts.sequence(Shortcut.FIND), self).activated.connect(self.search_input.setFocus)

    def show_error(self, message: str) -> None:
        '''Swap to the editor page in the stack and display error.'''
        self._status_label.setText(f'Error:\n{message}')
        self._stack.setCurrentIndex(0)
        super().show_error(message)   # also logs

    ###---------------------------- Contractuals ---------------------------------###

    def begin_loading(self, node: VfsNode) -> None:
        '''Shows placeholder while the worker thread fetches data'''
        super().begin_loading(node)
        self._status_label.setText(f'Loading {node.name}...')
        self._stack.setCurrentIndex(0)
        if self.model:
            self.table_view.setModel(None)
            self.model = None
        self._reset_inspector()

    def _populate_ui(self, data: Any) -> None:
        '''
        Build the hex model from raw bytes.
        Called by BaseEditor.receive_data (bytes path) and by discard_changes to refresh the view.
        Clearing the undo stack here marks it clean
        '''
        if not isinstance(data, bytes):
            self.show_error('Cannot display: expected bytes')
            return

        self.undo_stack.clear()
        self.model = HexTableModel(data, self.undo_stack)
        self.table_view.setModel(self.model)

        selection_model = self.table_view.selectionModel()
        if selection_model:
            selection_model.currentChanged.connect(self._on_cursor_changed)
            selection_model.selectionChanged.connect(self._on_selection_changed)
        self._stack.setCurrentIndex(1)


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
        clean_idx = self.undo_stack.cleanIndex()
        if clean_idx >= 0:
            self.undo_stack.setIndex(clean_idx)

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

    def _selected_positions(self) -> list[int]:
        '''Return the selected data in sequential order'''
        if not self.model:
            return []
        selection_model = self.table_view.selectionModel()
        if not selection_model:
            return []
        indexes = sorted(
            (idx for idx in selection_model.selectedIndexes() if idx.column() in EDITABLE_COLUMNS),
            key=lambda i: (i.row(), i.column()),
        )
        return [idx.row() * BYTES_PER_ROW + (idx.column() - COL_BYTE_START) for idx in indexes]

    def _selected_bytes(self) -> bytes:
        '''Return the selected data in bytes'''
        if not self.model:
            return b''
        data = self.model .get_bytes()
        return bytes(data[pos] for pos in self._selected_positions() if pos < len(data))

    @property
    def clipboard(self) -> QClipboard:
        clipboard = QApplication.clipboard()
        assert clipboard is not None
        return clipboard

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
        self.clipboard.setText(text)

    def _paste_hex(self) -> None:
        if not self.model:
            return
        text = self.clipboard.text()
        try:
            raw = bytes.fromhex(text.replace(' ', '').replace('\n', ''))
        except ValueError:
            QMessageBox.warning(self, 'Paste error', 'Clipboard is not valid hex bytes.')
            return
        positions = self._selected_positions()
        changes = {}
        for i, pos in enumerate(positions):
            if i >= len(raw):
                break
            if pos < len(self.model._data) and self.model._data[pos] != raw[i]:
                changes[pos] = raw[i]

        if changes:
            self.undo_stack.push(HistoryManager(self.model, changes, 'Paste hex bytes'))

    def _fill_zero(self) -> None:
        if not self.model:
            return
        changes = {
            pos: 0 for pos in self._selected_positions()
            if pos < len(self.model._data) and self.model._data[pos] != 0
        }
        if changes:
            self.undo_stack.push(HistoryManager(self.model, changes, 'Fill zeros'))

    def _show_context_menu(self, pos) -> None:
        if not self.model:
            return
        menu = QMenu(self)

        copy_menu = menu.addMenu('Copy')
        assert copy_menu is not None
        for label, fmt in (
            ('As hex bytes  (4A 2F …)',      'hex'),
            ('As Python literal  (b"\\x…")', 'python'),
            ('As C array  (0x4A, 0x2F …)',   'c'),
            ('As ASCII text  (SLZ. …)',      'ascii'),
        ):
            act = QAction(label, self)
            act.triggered.connect(lambda f=fmt: self._copy(f))
            copy_menu.addAction(act)

        menu.addSeparator()
        paste_act = QAction('Paste hex bytes', self)
        paste_act.triggered.connect(self._paste_hex)
        menu.addAction(paste_act)

        menu.addSeparator()
        fill_act = QAction('Fill selection with 00', self)
        fill_act.triggered.connect(self._fill_zero)
        menu.addAction(fill_act)

        vp = self.table_view.viewport()
        assert vp is not None
        menu.exec(vp.mapToGlobal(pos))

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
        '''ClearAndSelect to reset the LinearSelectionModel anchor to the new pos'''
        if not self.model or length <= 0:
            return
        selection_model = self.table_view.selectionModel()
        if not selection_model:
            return
        last_pos = byte_pos + length - 1
        start_index = self.model.index(byte_pos // 16, (byte_pos % 16) + 1)
        end_index = self.model.index(last_pos // 16, (last_pos % 16) + 1)

        selection_model.setCurrentIndex(
            start_index, QItemSelectionModel.SelectionFlag.ClearAndSelect
        )
        if length > 1:
            selection_model.select(end_index, QItemSelectionModel.SelectionFlag.Select)
        self.table_view.scrollTo(start_index)

    def _current_byte_offset(self) -> int:
        idx = self.table_view.currentIndex()
        if idx.isValid() and idx.column() in EDITABLE_COLUMNS:
            return idx.row() * BYTES_PER_ROW + (idx.column() - COL_BYTE_START)
        return 0

    ###-------------------------- Inspector ----------------------------------###

    def _on_cursor_changed(self, current: QModelIndex, _prev: QModelIndex) -> None:
        if not self.model or not current.isValid() or current.column() not in EDITABLE_COLUMNS:
            return
        pos   = current.row() * BYTES_PER_ROW + (current.column() - COL_BYTE_START)
        data = self.model.get_bytes()
        lbls = self._insp_labels

        lbls['Offset'].setText(f'Offset: {hex(pos)}')
        lbls['u8'].setText(f'u8: {data[pos]}' if pos < len(data) else 'u8: -')
        lbls['i8'].setText(f'i8: {struct.unpack_from("b", data, pos)[0]}' if pos < len(data) else 'i8: -')

        def safe_unpack(fmt: str, size: int) -> str:
            return str(struct.unpack_from(fmt, data, pos)[0]) if pos + size <= len(data) else '-'

        lbls['u16 LE'].setText(f'u16 LE: {safe_unpack("<H", 2)}')
        lbls['u32 LE'].setText(f'u32 LE: {safe_unpack("<I", 4)}')

    def _on_selection_changed(self, *_) -> None:
        raw = self._selected_bytes()
        n   = len(raw)
        self._insp_labels['Sel'].setText(f'Sel: {n} byte{"s" if n != 1 else ""}' if raw else 'Sel: -')

    def _reset_inspector(self) -> None:
        for key, lbl in self._insp_labels.items():
            lbl.setText(f'{key}: -')

###---------------------------------------- Model -------------------------------------###

class HexTableModel(HexGridModelBase):
    '''
    18 columns:
      col  0      — offset label   (read-only)
      cols 1-16   — hex byte cells (editable)
      col  17     — ASCII dump     (read-only)
    '''
    # Colours for modified bytes
    _MODIFIED_FG  = QColor('#E2A96B')
    _MODIFIED_BG  = QColor('#2A2218')
    _EXTREMITY_FG = QColor('#888888')

    def __init__(self, data: bytes, undo_stack: QUndoStack, parent=None) -> None:
        super().__init__(data, parent)
        self._data:     bytearray  = bytearray(data) # Overrides for mutability
        self._original: bytes      = data
        self._modified: set[int]   = set()
        self.undo_stack            = undo_stack

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        if index.column() in (COL_OFFSET, COL_ASCII):
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable

    def _cell_color(self, pos: int, col: int, role: int) -> QBrush | None:
        if col in EDITABLE_COLUMNS:
            if pos in self._modified:
                return QBrush(
                    self._MODIFIED_FG if role == Qt.ItemDataRole.ForegroundRole
                    else self._MODIFIED_BG
                )
            return None
        if col in (COL_OFFSET, COL_ASCII) and role == Qt.ItemDataRole.ForegroundRole:
            return QBrush(self._EXTREMITY_FG)
        return None

    def setData(self, index: QModelIndex, value: str, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or index.column() not in EDITABLE_COLUMNS:
            return False

        pos = index.row() * BYTES_PER_ROW + (index.column() - COL_BYTE_START)
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

            row = pos // BYTES_PER_ROW
            col = (pos % BYTES_PER_ROW) + COL_BYTE_START
            idx: QModelIndex = self.index(row, col)
            self.dataChanged.emit(idx, idx)
            ascii_idx: QModelIndex = self.index(row, COL_ASCII)
            self.dataChanged.emit(ascii_idx, ascii_idx)

    def get_bytes_mutable(self) -> bytearray:
        return self._data
