'''GUI for TAC audio players'''
from __future__ import annotations

import wave
from pathlib import Path
from io import BytesIO
from typing import Any
from PyQt6.QtCore import QTimer, Qt, QIODevice, QSettings
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QLabel, QHBoxLayout, QPushButton, 
    QCheckBox, QSlider, QMessageBox, QFileDialog, QStyle, QStyleOptionSlider,
    QStackedLayout,
)
from PyQt6.QtGui import QPainter, QPen
from PyQt6.QtMultimedia import QAudioSink, QAudioFormat
from core.contracts import BaseViewer
from core.node import VfsNode
from core.registry import Registry
from core.handlers.tac_leaf import TacEditorPayload, TacHandler, TacInfo, decode_tac_to_wav

import logging
logger = logging.getLogger(f'radiata.{__name__}')


TAC_SAMPLE_RATE = 48000
TAC_CHANNELS = 2
TAC_FRAME_SAMPLES = 1024
TAC_BLOCK_SIZE = 0x4E000

TAC_PROCESS_OK = 0
TAC_PROCESS_NEXT_BLOCK = 1
TAC_PROCESS_DONE = 2

class TacError(RuntimeError):
    pass

###----------------------------------------- Editor ------------------------------------------###

@Registry.register_editor(
    name='TAC Audio Viewer',
    handler=TacHandler,
    extensions=('.020',)
)
class TacAudioEditor(BaseViewer):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._raw: bytes = b""
        self._wav: bytes | None = None
        self._pcm: bytes | None = None
        self._audio_device: PcmLoopDevice | None = None
        self._audio_sink: QAudioSink | None = None
        self._info: TacInfo | None = None
        self._seeking = False
        self._is_playing = False
        self._playback_timer = QTimer(self)
        self._playback_timer.setInterval(50)
        self._playback_timer.timeout.connect(self._sync_playback_ui)
        self._setup_ui()

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
        editor_layout.addWidget(self._build_toolbar())
        editor_layout.addWidget(self._build_info_panel())
        editor_layout.addStretch()

        self._stack.addWidget(self._status_label)
        self._stack.addWidget(editor_widget)
 
    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("EditorToolbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 5, 10, 5)
 
        self._btn_export  = QPushButton("Export WAV")
        self._btn_export.setEnabled(False)
        self._btn_export.clicked.connect(self._export_wav)
 
        lay.addStretch()
        lay.addWidget(self._btn_export)
        return bar
 
    def _build_info_panel(self) -> QFrame:
        frame  = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
 
        controls = QHBoxLayout()
        controls.setSpacing(8)
 
        self._btn_play_pause = QPushButton("▶")
        self._btn_play_pause.setFixedSize(34, 28)
        self._btn_play_pause.setToolTip("Play/Pause")
        self._btn_play_pause.setEnabled(False)
        self._btn_play_pause.clicked.connect(self._toggle_playback)
 
        self._time_label  = QLabel("0:00 / 0:00")
        self._time_label.setMinimumWidth(88)
 
        self._seek_slider = LoopSeekSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.sliderPressed.connect(self._begin_seek)
        self._seek_slider.sliderReleased.connect(self._finish_seek)
        self._seek_slider.sliderMoved.connect(self._preview_seek)
 
        self._loop_checkbox = QCheckBox("Loop")
        self._loop_checkbox.setEnabled(False)
        self._loop_checkbox.setVisible(False)
        self._loop_checkbox.toggled.connect(self._on_loop_toggled)
 
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        settings = QSettings('RadiataModding', 'Tool')
        saved_volume = settings.value('volume', 75, type=int)
        self._volume_slider.setValue(saved_volume)
        self._volume_slider.setFixedWidth(120)
        self._volume_slider.valueChanged.connect(self._set_volume)
 
        controls.addWidget(self._btn_play_pause)
        controls.addWidget(self._seek_slider, stretch=1)
        controls.addWidget(self._time_label)
        controls.addWidget(self._loop_checkbox)
        controls.addWidget(QLabel("Volume:"))
        controls.addWidget(self._volume_slider)
        layout.addLayout(controls)
 
        self._info_rows: dict[str, QLabel] = {}
        for key in (
            "Sample rate", "Channels", "Frames", "Samples",
            "Duration", "Loop", "Huffman offset", "Joint stereo",
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{key}:"))
            value = QLabel("-")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(value, stretch=1)
            layout.addLayout(row)
            self._info_rows[key] = value
 
        return frame

    def begin_loading(self, node: VfsNode) -> None:
        super().begin_loading(node)
        self._stop_playback()
        self._reset_state()

        self._status_label.setText(f'Loading {node.name}...')
        self._stack.setCurrentIndex(0)
        self._set_controls_enabled(False)

    def receive_data(self, result: Any, data_resolver=None) -> None:
        self._data_resolver = data_resolver
        if not isinstance(result, TacEditorPayload):
            self.show_load_error(
                f'Unexpected payload type: {type(result).__name__} '
                f'(expected TacEditorPayload)'
            )
            return
        self._raw = result.raw_bytes
        self._wav = result.wav_bytes
        self._info = result.info

        try:
            self._load_pcm_from_wav(self._wav)
        except Exception as e:
            self.show_load_error(f'PCM load failed: {e}')
            logger.error(f'TacAudioEditor PCM load failed: {e}', exc_info=True)
            return
        
        self._populate_info(result.info)
        has_loop: bool = result.info.loop_sample is not None
        self._loop_checkbox.setEnabled(has_loop)
        self._loop_checkbox.setVisible(has_loop)
        self._seek_slider.setRange(0, self._duration_ms())
        self._seek_slider.set_loop_position(self._loop_position_ms())
        self._update_time_label(0, self._duration_ms())
        self._stack.setCurrentIndex(1)
        self._set_controls_enabled(True)
        logger.debug(
            f'TacAudioEditor: loaded {result.info.frame_count} frames '
            f'({result.info.duration_seconds:.2f}s)'
        )

    def show_load_error(self, message: str) -> None:
        self._status_label.setText(f'Load failed:\n{message}')
        self._stack.setCurrentIndex(0)
        self._set_controls_enabled(False)
        logger.error(f'TacAudioEditor: {message}')

    def _populate_ui(self, data: bytes) -> None:
        '''Not used recieve data handles everything for this editor'''

    def _reset_state(self) -> None:
        self._raw          = b""
        self._wav          = None
        self._pcm          = None
        self._audio_device = None
        self._info         = None
        self._seek_slider.setRange(0, 0)
        self._seek_slider.set_loop_position(None)
        self._time_label.setText("0:00 / 0:00")
        self._btn_play_pause.setText("▶")
        self._loop_checkbox.setChecked(False)
        self._loop_checkbox.setEnabled(False)
        self._loop_checkbox.setVisible(False)
        for lbl in self._info_rows.values():
            lbl.setText("-")
    
    def _set_controls_enabled(self, enabled: bool) -> None:
        self._btn_export.setEnabled(enabled)
        self._btn_play_pause.setEnabled(enabled)

    def _populate_info(self, info: TacInfo) -> None:
        loop = "none" if info.loop_sample is None else f"sample {info.loop_sample}"
        self._info_rows["Sample rate"].setText(f"{TAC_SAMPLE_RATE} Hz")
        self._info_rows["Channels"].setText(str(TAC_CHANNELS))
        self._info_rows["Frames"].setText(str(info.frame_count))
        self._info_rows["Samples"].setText(str(info.total_samples))
        self._info_rows["Duration"].setText(f"{info.duration_seconds:.2f}s")
        self._info_rows["Loop"].setText(loop)
        self._info_rows["Huffman offset"].setText(hex(info.huffman_offset))
        self._info_rows["Joint stereo"].setText(str(info.joint_stereo))

    def _export_wav(self) -> None:
        node_name = self.current_node.name if self.current_node else "audio"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export WAV", f"{node_name}.wav", "WAV Audio (*.wav)"
        )
        if not path:
            return
        wav_data = self._wav
        if not wav_data:
            try:
                wav_data, _ = decode_tac_to_wav(self._raw)
            except Exception as e:
                QMessageBox.warning(self, "Export failed", str(e))
                return
        Path(path).write_bytes(wav_data)
        logger.info(f"TAC exported WAV: {path}")

    def _load_pcm_from_wav(self, wav_data: bytes) -> None:
        with wave.open(BytesIO(wav_data), "rb") as src:
            channels     = src.getnchannels()
            sample_width = src.getsampwidth()
            sample_rate  = src.getframerate()
            frame_count  = src.getnframes()
            pcm          = src.readframes(frame_count)
 
        if channels != TAC_CHANNELS or sample_width != 2 or sample_rate != TAC_SAMPLE_RATE:
            raise TacError(
                f"Unexpected PCM format: {channels}ch, "
                f"{sample_width * 8}-bit, {sample_rate} Hz"
            )
 
        self._pcm          = pcm
        self._audio_device = PcmLoopDevice(
            pcm, sample_rate, channels, sample_width,
            self._info.loop_sample if self._info else None,
            self,
        )
        self._audio_device.set_loop_enabled(self._loop_checkbox.isChecked())

    def _toggle_playback(self) -> None:
        if not self._pcm:
            return
        if self._is_playing:
            self._pause_playback()
            return
        if self._audio_device and self._audio_device.at_end():
            self._audio_device.set_position_ms(0)
        self._start_playback()

    def _on_loop_toggled(self, checked: bool) -> None:
        if self._audio_device:
            self._audio_device.set_loop_enabled(checked)
 
    def _begin_seek(self) -> None:
        self._seeking = True
 
    def _preview_seek(self, position: int) -> None:
        self._update_time_label(position, self._duration_ms())
 
    def _finish_seek(self) -> None:
        self._seeking = False
        if self._audio_device:
            self._audio_device.set_position_ms(self._seek_slider.value())
            self._sync_playback_ui()
 
    def _start_playback(self) -> None:
        if not self._audio_device:
            return
        if not self._audio_device.isOpen():
            self._audio_device.open(QIODevice.OpenModeFlag.ReadOnly)
        if not self._audio_sink:
            self._audio_sink = QAudioSink(self._audio_format(), self)
            self._audio_sink.setVolume(self._volume_slider.value() / 100)
        self._audio_sink.start(self._audio_device)
        self._is_playing = True
        self._btn_play_pause.setText("⏸")
        self._playback_timer.start()
 
    def _pause_playback(self) -> None:
        if self._audio_sink:
            self._audio_sink.stop()
            self._audio_sink.deleteLater()
            self._audio_sink = None
        self._is_playing = False
        self._btn_play_pause.setText('▶')
        self._playback_timer.stop()
 
    def _stop_playback(self) -> None:
        self._playback_timer.stop()
        if self._audio_sink:
            self._audio_sink.stop()
            self._audio_sink.deleteLater()
            self._audio_sink = None
        if self._audio_device and self._audio_device.isOpen():
            self._audio_device.close()
            self._audio_device.set_position_ms(0)
        self._is_playing = False
        self._btn_play_pause.setText("▶")
 
    def _sync_playback_ui(self) -> None:
        if not self._audio_device:
            return
        position = self._audio_device.position_ms()
        if not self._seeking:
            self._seek_slider.setValue(min(position, self._duration_ms()))
        self._update_time_label(position, self._duration_ms())
        if self._audio_device.at_end() and not self._loop_checkbox.isChecked():
            self._pause_playback()
 
    def _set_volume(self, value: int) -> None:
        settings = QSettings('RadiataModding', 'Tool')
        settings.setValue('volume', value)
        if self._audio_sink:
            self._audio_sink.setVolume(value / 100)
 
    def _audio_format(self) -> QAudioFormat:
        fmt = QAudioFormat()
        fmt.setSampleRate(TAC_SAMPLE_RATE)
        fmt.setChannelCount(TAC_CHANNELS)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        return fmt
 
    def _duration_ms(self) -> int:
        if self._audio_device:
            return self._audio_device.duration_ms()
        if self._info:
            return round(self._info.duration_seconds * 1000)
        return 0
 
    def _loop_position_ms(self) -> int | None:
        if not self._info or self._info.loop_sample is None:
            return None
        return round((self._info.loop_sample / TAC_SAMPLE_RATE) * 1000)
 
    def _update_time_label(self, position: int, duration: int) -> None:
        self._time_label.setText(f"{_format_ms(position)} / {_format_ms(duration)}")
 
    def cleanup(self) -> None:
        self._stop_playback()
        super().cleanup()
 
    def closeEvent(self, a0) -> None:
        self._stop_playback()
        super().closeEvent(a0)

###------------------------------------------ Playback Widgets --------------------------------------------###

class LoopSeekSlider(QSlider):
    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._loop_position = -1

    def set_loop_position(self, position: int | None) -> None:
        self._loop_position = -1 if position is None else position
        self.update()

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if self._loop_position < self.minimum() or self._loop_position > self.maximum():
            return

        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )
        if not groove.isValid() or self.maximum() <= self.minimum():
            return

        ratio = (self._loop_position - self.minimum()) / (self.maximum() - self.minimum())
        x = groove.left() + round(ratio * groove.width())
        painter = QPainter(self)
        painter.setPen(QPen(Qt.GlobalColor.yellow, 2))
        painter.drawLine(x, groove.top() - 5, x, groove.bottom() + 5)


class PcmLoopDevice(QIODevice):
    def __init__(
        self,
        pcm: bytes,
        sample_rate: int,
        channels: int,
        sample_width: int,
        loop_sample: int | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pcm = pcm
        self._sample_rate = sample_rate
        self._frame_size = channels * sample_width
        self._position = 0
        self._loop_enabled = False
        self._loop_byte = -1
        if loop_sample is not None:
            self._loop_byte = self._align_byte(loop_sample * self._frame_size)

    def set_loop_enabled(self, enabled: bool) -> None:
        self._loop_enabled = enabled and 0 <= self._loop_byte < len(self._pcm)
        if self._loop_enabled and self._position >= len(self._pcm):
            self._position = self._loop_byte

    def set_position_ms(self, position: int) -> None:
        byte_pos = round((max(0, position) / 1000) * self._sample_rate) * self._frame_size
        self._position = min(self._align_byte(byte_pos), len(self._pcm))

    def position_ms(self) -> int:
        if not self._frame_size:
            return 0
        frame = self._position // self._frame_size
        return round((frame / self._sample_rate) * 1000)

    def duration_ms(self) -> int:
        if not self._frame_size:
            return 0
        frame_count = len(self._pcm) // self._frame_size
        return round((frame_count / self._sample_rate) * 1000)

    def at_end(self) -> bool:
        return (not self._loop_enabled) and self._position >= len(self._pcm)

    def readData(self, maxlen: int) -> bytes:
        if maxlen <= 0 or not self._pcm:
            return b""

        out = bytearray()
        while len(out) < maxlen:
            if self._position >= len(self._pcm):
                if self._loop_enabled:
                    self._position = self._loop_byte
                else:
                    break

            available = min(maxlen - len(out), len(self._pcm) - self._position)
            out.extend(self._pcm[self._position : self._position + available])
            self._position += available

        return bytes(out)

    def writeData(self, a0) -> int:
        return -1

    def bytesAvailable(self) -> int:
        if self._loop_enabled:
            return len(self._pcm) + super().bytesAvailable()
        remaining = max(0, len(self._pcm) - self._position)
        return remaining + super().bytesAvailable()

    def isSequential(self) -> bool:
        return True

    def _align_byte(self, value: int) -> int:
        if self._frame_size <= 0:
            return value
        return value - (value % self._frame_size)

###-------------------------------------- Utility --------------------------------------------###

def _format_ms(value: int) -> str:
    seconds = max(0, value // 1000)
    minutes, seconds = divmod(seconds, 60)
    hours,   minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
