'''Global log signal for the application.
For more advanced debugging implementation should use {__name__}
Only signals messages to the workspace log console (or any build console where __name__ helps)'''
from __future__ import annotations

from PyQt6.QtWidgets import QGridLayout, QPushButton, QPlainTextEdit, QWidget
from PyQt6.QtGui import QTextCursor, QColor
from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot, Qt

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
        self.log_view.setObjectName('LogView')
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self.clear_button = QPushButton('Clear')
        self.clear_button.setObjectName('FloatClearButton')
        self.clear_button.clicked.connect(self.log_view.clear)

        layout = QGridLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.addWidget(self.log_view, 0, 0)

        layout.addWidget(
            self.clear_button,
            0,0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight
        )

    def __repr__(self) -> str:
        lines = self.log_view.document().lineCount()
        return f"<LoggingWindow lines={lines}>"

    def __str__(self) -> str:
        return "Log Console"
    
    @pyqtSlot(str, int)
    def append_log(self, message: str, level: int):
        '''Slot gets logs from QtLogHandler'''
        scrollbar = self.log_view.verticalScrollBar()
        at_bottom = scrollbar.value() >= (scrollbar.maximum() - 10)

        colors = {
            logging.DEBUG: "#7f6df2",
            logging.INFO: "#dcddde",
            logging.WARNING: "#ffaa33",
            logging.ERROR: "#ff3333",
            logging.CRITICAL: "#990000"
        }
        color = colors.get(level, "#dcddde")

        cursor = self.log_view.textCursor()
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        _format = cursor.charFormat()
        _format.setForeground(QColor(color))
        cursor.setCharFormat(_format)

        cursor.insertText(message + '\n')
        cursor.endEditBlock()

        if at_bottom:
            self.log_view.moveCursor(QTextCursor.MoveOperation.End)
            # self.log_view.ensureCursorVisible()