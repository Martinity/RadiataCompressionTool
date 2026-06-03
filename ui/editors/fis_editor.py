'''BaseEditor for modifiying FIS textures. All FIS textures are displayed as Indexed QImage'''
from __future__ import annotations

from typing import Callable, Any
from pathlib import Path
from PyQt6.QtCore import Qt, QSize, pyqtSignal, QPoint, QTimer, QObject
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton, QScrollArea, QSizePolicy, 
    QFrame, QFileDialog, QListWidget, QListView, QAbstractItemView, QListWidgetItem, QColorDialog
)
from PyQt6.QtGui import QPixmap, QImage, QColor, QIcon

from core.contracts import BaseEditor
from core.registry import Registry
from core.node import VfsNode
from core.handlers.fis_leaf import FisEditorPayload, FISInfo, FisHandler

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------------------- History ------------------------------------------------###

class HistoryManager(QObject):
    can_undo_changed = pyqtSignal(bool)
    can_redo_changed = pyqtSignal(bool)

    def __init__(self, debounce_ms: int = 400, parent=None):
        super().__init__(parent)
        self.undo_stack: list[QImage] = []
        self.redo_stack: list[QImage] = []

        self._current_state:  QImage | None = None
        self._baseline_state: QImage | None = None
        self._pending_state:  QImage | None = None

        self.debounce_timer = QTimer(self)
        self.debounce_timer.setSingleShot(True)
        self.debounce_timer.setInterval(debounce_ms)
        self.debounce_timer.timeout.connect(self._commit_state)

    def initialize(self, initial_state: QImage) -> None:
        self._current_state = initial_state.copy()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self._emit_status()

    def push_change(self, new_state: QImage) -> None:
        if not self.debounce_timer.isActive():
            self._baseline_state = self._current_state.copy() if self._current_state else new_state.copy()
        self._pending_state = new_state.copy()
        self.debounce_timer.start()

    def _commit_state(self) -> None:
        if self._baseline_state:
            self.undo_stack.append(self._baseline_state)
        self._current_state = self._pending_state.copy() if self._pending_state else None
        self.redo_stack.clear()
        self._emit_status()

    def undo(self) -> QImage | None:
        if self.debounce_timer.isActive():
            self._commit_state()
            self.debounce_timer.stop()
        if not self.undo_stack or not self._current_state:
            return None
        
        self.redo_stack.append(self._current_state.copy())
        self._current_state = self.undo_stack.pop()
        self._emit_status()
        return self._current_state.copy()

    def redo(self) -> QImage | None:
        if self.debounce_timer.isActive():
            self._commit_state()
            self.debounce_timer.stop()
        if not self.redo_stack or not self._current_state:
            return None

        self.undo_stack.append(self._current_state.copy())
        self._current_state = self.redo_stack.pop()
        return self._current_state.copy()

    def _emit_status(self):
        self.can_undo_changed.emit(bool(self.undo_stack))
        self.can_redo_changed.emit(bool(self.can_redo_changed))

###----------------------------------------- Canvas ------------------------------------------------###

class InteractiveCanvas(QLabel):
    '''Zoom-aware canvas for editing indexed QImages'''
    editing_started = pyqtSignal()
    painted         = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image_ref: QImage | None = None
        self.zoom_factor:       float = 1.0
        self.selected_color_idx:  int = -1

    def set_image(self, img: QImage, zoom: float):
        self.image_ref = img
        self.zoom_factor = zoom
        self.update_display()

    def update_display(self):
        if not self.image_ref:
            return
        new_size = QSize(
            int(self.image_ref.width() * self.zoom_factor),
            int(self.image_ref.height() * self.zoom_factor)
        )
        pixmap = QPixmap.fromImage(self.image_ref).scaled(
            new_size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation
        )
        self.setPixmap(pixmap)
        self.resize(pixmap.size())

    def mousePressEvent(self, event) -> None:
        if event.button() & Qt.MouseButton.LeftButton:
            self.editing_started.emit()
            self._paint_pixel(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._paint_pixel(event.position().toPoint())
        super().mouseMoveEvent(event)

    def _paint_pixel(self, pos: QPoint):
        if not self.image_ref or self.selected_color_idx < 0:
            return
        x = int(pos.x() / self.zoom_factor)
        y = int(pos.y() / self.zoom_factor)
        if 0 <= x < self.image_ref.width() and 0 <= y < self.image_ref.height():
            if self.image_ref.pixelIndex(x, y) != self.selected_color_idx:
                self.image_ref.setPixel(x, y, self.selected_color_idx)
                self.update_display()
                self.painted.emit()    

###-------------------------------------------------- Editor -------------------------------------------------###

@Registry.register_editor(name='FIS Texture Editor', handler=FisHandler, extensions=('.fis',))
class FisEditorWidget(BaseEditor):
    '''Displays a decoded FIS texture with metadata.'''

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.node:  VfsNode | None = None
        self.img:   QImage  | None = None
        self.info:  FISInfo | None = None
        self.raw_fis: bytes | None = None

        self.history = HistoryManager(debounce_ms=400, parent=self)

        self._zoom_factor: float = 1.0
        self._zoom_step:   float = 1.2
        self._min_zoom:    float = 0.1
        self._max_zoom:    float = 30.0
        self._is_panning:  bool  = False

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
        self._image_label = InteractiveCanvas()
        self._image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._image_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._image_label.setMinimumSize(64, 64)
        self._image_label.setText('No texture loaded')

        self._image_label.painted.connect(lambda: self.set_dirty(True))

        area = QScrollArea()
        area.setWidget(self._image_label)
        area.setWidgetResizable(False)
        area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        area.viewport().installEventFilter(self)
        self._image_label.installEventFilter(self)
        self._scroll_area = area
        return area

    def _build_info_panel(self) -> QFrame:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame.setFixedWidth(240)
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
        lay.addSpacing(15)
        lay.addWidget(QLabel('<b>Color Look-Up Table (CLUT)</b>'))

        self._palette_list = QListWidget()
        self._palette_list.setViewMode(QListView.ViewMode.IconMode)
        self._palette_list.setIconSize(QSize(20, 20))
        self._palette_list.setSpacing(2)
        self._palette_list.setResizeMode(QListView.ResizeMode.Adjust)
        self._palette_list.setMovement(QListView.Movement.Static)
        self._palette_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._palette_list.currentRowChanged.connect(self._on_color_selected)
        self._palette_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._palette_list.customContextMenuRequested.connect(self._on_palette_context)

        lay.addWidget(self._palette_list, stretch=1)
    
        return frame

    ###------------------------------------------ Editing SLots ------------------------------------------###

    def _on_editing_started(self) -> None:
        if self.img:
            self.history.push_change(self.img)

    def _on_painted(self) -> None:
        if self.img:
            self.history.push_change(self.img)
        self._update_dirty_state()

    def _on_palette_context(self, pos: QPoint) -> None:
        item = self._palette_list.itemAt(pos)
        if not item or not self.img:
            return

        idx = self._palette_list.row(item)
        current_rgb = self.img.color(idx)
        current_color = QColor.fromRgba(current_rgb)

        new_color = QColorDialog.getColor(
            initial=current_color,
            parent=self,
            title=f'Overwrite CLUT Color [{idx}]',
            options=QColorDialog.ColorDialogOption.DontUseNativeDialog
        )
        if new_color.isValid() and new_color != current_color:
            self.history.push_change(self.img)
            self.img.setColor(idx, new_color.rgba())
            self.history.push_change(self.img)
            pixmap = QPixmap(20, 20)
            pixmap.fill(new_color)
            item.setIcon(QIcon(pixmap))
            item.setToolTip(f'Index: {idx} (Hex: {new_color.name()})')
            self._image_label.update_display()
            self._update_dirty_state()

    def undo(self) -> None:
        '''Undo action and sync states'''
        prev_img = self.history.undo()
        if prev_img:
            self.img = prev_img
            self._apply_zoom()
            self._populate_palette()
            self._update_dirty_state()

    def redo(self) -> None:
        next_img = self.history.redo()
        if next_img:
            self.img = next_img
            self._apply_zoom()
            self._populate_palette()
            self._update_dirty_state()

    def _update_dirty_state(self) -> None:
        is_dirty = bool(self.history.undo_stack) or self.history.debounce_timer.isActive()
        self.set_dirty(is_dirty)

###----------------------------------- Contractuals ---------------------------------------------###

    def begin_loading(self, node: VfsNode) -> None:
        super().begin_loading(node)
        self.node = node
        self._image_label.setText(f'Loading {node.name}...')
        self._image_label.adjustSize()
        self._btn_export.setEnabled(False)

    def receive_data(self, result: FisEditorPayload, data_resolver: Callable[[VfsNode], bytes] | None = None) -> None:
        '''Override for recieving the FISEditorPayload'''
        self._data_resolver = data_resolver
        if isinstance(result, FisEditorPayload):
            self.img     = result.image
            self.info    = result.info
            self.raw_fis = result.raw_bytes
            self._populate_ui()
        else:
            self._image_label.setText(f'Error loading texture: Got {type(result)} expected "FisEditorPayload"')

    def _populate_ui(self, data: bytes = b'') -> None:
        if not self.img or not self.info:
            return
        self.history.initialize(self.img)
        self._populate_palette()
        self._display_image(self.img)
        self._populate_info(self.info)
        self._title_label.setText(
            f'{self.node.name}  —  {self.info.width}×{self.info.height}  {self.info.psm_name}'
        )
        self._btn_export.setEnabled(True)
        logger.debug(f'FIS: loaded {self.node.name} ({self.info.width}×{self.info.height} {self.info.psm_name})')

    def get_modified_data(self) -> Any:
        if not self.is_dirty() or not self.img or not self.raw_fis:
            return self._original_data
        return (self.img, self.raw_fis)

    def show_load_error(self, message: str) -> None:
        self._show_error(message)
        return 

###------------------------------------ Display Helpers ---------------------------------------------###

    def _display_image(self, img: QImage) -> None:
        '''Show the decoded image, scaled to fit without distortion.'''
        available = self._scroll_area.size() - QSize(4, 4)
        if available.width() > 50 and available.height() > 50:
            if img.width() > available.width() or img.height() > available.height():
                zoom_x = available.width() / img.width()
                zoom_y = available.height() / img.height()
                self._zoom_factor = min(zoom_x, zoom_y)
            else:
                self._zoom_factor = 1.0
        else:
            self._zoom_factor = 1.0
        self._apply_zoom()

    def _show_error(self, message: str) -> None:
        self._image_label.setPixmap(QPixmap())
        self._image_label.setText(message)
        self._image_label.adjustSize()
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

    def _populate_palette(self) -> None:
        '''Extracts the Qimage color table (CLUT) stored from the handler'''
        self._palette_list.clear()
        if not self.img:
            return
        colors = self.img.colorTable()
        for idx, rgb in enumerate(colors):
            color = QColor.fromRgba(rgb)
            pixmap = QPixmap(20, 20)
            pixmap.fill(color)

            item = QListWidgetItem()
            item.setIcon(QIcon(pixmap))
            item.setToolTip(f'Index: {idx} (Hex: {color.name()})')
            self._palette_list.addItem(item)

        if colors:
            self._palette_list.setCurrentRow(0)

    def _on_color_selected(self, index: int):
        '''Passes the selected CLUT index to the drawing canvas'''
        if isinstance(self._image_label, InteractiveCanvas):
            self._image_label.selected_color_idx = index

    def _apply_zoom(self):
        if isinstance(self._image_label, InteractiveCanvas):
            if self.img:
                self._image_label.image_ref = self.img
            self._image_label.zoom_factor = self._zoom_factor
            self._image_label.update_display()

    def _export_png(self) -> None:
        if not self.img or not self.node:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Export PNG', f'{self.node.name}.png', 'PNG Images (*.png)')
        if not path:
            return
        if not self.img.save(path, 'PNG'):
            logger.error(f'FIS: QImage.save() failed for {path}')
        else:
            logger.info(f'FIS: exported to {Path(path).name}')

    def eventFilter(self, source, event) -> bool:
        '''Intercept viewport and label events to handle middle-mouse panning'''
        if source in (self._scroll_area.viewport(), self._image_label):
            if event.type() == event.Type.MouseButtonPress: # Middle Mouse Event
                if event.button() == Qt.MouseButton.MiddleButton:
                    self._is_panning = True
                    self._pan_start_pos = event.globalPosition().toPoint()
                    self._pan_start_h_bar = self._scroll_area.horizontalScrollBar().value()
                    self._pan_start_v_bar = self._scroll_area.verticalScrollBar().value()
                    self._scroll_area.setCursor(Qt.CursorShape.ClosedHandCursor)
                    return True
            elif event.type() == event.Type.MouseMove: # Mouse Drag Event
                if self._is_panning:
                    delta = event.globalPosition().toPoint() - self._pan_start_pos
                    self._scroll_area.horizontalScrollBar().setValue(self._pan_start_h_bar - delta.x())
                    self._scroll_area.verticalScrollBar().setValue(self._pan_start_v_bar - delta.y())
                    return True
            elif event.type() == event.Type.MouseButtonRelease: # Release Middle Mouse Event
                if event.button() == Qt.MouseButton.MiddleButton and getattr(self, '_is_panning', False):
                    self._is_panning = False
                    self._scroll_area.unsetCursor()
                    return True
        return super().eventFilter(source, event)
    
    def wheelEvent(self, event):
        '''Ctrl+scroll to adjust image size'''
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self._zoom_in()
            elif delta < 0:
                self._zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    def _zoom_in(self):
        self._zoom_factor *= self._zoom_step
        if self._zoom_factor > self._max_zoom:
            self._zoom_factor = self._max_zoom
        self._apply_zoom()

    def _zoom_out(self):
        self._zoom_factor /= self._zoom_step
        if self._zoom_factor < self._min_zoom:
            self._zoom_factor = self._min_zoom
        self._apply_zoom()

