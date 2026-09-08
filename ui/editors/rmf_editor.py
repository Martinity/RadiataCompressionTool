'''RMF Editor
Early version that round-trips on text edits.

Current Limitations:
- Commands are not fully supported (parsed or mutable). I was planning on adding editing to the token table.
- Glyph data and tables are importable (i hope) but not yet parsed. In the case of glyph data a new handler is needed.
- Positional/Dimensional data is losely interpreted based on no tables parsed.
- The Canvas's aspect ratio is taken from my pcsx2 output, so may not match the actual aspect ratio.
- The timing data is also loosely interpreted. Since there is no manual advance yet I assume a max selection WaitSelect(2) time.
    WaitTime is also not calculated correctly.
'''
from __future__ import annotations

import bisect
import copy
from typing import Any
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QTableWidget, QTableWidgetItem, QLabel, QSplitter,
    QGroupBox, QFormLayout, QSpinBox, QComboBox, QSlider, QHeaderView, QSizePolicy, QTextEdit, QPushButton,
)
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QFont
from PyQt6.QtCore import Qt, QRect, QTimer

from core.contracts import BaseEditor
from core.registry import Registry
from core.handlers.rmf_leaf import (
    RmfHandler, RmfFile, RmfToken, RmfPacket, describe_token, MAX_GLYPHS_BUDGET,
    GlyphDraw
)
from core.asset_symbols import name_for
from utilities import svg_to_icon

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------------------- Canvas -------------------------------------------###

class MessageCanvas(QLabel):
    '''Canvas for displaying RMF message data with proper virtual-to-pixel coordinate mapping.'''
    VIRTUAL_PLANE_W = 640  # RMF positional plane, these may be consolidated
    VIRTUAL_PLANE_H = 448
    FORCED_RENDER_W = 796  # Screen simulation, remnants from a 16:9, 4:3 aspect ratio toggle
    FORCED_RENDER_H = 448
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.current_glyphs: list[tuple[str, float, float, float, float, QColor]] = []
        self.update_canvas()

    def _fit_canvas_rect(self) -> QRect:
        '''Render the canvas to the largest possible size while maintaining the aspect ratio.'''
        canvas_w, canvas_h = self.FORCED_RENDER_W, self.FORCED_RENDER_H
        avail_w = max(1, self.width())
        avail_h = max(1, self.height())
        scale = min(avail_w / canvas_w, avail_h / canvas_h)
        fit_w = max(1, int(canvas_w * scale))
        fit_h = max(1, int(canvas_h * scale))
        x = (avail_w - fit_w) // 2
        y = (avail_h - fit_h) // 2
        return QRect(x, y, fit_w, fit_h)

    def update_canvas(
        self,
        glyphs: list[tuple[str, float, float, float, float, QColor]] | None = None,
    ) -> None:
        if glyphs is not None:
            self.current_glyphs = glyphs

        avail_w = max(1, self.width())
        avail_h = max(1, self.height())
        # Paint only the virtual screen not the entire Qt Panel
        image = QImage(avail_w, avail_h, QImage.Format.Format_ARGB32)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        fit_rect = self._fit_canvas_rect()
        painter.fillRect(fit_rect, QColor(0, 0, 0))
        # Fit the virtual screen within the canvas
        scale_x = (self.FORCED_RENDER_W / self.VIRTUAL_PLANE_W) * (fit_rect.width() / self.FORCED_RENDER_W)
        scale_y = (self.FORCED_RENDER_H / self.VIRTUAL_PLANE_H) * (fit_rect.height() / self.FORCED_RENDER_H)
        font = QFont('Monospace')
        # Temporary middle calculation
        lines = {}
        for glyph in self.current_glyphs:
            py = glyph[2]
            lines.setdefault(py, []).append(glyph)
        for py, line_glyphs in lines.items():
            if not line_glyphs:
                continue
            min_px = min(g[1] for g in line_glyphs)
            max_px = max(g[1] + g[3] for g in line_glyphs)
            run_width = max_px - min_px
            center_offset_x = (self.VIRTUAL_PLANE_W - run_width) / 2.0 - min_px

            for char, px, py, cell_w, cell_h, color in self.current_glyphs:
                # Calculate draw position based on virtual screen coordinates
                draw_x = fit_rect.x() + int((px + self.VIRTUAL_PLANE_W / 2) * scale_x)
                draw_y = fit_rect.y() + int(py * scale_y)
                draw_w = int(cell_w * scale_x)
                draw_h = int(cell_h * scale_y)
                rect = QRect(draw_x, draw_y, max(1, draw_w), max(1, draw_h))
                # Debug: faint debug cell borders to verify alignment
                painter.setPen(QColor(255, 255, 255, 15))
                painter.drawRect(rect)

                font_size = max(1, int(draw_h * 0.75))
                font.setPixelSize(font_size)
                painter.setFont(font)
                painter.setPen(color)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, char)
        painter.end()
        self.setPixmap(QPixmap.fromImage(image))

    def resizeEvent(self, a0):
        '''Keep the canvas aspect ratio during window resize.'''
        super().resizeEvent(a0)
        self.update_canvas()

###------------------------------------------- Editor -------------------------------------------###

@Registry.register_editor(
    name="RMF Message Data Editor",
    handler=RmfHandler,
    extensions=(".rmf",),
)
class RmfEditor(BaseEditor):
    def __init__(self, parent: QWidget | None = None, data_resolver=None):
        super().__init__(parent, data_resolver)
        self.rmf_file:             RmfFile | None = None
        self._is_english:          bool = True
        self._data_resolver        = data_resolver
        self._timeline_states:     list[list[tuple[str, float, float, float, float, QColor]]] = []
        self._packet_start_frames: list[int] = []
        self._history:             list[RmfFile] = []
        self._history_index:       int = -1
        self.playback_timer        = QTimer()
        self.playback_timer.timeout.connect(self._advance_timeline)
        self._setup_ui()
        # Font encoding datas
        self._glyph_encoding_data: dict[tuple[int, ...], Any] = {}  # (hid, payload type)

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(self.splitter)

        # Right Panel
        instruction_widget = QWidget()
        inst_layout = QVBoxLayout(instruction_widget)
        inst_layout.setContentsMargins(4, 4, 4, 4)

        # Packet Selection
        packet_layout = QHBoxLayout()
        packet_layout.addWidget(QLabel('Select Packet:'))
        self.packet_combo = QComboBox()
        self.packet_combo.currentIndexChanged.connect(self._on_packet_selected)
        packet_layout.addWidget(self.packet_combo, stretch=1)
        inst_layout.addLayout(packet_layout)

        # Speaker table
        speaker_group = QGroupBox('Speaker Table')
        speaker_layout = QVBoxLayout()
        self.speaker_table = QTableWidget(0, 2)
        self.speaker_table.setHorizontalHeaderLabels(['Character ID', 'Variant ID'])
        self.speaker_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch) # type: ignore
        speaker_layout.addWidget(self.speaker_table)
        speaker_group.setLayout(speaker_layout)
        inst_layout.addWidget(speaker_group)

        # Message text
        text_group = QGroupBox('Message Text')
        text_layout = QVBoxLayout()
        self.text_editor = QTextEdit()
        self.text_editor.setMinimumHeight(120)
        text_layout.addWidget(self.text_editor)

        self.text_apply_status = QLabel('')
        self.text_apply_status.setWordWrap(True)
        text_layout.addWidget(self.text_apply_status)

        apply_row = QHBoxLayout()
        self.apply_text_button = QPushButton('Apply Text Changes')
        self.apply_text_button.clicked.connect(self._on_apply_text_clicked)
        apply_row.addWidget(self.apply_text_button)
        apply_row.addStretch()
        text_layout.addLayout(apply_row)

        text_group.setLayout(text_layout)
        inst_layout.addWidget(text_group)

        # Token stream
        stream_group = QGroupBox('Token Stream')
        stream_layout = QVBoxLayout()
        self.stream_list = QListWidget()
        stream_layout.addWidget(self.stream_list)
        stream_group.setLayout(stream_layout)
        inst_layout.addWidget(stream_group)

        # Left panel
        window_widget = QWidget()
        window_layout = QVBoxLayout(window_widget)
        window_layout.setContentsMargins(4, 4, 4, 4)

        # Preview window
        window_group = QGroupBox('Message Window')
        preview_layout = QVBoxLayout()
        self.screen_canvas = MessageCanvas()
        preview_layout.addWidget(self.screen_canvas, stretch=1)

        # Timeline controls
        timeline_layout = QHBoxLayout()
        self.play_button = QPushButton()
        self.play_button.setIcon(svg_to_icon('play'))
        self.play_button.clicked.connect(self._toggle_playback)
        self.time_slider = QSlider(Qt.Orientation.Horizontal)
        self.time_slider.setMinimum(0)
        self.time_slider.setMaximum(100)
        self.time_slider.valueChanged.connect(self._on_time_scrubbed)

        self.time_label = QLabel('Frame: 0')
        self.time_label.setMinimumWidth(80)

        timeline_layout.addWidget(self.play_button)
        timeline_layout.addWidget(self.time_slider)
        timeline_layout.addWidget(self.time_label)
        preview_layout.addLayout(timeline_layout)

        window_group.setLayout(preview_layout)
        window_layout.addWidget(window_group)

        self.splitter.addWidget(window_widget)
        self.splitter.addWidget(instruction_widget)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 6)

    def receive_data(self, result: RmfFile, data_resolver=None) -> None:
        if not isinstance(result, RmfFile):
            raise TypeError('RmfHandler.prepare_editor_data must return type RmfFile')
        self._data_resolver = data_resolver
        self.request_font_encoding_data()
        self.rmf_file = result
        self._is_english = result.is_english
        self._history = [copy.deepcopy(self.rmf_file)]
        self._history_index = 0
        self.undo_state_changed.emit(False, False)
        self.set_dirty(False)
        self._populate_ui(result)

    def current_data(self) -> RmfFile:
        if not isinstance(self.rmf_file, RmfFile):
            raise TypeError('No RmfFile loaded, yet save was requested.')
        return self.rmf_file

    def receive_request(self, payload: Any) -> None:
        pass

    def request_font_encoding_data(self) -> None:
        '''Dispatch workers to gather font encoding data from the data resolver.'''
        if not self._data_resolver:
            self.text_apply_status.setText('No data resolver available, falling back to defaults.')
            return
        REQUIRED_PAYLOADS = {
            (4, 0):     'Ank Font',
            (26, 0, 0): 'Playstation Glyphs',
            (26, 3, 0): 'Japanese Glyphs',
            (26, 2, 0): 'Japanese Accent Glyphs',  # This has a specific name I just don't know it
        }
        REQUIRED_RAW = {
            (26, 10, 0): 'English Font Encoding',
            (26, 11, 0): 'Japanese Font Encoding',
            (26, 22, 0): 'English Spacing Table',
            (26, 23, 0): 'Japanese Spacing Table'
        }
        # for hid in REQUIRED_PAYLOADS.keys():   # these require a new glyph render handler to process NotYetImplemented
        #     self._data_resolver.fetch_payload(hid, requester=self)
        for hid in REQUIRED_RAW.keys():
            self.request_raw_data(hid)

    def _populate_ui(self, data: RmfFile) -> None:
        if not data or not data.packets:
            return

        self._build_display_timeline(data)
        self.time_slider.blockSignals(True)
        self.time_slider.setMinimum(0)
        self.time_slider.setMaximum(max(0, len(self._timeline_states) - 1))
        self.time_slider.setValue(0)
        self.time_slider.blockSignals(False)

        self.packet_combo.blockSignals(True)
        self.packet_combo.clear()
        for i, packet in enumerate(data.packets):
            label = f'Packet {i:03d}' if packet is not None else f'Packet {i:03d} (absent)'
            self.packet_combo.addItem(label)
        self.packet_combo.blockSignals(False)

        self.packet_combo.setCurrentIndex(0)
        self._update_packet_ui(0)
        self._on_time_scrubbed(0)

    def _push_state(self) -> None:
        '''Takes a snapshot of the current rmf_file and adds it to the undo stack.'''
        if not self.rmf_file:
            raise TypeError('No RmfFile loaded, yet undo stack was mutated.')
        self._history = self._history[:self._history_index + 1]
        self._history.append(copy.deepcopy(self.rmf_file))
        self._history_index += 1
        self.set_dirty(True)
        self.undo_state_changed.emit(self._history_index > 0, False)

    def undo(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self.rmf_file = copy.deepcopy(self._history[self._history_index])
            self._populate_ui(self.rmf_file)
            self.set_dirty(self._history_index > 0)
            self.undo_state_changed.emit(self._history_index > 0, False)

    def redo(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self.rmf_file = copy.deepcopy(self._history[self._history_index])
            self._populate_ui(self.rmf_file)
            self.set_dirty(True)
            self.undo_state_changed.emit(True, self._history_index < len(self._history) - 1)

    def _toggle_playback(self) -> None:
        '''Toggles the playback state of the timeline.'''
        if self.playback_timer.isActive():
            self.playback_timer.stop()
            self.play_button.setIcon(svg_to_icon('play'))
        else:
            if self.time_slider.value() >= self.time_slider.maximum():
                self.time_slider.setValue(0)
            self.playback_timer.start(32) # 30fps
            self.play_button.setIcon(svg_to_icon('pause'))

    def _advance_timeline(self) -> None:
        '''Advance the time slider by one frame, stops if at the end.'''
        current_frame = self.time_slider.value()
        if current_frame < self.time_slider.maximum():
            self.time_slider.setValue(current_frame + 1)
        else:
            self.playback_timer.stop()
            self.play_button.setIcon(svg_to_icon('play'))

    def _get_packet_for_frame(self, frame: int) -> int:
        if not self._packet_start_frames:
            return 0
        return max(0, bisect.bisect_right(self._packet_start_frames, frame) - 1)

    def _update_packet_ui(self, index: int) -> None:
        if index < 0 or not self.rmf_file or index >= len(self.rmf_file.packets):
            return
        packet = self.rmf_file.packets[index]
        if packet is None:
            self.speaker_table.setRowCount(0)
            self.stream_list.clear()
            self.stream_list.addItem('(absent packet slot / no data)')
            self.text_editor.setPlainText('')
            self.text_apply_status.setText('')
            return
        # Populate speaker table
        self.speaker_table.setRowCount(len(packet.speakers))
        for row, speaker in enumerate(packet.speakers):
            char_name = name_for('character', speaker.character_id)
            self.speaker_table.setItem(row, 0, QTableWidgetItem(char_name if char_name else str(speaker.character_id)))
            self.speaker_table.setItem(row, 1, QTableWidgetItem(str(speaker.variant_id)))
        # Populate command table
        self.stream_list.clear()
        for token in packet.tokens:
            cmd_name = describe_token(token)
            if token.is_glyph:
                self.stream_list.addItem(f'{cmd_name}: {token.raw_word:04X}')
            else:
                hex_payload = token.command_bytes.hex().upper() if token.command_bytes else ''
                self.stream_list.addItem(f'Command: {cmd_name} ({token.raw_word:04X})[{hex_payload}]')
        self.text_editor.setPlainText(packet.get_display_text(self._is_english))
        self.text_apply_status.setText('')

    def _on_apply_text_clicked(self) -> None:
        '''Re-encode text back into packet.tokens'''
        index = self.packet_combo.currentIndex()
        if not self.rmf_file or index < 0 or index >= len(self.rmf_file.packets):
            return
        packet = self.rmf_file.packets[index]
        if packet is None:
            return
        text = self.text_editor.toPlainText()
        try:
            total_glyphs = packet.encode_text_mutation(
                text, glyph_budget=MAX_GLYPHS_BUDGET, is_english=self._is_english
            )
        except ValueError as e:
            self.text_apply_status.setText(f'Could not apply text edits: {str(e)}')
            return
        self._push_state()
        self._update_packet_ui(index)
        self._build_display_timeline(self.rmf_file)
        self.time_slider.blockSignals(True)
        self.time_slider.setMaximum(max(0, len(self._timeline_states) - 1))
        self.time_slider.blockSignals(False)
        self._on_time_scrubbed(self.time_slider.value())
        self.text_apply_status.setText(f'Applied. Glyph count: {total_glyphs}.')

    def _build_display_timeline(self, data: RmfFile) -> None:
        '''Populates the timeline states from the RMF file's render timeline.'''
        raw_timeline, self._packet_start_frames = data.build_render_timeline()
        self._timeline_states = [
            [(g.char, g.x, g.y, g.w, g.h, QColor(*g.color)) for g in frame]
            for frame in raw_timeline
        ]

    def _on_packet_selected(self, index: int) -> None:
        '''Updates the instruction view when a new packet is selected'''
        self._update_packet_ui(index)
        if self._packet_start_frames and index < len(self._packet_start_frames):
            start_frame = self._packet_start_frames[index]
            self.time_slider.blockSignals(True)
            self.time_slider.setValue(start_frame)
            self.time_slider.blockSignals(False)
            self._on_time_scrubbed(start_frame)

    def _on_time_scrubbed(self, value: int) -> None:
        '''Handles time scrubbing by updating the slider value and preview window'''
        self.time_label.setText(f'Frame: {value}')
        if 0 <= value < len(self._timeline_states):
            self.screen_canvas.update_canvas(glyphs=self._timeline_states[value])

        current_packet_idx = self._get_packet_for_frame(value)
        if self.packet_combo.currentIndex() != current_packet_idx:
            self.packet_combo.blockSignals(True)
            self.packet_combo.setCurrentIndex(current_packet_idx)
            self._update_packet_ui(current_packet_idx)
            self.packet_combo.blockSignals(False)
