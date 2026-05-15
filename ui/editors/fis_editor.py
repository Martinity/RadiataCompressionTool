from __future__ import annotations

from typing import Callable
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton, QScrollArea, QSizePolicy, QFrame, QFileDialog
)
from PyQt6.QtGui import QPixmap, QImage

from core.contracts import BaseViewer
from core.registry import Registry
from core.node import VfsNode
from core.handlers.fis_handler import FisEditorPayload, FISInfo

import logging
logger = logging.getLogger(f'radiata.{__name__}')

@Registry.register(name='FIS Texture')
class FisEditorWidget(BaseViewer):
    '''Displays a decoded FIS texture with metadata.'''

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.node: VfsNode | None = None
        self.img:  QImage  | None = None
        self.info: FISInfo | None = None
        self.raw_png: bytes | None = None
        self._setup_ui()

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())

        body = QHBoxLayout()
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(8)
        body.addWidget(self._build_image_area(), stretch=3)
        body.addWidget(self._build_info_panel(), stretch=1)
        root.addLayout(body)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName('EditorToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 5, 10, 5)

        self._title_label = QLabel('FIS Texture Viewer')
        self._btn_export  = QPushButton('Export PNG')
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._export_png)

        lay.addWidget(self._title_label)
        lay.addStretch()
        lay.addWidget(self._btn_export)
        return bar

    def _build_image_area(self) -> QScrollArea:
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._image_label.setMinimumSize(64, 64)
        self._image_label.setText('No texture loaded')

        area = QScrollArea()
        area.setWidget(self._image_label)
        area.setWidgetResizable(True)
        area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll_area = area
        return area

    def _build_info_panel(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setFixedWidth(220)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        self._info_rows: dict[str, QLabel] = {}
        for key in ('Name', 'PSM', 'BPP', 'Width', 'Height',
                    'Dim mode', 'Swizzled', 'Padded CLUT',
                    'Pal offset', 'Image offset', 'Image size'):
            row = QHBoxLayout()
            row.addWidget(QLabel(f'{key}:'))
            val = QLabel('—')
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setWordWrap(True)
            row.addWidget(val, stretch=1)
            lay.addLayout(row)
            self._info_rows[key] = val

        lay.addStretch()
        return frame
    
    def _export_png(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, self.node.name, '', 'All Files (*)')
        if not path:
            return
        with open(path, 'wb') as f:
            f.write(self.raw_png)
        logger.info(f'Node exported to {path.name}')
    
    def wheelEvent(self, event):
        '''Ctrl+scroll to ajust image size'''
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            elif delta < 0:
                self.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    def zoom_in(self):
        pass

    def zoom_out(self):
        pass

###----------------------------------- Contractuals ---------------------------------------------###

    def begin_loading(self, node: VfsNode) -> None:
        super().begin_loading(node)
        self.node = node
        self._image_label.setText(f'Loading {node.name}...')
        self._btn_export.setEnabled(False)

    def receive_data(self, result: FisEditorPayload, data_resolver: Callable[[VfsNode], bytes] | None = None) -> None:
        '''Override for recieving the FISEditorPayload'''
        self._data_resolver = data_resolver
        if isinstance(result, FisEditorPayload):
            self.img     = result.image
            self.info    = result.info
            self.raw_png = result.raw_bytes
            self._populate_ui()
        else:
            self._image_label.setText(f'Error loading texture: Got {type(result)} expected "FisEditorPayload"')

    def _populate_ui(self) -> None:
        self._display_image(self.img)
        self._populate_info(self.info)
        self._title_label.setText(
            f'{self.node.name}  —  {self.info.width}×{self.info.height}  {self.info.psm_name}'
        )
        self._btn_export.setEnabled(True)
        logger.debug(f'FIS: loaded {self.node.name} ({self.info.width}×{self.info.height} {self.info.psm_name})')

###------------------------------------ Display Helpers ---------------------------------------------###

    def _display_image(self, img: QImage) -> None:
        '''Show the decoded image, scaled to fit without distortion.'''
        pixmap = QPixmap.fromImage(img)
        # Scale to fit the scroll area while preserving aspect ratio
        available = self._scroll_area.size() - QSize(4, 4)
        if img.width() > available.width() or img.height() > available.height():
            pixmap = pixmap.scaled(
                available,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
        self._image_label.setPixmap(pixmap)
        self._image_label.resize(pixmap.size())

    def _show_error(self, message: str) -> None:
        self._image_label.setPixmap(QPixmap())
        self._image_label.setText(message)
        self._btn_export.setEnabled(False)
        for lbl in self._info_rows.values():
            lbl.setText('—')

    def _populate_info(self, info: FISInfo) -> None:
        def fmt_hex(v: int | None) -> str:
            return '—' if v is None else hex(v)

        self._info_rows['Name'].setText(repr(info.name))
        self._info_rows['PSM'].setText(info.psm_name)
        self._info_rows['BPP'].setText(str(info.bpp))
        self._info_rows['Width'].setText(str(info.width))
        self._info_rows['Height'].setText(str(info.height))
        self._info_rows['Dim mode'].setText(info.dimension_mode)
        self._info_rows['Swizzled'].setText(str(info.swizzled))
        self._info_rows['Padded CLUT'].setText(str(info.padded_4bpp_clut))
        self._info_rows['Pal offset'].setText(fmt_hex(info.palette_offset))
        self._info_rows['Image offset'].setText(fmt_hex(info.image_offset))
        self._info_rows['Image size'].setText(fmt_hex(info.image_size))

