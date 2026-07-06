'''Custom rules for painting, editing, and selecting for a proper hex editor/viewer feel.
Used by the HexEditor and StagingPage diff'''
from __future__ import annotations

from PyQt6.QtWidgets import (
    QStyledItemDelegate, QStyle, QTableView, QHeaderView,
    QLineEdit, QAbstractItemView
)
from PyQt6.QtCore import (
    QEvent, QItemSelectionModel, QItemSelection, QModelIndex, QRegularExpression,
    Qt, QItemSelectionRange, QAbstractTableModel
)
from PyQt6.QtGui import QRegularExpressionValidator, QBrush

BYTES_PER_ROW  = 16
COL_OFFSET     = 0
COL_BYTE_START = 1
COL_BYTE_END   = 16
COL_ASCII      = 17
TOTAL_COLUMNS  = 18
EDITABLE_COLUMNS = range(COL_BYTE_START, COL_BYTE_END + 1)

class HexCellDelegate(QStyledItemDelegate):
    '''Creates the sequential nature of a hex editor'''
    def __init__(self, view: QTableView, parent=None) -> None:
        super().__init__(parent)
        self._view = view

    ### ------------ Painting ------------- ###

    def paint(self, painter, option, index) -> None:
        '''Snapshots the state into opt before painting'''
        if index.column() in (COL_OFFSET, COL_ASCII):
            opt = type(option)(option)
            opt.state &= ~QStyle.StateFlag.State_MouseOver
            opt.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, index)

    ### ------------ Editing -------------- ###

    def createEditor(self, parent, option, index: QModelIndex):
        if index.column() not in EDITABLE_COLUMNS:
            return None
        editor = QLineEdit(parent)
        editor.setFrame(False)
        editor.setMaxLength(2)
        editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
        editor.setValidator(QRegularExpressionValidator(QRegularExpression('^[0-9A-Fa-f]{0,2}$'), editor))
        editor.textEdited.connect(lambda text, e=editor: self._on_text_edited(e, text))
        return editor

    def setEditorData(self, editor, index: QModelIndex) -> None:
        super().setEditorData(editor, index)
        if isinstance(editor, QLineEdit):
            editor.selectAll()

    def setModelData(self, editor, model, index: QModelIndex) -> None:
        super().setModelData(editor, model, index)

    def _on_text_edited(self, editor: QLineEdit, text: str) -> None:
        '''Typing characters into a cell advances to the next sequential cell'''
        if len(text) < 2:
            return
        self.commitData.emit(editor)
        self.closeEditor.emit(editor, QStyledItemDelegate.EndEditHint.NoHint)
        self._advance_to_next_cell(self._view.currentIndex())

    def _advance_to_next_cell(self, index: QModelIndex) -> None:
        model = self._view.model()
        if not index.isValid() or model is None:
            return
        row, col = index.row(), index.column()
        if col < COL_BYTE_END:
            next_index = model.index(row, col + 1)
        else:
            next_row = row + 1
            if next_row >= model.rowCount():
                return
            next_index = model.index(next_row, COL_BYTE_START)
        self._view.setCurrentIndex(next_index)
        self._view.edit(next_index)

class HexTableView(QTableView):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName('TextMono')
        self.horizontalHeader().setVisible(False)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setSelectionMode(QTableView.SelectionMode.ContiguousSelection)
        self.horizontalHeader().setMinimumSectionSize(0)
        self.setItemDelegate(HexCellDelegate(self))

        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked  |
            QAbstractItemView.EditTrigger.EditKeyPressed |
            QAbstractItemView.EditTrigger.AnyKeyPressed
        )

    def mousePressEvent(self, event) -> None:
        '''Marks the start of a new selection event explicitly.
        Trying to infer results in qt always trying to draw a rectangle'''
        selection_model = self.selectionModel()
        if isinstance(selection_model, LinearSelectionModel):
            selection_model.begin_new_gesture()
        super().mousePressEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.recalculate_column_widths()

    def setModel(self, model) -> None:
        super().setModel(model)
        if model:
            sequential_selection = LinearSelectionModel(model, self)
            self.setSelectionModel(sequential_selection)
        self.recalculate_column_widths()

    def recalculate_column_widths(self) -> None:
        model = self.model()
        if not model or model.columnCount() < TOTAL_COLUMNS:
            return
        header = self.horizontalHeader()
        if header is None:
            return

        fm = self.fontMetrics()
        offset_width = fm.horizontalAdvance('00000000') + 16
        cell_width = fm.horizontalAdvance('FF') + 8
        header.setSectionResizeMode(COL_OFFSET, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(COL_OFFSET, offset_width)
        for col in EDITABLE_COLUMNS:
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            self.setColumnWidth(col, cell_width)
        # Overwrite the last column width for spacing before the ascii
        self.setColumnWidth(COL_BYTE_END, cell_width + 8)
        header.setSectionResizeMode(COL_ASCII, QHeaderView.ResizeMode.Stretch)

class LinearSelectionModel(QItemSelectionModel):
    def __init__(self, model, parent=None):
        super().__init__(model, parent)
        self._anchor_offset:  int | None = None
        self._active_gesture: bool       = True

    def begin_new_gesture(self) -> None:
        '''Gates select calls behind new selection events'''
        self._active_gesture = True

    def index_to_offset(self, index: QModelIndex) -> int | None:
        if not index.isValid() or index.column() not in EDITABLE_COLUMNS:
            return None
        return index.row() * BYTES_PER_ROW + (index.column() - COL_BYTE_START)

    def select(self, selection, flags):
        target_offset: int | None = None
        # Explicitly verify the mask
        is_clear_and_select = (
            flags & QItemSelectionModel.SelectionFlag.ClearAndSelect
        ) == QItemSelectionModel.SelectionFlag.ClearAndSelect

        if isinstance(selection, QModelIndex): # Single Cell
            target_index: QModelIndex = selection
            target_offset = self.index_to_offset(target_index)

            if target_offset is None and target_index.isValid(): # Selection on offset or ascii column
                row = target_index.row()
                target_offset = row * BYTES_PER_ROW + (BYTES_PER_ROW - 1)
                if (self._active_gesture or self._anchor_offset is None
                        or is_clear_and_select):
                    self._anchor_offset  = row * BYTES_PER_ROW
                    self._active_gesture = False
            elif (self._active_gesture or self._anchor_offset is None # Selection on hex cell
                  or is_clear_and_select):
                self._anchor_offset  = target_offset
                self._active_gesture = False

        elif isinstance(selection, QItemSelection) and not selection.isEmpty(): # Multiple Cells
            qrange: QItemSelectionRange = selection[0]

            if self._active_gesture or self._anchor_offset is None: # The First selection, sets anchor
                anchor_row = qrange.top()
                anchor_col = max(COL_BYTE_START, min(COL_BYTE_END, qrange.left()))
                self._anchor_offset = anchor_row * BYTES_PER_ROW + (anchor_col - COL_BYTE_START)
                self._active_gesture = False
            
            anchor_row    = self._anchor_offset // BYTES_PER_ROW
            anchor_col    = (self._anchor_offset % BYTES_PER_ROW) + COL_BYTE_START
            target_row    = qrange.bottom() if anchor_row == qrange.top() else qrange.top()
            left_col      = max(COL_BYTE_START, min(COL_BYTE_END, qrange.left()))
            right_col     = max(COL_BYTE_START, min(COL_BYTE_END, qrange.right()))
            target_col    = right_col if anchor_col == left_col else left_col
            target_offset = target_row * BYTES_PER_ROW + (target_col - COL_BYTE_START)

        else: # Fallback
            super().select(selection, flags)
            return

        if target_offset is None or self._anchor_offset is None:
            super().select(selection, flags)
            return

        if flags & QItemSelectionModel.SelectionFlag.Select: # Calculate the sequential selection size
            model = self.model()
            if model is None:
                super().select(selection, flags)
                return

            start = min(self._anchor_offset, target_offset)
            end   = max(self._anchor_offset, target_offset)
            sequential_selection = QItemSelection()
            current_offset = start
            while current_offset <= end:
                row       = current_offset // BYTES_PER_ROW
                start_col = (current_offset % BYTES_PER_ROW) + COL_BYTE_START
                remaining_in_row = BYTES_PER_ROW - (start_col - COL_BYTE_START)
                run_length       = min(remaining_in_row, end - current_offset + 1)
                end_col          = start_col + run_length - 1
                top_left: QModelIndex     = model.index(row, start_col)
                bottom_right: QModelIndex = model.index(row, end_col)
                sequential_selection.select(top_left, bottom_right)

                current_offset += run_length

            if flags & QItemSelectionModel.SelectionFlag.ClearAndSelect: # Apply the selection
                self.clearSelection()
                flags = QItemSelectionModel.SelectionFlag.Select
            super().select(sequential_selection, flags)
        else:
            super().select(selection, flags)

class HexGridModelBase(QAbstractTableModel):
    '''Hex model base shared by HexEditor and StagingPage diff'''
    def __init__(self, data: bytes, parent=None) -> None:
        super().__init__(parent)
        self._data: bytes = data

    def rowCount(self, parent=QModelIndex()) -> int:
        return max(1, (len(self._data) + (BYTES_PER_ROW - 1)) // BYTES_PER_ROW)

    def columnCount(self, parent=QModelIndex()) -> int:
        return TOTAL_COLUMNS

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        if index.column() in (COL_OFFSET, COL_ASCII):
            return Qt.ItemFlag.ItemIsEnabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    
    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        pos = row * BYTES_PER_ROW + (col - COL_BYTE_START)

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if col == COL_OFFSET:
                return f'{row * BYTES_PER_ROW:08X}'
            if col in EDITABLE_COLUMNS:
                return f'{self._data[pos]:02X}' if pos < len(self._data) else ''
            if col == COL_ASCII:
                chunk = self._data[row * BYTES_PER_ROW: (row + 1) * BYTES_PER_ROW]
                return ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)
        if role in (Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole):
            return self._cell_color(pos, col, role)
        return None

    def _cell_color(self, pos: int, col: int, role: int) -> QBrush | None:
        '''Overridden for color specifications'''
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        return None

    def get_bytes(self) -> bytes:
        return self._data