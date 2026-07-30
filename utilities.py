'''
Common functions and classes; Resource fetching, string formatting, UI utilities.
'''
from __future__ import annotations

import sys
from pathlib import Path
from PyQt6.QtWidgets import QFrame, QWidget, QVBoxLayout, QLabel, QProgressBar
from PyQt6.QtCore import Qt, QTimer

def get_resource_path(relative_path: str | Path) -> Path:
    '''
    Used to get the path to an asset, either from the frozen build or from source.
    '''
    if hasattr(sys, '_MEIPASS'): # For frozen builds
        base_path = Path(sys._MEIPASS)
    else: # For running from source
        base_path = Path(__file__).resolve().parent
    return base_path / relative_path


def human_size(n: int) -> str:
    '''Converts bytes into a human-readable string'''
    if n < 0:
        return 'Invalid Size'
    if n == 0:
        return '0 B'
    value = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024:
            return f'{value:.1f} {unit}' if unit != 'B' else f'{value} B'
        value /= 1024
    return f'{value:.1f} TB'


def hline() -> QFrame:
    f = QFrame()
    f.setObjectName('HLine')
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f

def vline() -> QFrame:
    f = QFrame()
    f.setObjectName('VLine')
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFrameShadow(QFrame.Shadow.Sunken)
    return f


class ToastProgressBar(QWidget):
    '''
    Toast-style progress bar.
    '''
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 30, 0)

        self.frame = QFrame()
        self.frame.setObjectName('Popup')

        frame_layout = QVBoxLayout(self.frame)
        self.label = QLabel("")
        self.label.setAlignment(Qt.AlignmentFlag.AlignLeft)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(True)

        frame_layout.addWidget(self.label)
        frame_layout.addWidget(self.progress)
        layout.addWidget(self.frame)

        self.hide_timer = QTimer()
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide)
        self.hide()

    def show_progress(self, value: int, title: str) -> None:
        self.progress.setValue(value)
        self.label.setText(title)
        if not self.isVisible():
            self.raise_()
            self._reposition()
            self.show()
        if value >= 100:
            self.hide_timer.start(2500)
        else:
            self.hide_timer.stop()

    def _reposition(self) -> None:
        if not self.parent():
            return
        self.adjustSize()
        parent_rect = self.parent().rect()
        padding_x = 30
        padding_y = 40
        x = parent_rect.width() - self.width() - padding_x
        y = parent_rect.height() - self.height() - padding_y
        self.setGeometry(x, y, self.width(), self.height())
