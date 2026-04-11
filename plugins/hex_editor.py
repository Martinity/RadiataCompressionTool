from PyQt6.QtWidgets import QPlainTextEdit, QVBoxLayout, QMessageBox
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont
from core.contracts import BaseEditorWidget
from core.registry import Registry
import logging
logger = logging.getLogger(f'radiata.{__name__}')

@Registry.register(name='Hex Editor', extensions=(), magics=(), is_fallback=True)
class HexEditorWidget(BaseEditorWidget):
    data_modified = pyqtSignal(object, bytes)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_node = None
        # Layout setup
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        # Configure UI
        self.editor = QPlainTextEdit()
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setFont(QFont('Courier New', 10))
        self.editor.setStyleSheet('background-color: #1e1e1e; color: #dcdcdc;')

        layout.addWidget(self.editor)

    def load_node(self, node, data):
        super().load_node(node, data)
        self.current_node = node

        MAX_SIZE = 1_048_576
        display_data = data[:MAX_SIZE]

        if not data:
            self.editor.setPlainText('Empty File or Unable to Read Data')
            return

        logger.debug(f'loading node {node.name} size {node.size * 2048}')
        if len(data) > MAX_SIZE:
            logger.warning(f'View truncated by {len(data) - MAX_SIZE}')

        hex_dump = self._format_hex_dump(display_data)
        self.editor.setPlainText(hex_dump)

    def _handle_apply(self):
        if not self.current_node: 
            return

        try:
            new_bytes = self.get_modified_data()
            self.data_modified.emit(self.current_node, new_bytes)
            logger.info(f'Changes staged for {self.current_node.name}')
        except ValueError as e:
            QMessageBox.critical(self, 'Parse Error', f'Invalid hex data format: {e}')
            logger.error(f'Failed to parse hex dump: {e}')

    def get_modified_data(self) -> bytes:
        text = self.editor.toPlainText()
        return self._parse_hex_dump(text)

    def _format_hex_dump(self, data: bytes) -> str:
        '''format and transform bytes to str'''
        lines = []
        view = memoryview(data)

        for i in range(0, len(view), 16):
            chunk = data[i:i+16]

            offset_str = f'{i:08X}'
            hex_str = ' '.join(f'{b:02X}' for b in chunk).ljust(47)
            ascii_str = ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)

            lines.append(f'{offset_str}  {hex_str}  |{ascii_str}|')
        logger.debug(f'Successfully formated {len(lines)} lines.')
        return '\n'.join(lines)

    def _parse_hex_dump(self, text: str) -> bytes:
        '''reverse the bytes to str formatting'''
        processed = bytearray()

        for line in text.splitlines():
            if not line.strip(): 
                continue

            parts = line.split('  ')
            if len(parts) >= 2:
                hex_string = parts[1].strip()
                hex_pairs = [pair for pair in hex_string.split(' ') if pair]
                for pair in hex_pairs:
                    processed.append(int(pair, 16))

        return bytes(processed)