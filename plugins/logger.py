from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QPushButton, QPlainTextEdit, QWidget
from PyQt6.QtGui import QTextCursor, QColor, QFont
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

import logging

class QtLogHandler(logging.Handler, QObject):
    '''Python logs routed to Qt signals'''
    log_signal = pyqtSignal(str, int) # format: message, level

    def __init__(self, parent: QObject | None = None):
        logging.Handler.__init__(self)
        QObject.__init__(self, parent)

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        self.log_signal.emit(msg, record.levelno)


def setup_logging(level: int = logging.INFO) -> QtLogHandler:
    '''Called on application launch. Returns logger, log_signal'''
    logger = logging.getLogger('radiata')
    logger.setLevel(level)
    logger.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter('%(levelname)s: %(name)s - %(message)s'))
    logger.addHandler(console)

    qt_handler = QtLogHandler()
    qt_handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(qt_handler)

    return qt_handler

###---------------------------------------- UI ---------------------------------------------###

class LoggingWindow(QWidget):
    '''Log viewer widget'''
    def __init__(self, parent=None):
        super().__init__(parent)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setFont(QFont('Courier New', 9))
        self.log_view.setStyleSheet('QPlainTextEdit {background-color: #1e1e1e; color: #dcdcdc;}')

        toolbar = QHBoxLayout()
        clear_button = QPushButton('Clear Log')
        clear_button.clicked.connect(self.log_view.clear)
        toolbar.addWidget(clear_button)
        toolbar.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.addLayout(toolbar)
        layout.addWidget(self.log_view)

    def __repr__(self) -> str:
        lines = self.log_view.document().lineCount()
        return f"<LoggingWindow lines={lines}>"

    def __str__(self) -> str:
        return "Log Console"
    
    @pyqtSlot(str, int)
    def append_log(self, message: str, level: int):
        '''Slot gets logs from QtLogHandler'''
        colors = {
            logging.DEBUG: "#7CFB41",
            logging.INFO: "#ffffff",
            logging.WARNING: "#ffaa55",
            logging.ERROR: "#ff5555",
            logging.CRITICAL: "#ff0000"
        }
        color = colors.get(level, "#ffffff")

        cursor = self.log_view.textCursor()
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        _format = cursor.charFormat()
        _format.setForeground(QColor(color))
        cursor.setCharFormat(_format)

        cursor.insertText(message + '\n')
        cursor.endEditBlock()

        self.log_view.ensureCursorVisible()