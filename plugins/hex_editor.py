from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QMessageBox, QTableView, QHeaderView, QWidget
from PyQt6.QtCore import pyqtSignal, Qt, QAbstractTableModel, QModelIndex
from PyQt6.QtGui import QFont, QShortcut, QKeySequence
from core.contracts import BaseEditor
from core.registry import Registry
import logging
logger = logging.getLogger(f'radiata.{__name__}')

@Registry.register(name='Hex Editor', extensions=(), is_fallback=True)
class HexEditorWidget(BaseEditor):
    data_modified = pyqtSignal(object, bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_node = None
        self.model = None
        self._setup_ui()
        self._setup_shortcuts()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar (Same as before)
        self.toolbar = QWidget()
        self.toolbar.setObjectName('EditorToolbar')
        tool_layout = QHBoxLayout(self.toolbar)
        tool_layout.setContentsMargins(10, 5, 10, 5)

        self.info_label = QLabel("Hex View")
        self.btn_apply = QPushButton("Apply Changes")
        self.btn_apply.setFixedWidth(120)
        self.btn_apply.setEnabled(False)
        self.btn_apply.clicked.connect(self._handle_apply)

        tool_layout.addWidget(self.info_label)
        tool_layout.addStretch()
        tool_layout.addWidget(self.btn_apply)

        # The New Table View
        self.table_view = QTableView()
        self.table_view.setObjectName('HexView')
        self.table_view.setFont(QFont('Courier New', 10))

        # Hide standard headers to look like a clean hex editor
        self.table_view.horizontalHeader().setVisible(False)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.setShowGrid(False)

        layout.addWidget(self.toolbar)
        layout.addWidget(self.table_view)

    def load_node(self, node, data):
        super().load_node(node, data)
        self.current_node = node
        self.btn_apply.setEnabled(True)
        self.info_label.setText(f"Editing: {node.name}")

        # Instantiate and set the new model
        self.model = HexTableModel(data)
        self.table_view.setModel(self.model)

        # Format column widths
        h_header = self.table_view.horizontalHeader()
        h_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table_view.setColumnWidth(0, 80) # Offset
        for i in range(1, 17):
            h_header.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed) # Hex cells (narrow)
            self.table_view.setColumnWidth(i, 25)
        h_header.setSectionResizeMode(17, QHeaderView.ResizeMode.Stretch) # ASCII dump 

        logger.debug(f"Loaded {node.name} into Hex Editor Table.")

    def _handle_apply(self):
        if not self.current_node or not self.model: 
            return

        # Fetch the mutated bytes directly from the model
        new_bytes = self.model.get_bytes()
        self.apply_requested.emit(self.current_node, new_bytes)
        
        QMessageBox.information(self, "Success", f"Changes applied to {self.current_node.name}")

    def get_modified_data(self) -> bytes:
        return self.model.get_bytes() if self.model else b''

    def _setup_shortcuts(self) -> None:
        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self.save_shortcut.activated.connect(self._handle_apply)
    
    
class HexTableModel(QAbstractTableModel):
    def __init__(self, data: bytes, parent=None):
        super().__init__(parent)
        # We use a bytearray so we can mutate individual bytes
        self._data = bytearray(data)
        self._columns = 18 # 1 Offset + 16 Hex + 1 ASCII

    def rowCount(self, parent=QModelIndex()) -> int:
        # Calculate rows needed (ceil division)
        return (len(self._data) + 15) // 16

    def columnCount(self, parent=QModelIndex()) -> int:
        return self._columns

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
            
        col = index.column()
        # Offset (0) and ASCII (17) are strictly Read-Only
        if col == 0 or col == 17:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            
        # Hex cells (1-16) are Editable
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return None

        row = index.row()
        col = index.column()
        data_index = (row * 16) + (col - 1)

        if col == 0:
            # Column 0: Offset
            return f"{row * 16:08X}"
            
        elif 1 <= col <= 16:
            # Columns 1-16: Hex Data
            if data_index < len(self._data):
                return f"{self._data[data_index]:02X}"
            return "" # Empty cell if we are at the end of the file
            
        elif col == 17:
            # Column 17: ASCII Dump
            chunk = self._data[row * 16 : (row + 1) * 16]
            return "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)

    def setData(self, index: QModelIndex, value: str, role: int = Qt.ItemDataRole.EditRole) -> bool:
        '''Handles the user typing into a hex cell'''
        if role == Qt.ItemDataRole.EditRole and 1 <= index.column() <= 16:
            data_index = (index.row() * 16) + (index.column() - 1)
            
            if data_index >= len(self._data):
                return False

            try:
                # Ensure they only typed a valid 2-character hex byte
                clean_val = value.strip()
                if len(clean_val) > 2:
                    return False
                new_byte = int(clean_val, 16)
                
                # Update the data
                self._data[data_index] = new_byte
                
                # Tell the UI that this hex cell AND the ASCII cell changed
                self.dataChanged.emit(index, index)
                ascii_index = self.index(index.row(), 17)
                self.dataChanged.emit(ascii_index, ascii_index)
                return True
            except ValueError:
                return False # Invalid hex typed
        return False

    def get_bytes(self) -> bytes:
        return bytes(self._data)