"""TAC audio support for Radiata Stories audio streams."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import wave

from PyQt6.QtCore import QIODevice, QTimer, Qt
from PyQt6.QtGui import QPainter, QPen
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink
from PyQt6.QtWidgets import (
    QFileDialog,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
    QStyle,
    QStyleOptionSlider,
)

from core.contracts import BaseEditor
from core.node import VfsNode
from core.registry import Registry

import logging

logger = logging.getLogger(f"radiata.{__name__}")

TAC_SAMPLE_RATE = 48000
TAC_CHANNELS = 2
TAC_FRAME_SAMPLES = 1024
TAC_BLOCK_SIZE = 0x4E000

TAC_PROCESS_OK = 0
TAC_PROCESS_NEXT_BLOCK = 1
TAC_PROCESS_DONE = 2


class TacError(RuntimeError):
    pass


@dataclass(frozen=True)
class TacInfo:
    huffman_offset: int
    unknown: int
    loop_frame: int
    loop_discard: int
    frame_count: int
    frame_last: int
    loop_offset: int
    file_size: int
    joint_stereo: int
    empty: int

    @property
    def total_samples(self) -> int:
        if not self.frame_count:
            return 0
        return ((self.frame_count - 1) * TAC_FRAME_SAMPLES) + self.frame_last + 1

    @property
    def duration_seconds(self) -> float:
        return self.total_samples / TAC_SAMPLE_RATE if self.total_samples else 0.0

    @property
    def loop_sample(self) -> int | None:
        if not self.loop_frame:
            return None
        return ((self.loop_frame - 1) * TAC_FRAME_SAMPLES) + self.loop_discard


class TacHeader(ctypes.Structure):
    _fields_ = [
        ("huffman_offset", ctypes.c_uint32),
        ("unknown", ctypes.c_uint32),
        ("loop_frame", ctypes.c_uint16),
        ("loop_discard", ctypes.c_uint16),
        ("frame_count", ctypes.c_uint16),
        ("frame_last", ctypes.c_uint16),
        ("loop_offset", ctypes.c_uint32),
        ("file_size", ctypes.c_uint32),
        ("joint_stereo", ctypes.c_uint32),
        ("empty", ctypes.c_uint32),
    ]


def parse_tac_header(data: bytes) -> TacInfo:
    if len(data) < 0x20:
        raise TacError("Buffer is too small for a TAC header")
    return TacInfo(
        huffman_offset=int.from_bytes(data[0x00:0x04], "little"),
        unknown=int.from_bytes(data[0x04:0x08], "little"),
        loop_frame=int.from_bytes(data[0x08:0x0A], "little"),
        loop_discard=int.from_bytes(data[0x0A:0x0C], "little"),
        frame_count=int.from_bytes(data[0x0C:0x0E], "little"),
        frame_last=int.from_bytes(data[0x0E:0x10], "little"),
        loop_offset=int.from_bytes(data[0x10:0x14], "little"),
        file_size=int.from_bytes(data[0x14:0x18], "little"),
        joint_stereo=int.from_bytes(data[0x18:0x1C], "little"),
        empty=int.from_bytes(data[0x1C:0x20], "little"),
    )


def _native_root() -> Path:
    return Path(__file__).resolve().parents[1] / "tac"


def _source_map(root: Path) -> list[tuple[Path, str]]:
    return [
        (root / "tac_lib.c", "tac_lib.c"),
        (root / "tac_lib.h", "tac_lib.h"),
        (root / "tac_data.h", "tac_data.h"),
        (root / "tac_ops.h", "tac_ops.h"),
    ]


def _build_needed(dll_path: Path, sources: list[tuple[Path, str]], force: bool) -> bool:
    if force or not dll_path.exists():
        return True
    dll_mtime = dll_path.stat().st_mtime
    return any(src.stat().st_mtime > dll_mtime for src, _ in sources)


def ensure_native_decoder(force_rebuild: bool = False) -> Path:
    root = _native_root()
    build_dir = root / ".tac_build"
    dll_path = build_dir / "tac_codec.dll"
    sources = _source_map(root)

    for src, _ in sources:
        if not src.exists():
            raise TacError(f"Missing TAC decoder source: {src.name}")

    if not _build_needed(dll_path, sources, force_rebuild):
        return dll_path

    gcc = shutil.which("gcc")
    if not gcc:
        raise TacError(
            "gcc was not found on PATH. Install MinGW or run from a shell where gcc is available."
        )

    build_dir.mkdir(exist_ok=True)
    for src, dst_name in sources:
        shutil.copyfile(src, build_dir / dst_name)

    cmd = [
        gcc,
        "-shared",
        "-O2",
        "-std=c99",
        "-Wall",
        "-Wextra",
        "-static-libgcc",
        "-o",
        str(dll_path),
        str(build_dir / "tac_lib.c"),
        "-lm",
    ]
    result = subprocess.run(cmd, cwd=build_dir, text=True, capture_output=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise TacError(f"Failed to build TAC decoder DLL:\n{details}")

    return dll_path


def load_decoder(dll_path: Path) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(dll_path))
    u8_ptr = ctypes.POINTER(ctypes.c_uint8)
    s16_ptr = ctypes.POINTER(ctypes.c_int16)

    lib.tac_init.argtypes = [u8_ptr, ctypes.c_int]
    lib.tac_init.restype = ctypes.c_void_p
    lib.tac_get_header.argtypes = [ctypes.c_void_p]
    lib.tac_get_header.restype = ctypes.POINTER(TacHeader)
    lib.tac_reset.argtypes = [ctypes.c_void_p]
    lib.tac_reset.restype = None
    lib.tac_free.argtypes = [ctypes.c_void_p]
    lib.tac_free.restype = None
    lib.tac_decode_frame.argtypes = [ctypes.c_void_p, u8_ptr]
    lib.tac_decode_frame.restype = ctypes.c_int
    lib.tac_get_samples_pcm16.argtypes = [ctypes.c_void_p, s16_ptr]
    lib.tac_get_samples_pcm16.restype = None
    return lib


def _block_buffer(block: bytes):
    return (ctypes.c_uint8 * TAC_BLOCK_SIZE).from_buffer_copy(block)


def _read_block(data: bytes, offset: int) -> tuple[bytes, int]:
    block = data[offset : offset + TAC_BLOCK_SIZE]
    if not block:
        return b"", 0
    return block.ljust(TAC_BLOCK_SIZE, b"\x00"), len(block)


def _error_name(code: int) -> str:
    names = {
        -1: "header error",
        -2: "buffer too small",
        -3: "frame id mismatch",
        -4: "CRC mismatch",
        -5: "Huffman count mismatch",
        TAC_PROCESS_DONE: "done",
    }
    return names.get(code, f"decoder error {code}")


def decode_tac_to_wav(data: bytes, *, force_rebuild: bool = False) -> tuple[bytes, TacInfo]:
    dll_path = ensure_native_decoder(force_rebuild)
    lib = load_decoder(dll_path)

    first_block, first_size = _read_block(data, 0)
    if not first_block:
        raise TacError("TAC stream is empty")
    if first_size < TAC_BLOCK_SIZE:
        raise TacError(
            f"The first TAC block is incomplete. Need {TAC_BLOCK_SIZE} bytes, got {first_size}."
        )

    first_buf = _block_buffer(first_block)
    handle = lib.tac_init(first_buf, TAC_BLOCK_SIZE)
    if not handle:
        raise TacError("TAC header/init failed. Is this file a raw TAC stream?")

    try:
        c_header = lib.tac_get_header(handle).contents
        info = TacInfo(
            c_header.huffman_offset,
            c_header.unknown,
            c_header.loop_frame,
            c_header.loop_discard,
            c_header.frame_count,
            c_header.frame_last,
            c_header.loop_offset,
            c_header.file_size,
            c_header.joint_stereo,
            c_header.empty,
        )
        sample_buf = (ctypes.c_int16 * (TAC_FRAME_SAMPLES * TAC_CHANNELS))()
        block = first_buf
        block_offset = 0
        decoded_frames = 0
        wav_io = BytesIO()

        with wave.open(wav_io, "wb") as wav:
            wav.setnchannels(TAC_CHANNELS)
            wav.setsampwidth(2)
            wav.setframerate(TAC_SAMPLE_RATE)

            while decoded_frames < info.frame_count:
                status = lib.tac_decode_frame(handle, block)
                if status == TAC_PROCESS_NEXT_BLOCK:
                    block_offset += TAC_BLOCK_SIZE
                    next_block, _size = _read_block(data, block_offset)
                    if not next_block:
                        raise TacError(
                            f"Decoder requested another block at 0x{block_offset:X}, but the file ended."
                        )
                    block = _block_buffer(next_block)
                    continue
                if status != TAC_PROCESS_OK:
                    raise TacError(
                        f"TAC decode failed at frame {decoded_frames + 1}: {_error_name(status)}"
                    )

                lib.tac_get_samples_pcm16(handle, sample_buf)
                decoded_frames += 1
                frame_samples = TAC_FRAME_SAMPLES
                if decoded_frames == info.frame_count:
                    frame_samples = info.frame_last + 1
                frame_bytes = frame_samples * TAC_CHANNELS * 2
                wav.writeframesraw(ctypes.string_at(sample_buf, frame_bytes))

        return wav_io.getvalue(), info
    finally:
        lib.tac_free(handle)


class LoopSeekSlider(QSlider):
    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._loop_position = -1

    def set_loop_position(self, position: int | None) -> None:
        self._loop_position = -1 if position is None else position
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
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

    def writeData(self, data: bytes) -> int:
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


@Registry.register(name="TAC Audio Viewer", extensions=(".020",), categories=("TAC Audio",))
class TacAudioEditor(BaseEditor):
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
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_toolbar())
        root.addWidget(self._build_info_panel())
        root.addStretch()

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("EditorToolbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 5, 10, 5)

        self._title_label = QLabel("TAC Audio")
        self._btn_export = QPushButton("Export WAV")
        self._btn_rebuild = QPushButton("Rebuild Decoder")

        self._btn_export.clicked.connect(self._export_wav)
        self._btn_rebuild.clicked.connect(lambda: self._decode(force_rebuild=True))

        lay.addWidget(self._title_label)
        lay.addStretch()
        lay.addWidget(self._btn_export)
        lay.addWidget(self._btn_rebuild)
        return bar

    def _build_info_panel(self) -> QFrame:
        frame = QFrame()
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self._btn_play_pause = QPushButton("▶")
        self._btn_play_pause.setFixedSize(34, 28)
        self._btn_play_pause.setToolTip("Play/Pause")
        self._btn_play_pause.clicked.connect(self._toggle_playback)

        self._time_label = QLabel("0:00 / 0:00")
        self._time_label.setMinimumWidth(88)
        self._seek_slider = LoopSeekSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.sliderPressed.connect(self._begin_seek)
        self._seek_slider.sliderReleased.connect(self._finish_seek)
        self._seek_slider.sliderMoved.connect(self._preview_seek)

        self._loop_checkbox = QCheckBox("Loop")
        self._loop_checkbox.setEnabled(False)
        self._loop_checkbox.toggled.connect(self._on_loop_toggled)

        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(75)
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
            "Sample rate",
            "Channels",
            "Frames",
            "Samples",
            "Duration",
            "Loop",
            "File size",
            "Huffman offset",
            "Joint stereo",
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(f"{key}:"))
            value = QLabel("-")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(value, stretch=1)
            layout.addLayout(row)
            self._info_rows[key] = value
        return frame

    def load_node(self, node: VfsNode, data: bytes) -> None:
        super().load_node(node, data)
        self._stop_playback()
        self._raw = data
        self._wav = None
        self._pcm = None
        self._audio_device = None
        self._info = None
        self._title_label.setText(f"{node.name} - TAC Audio")
        self._set_buttons(False)
        self._seek_slider.setRange(0, 0)
        self._seek_slider.set_loop_position(None)
        self._time_label.setText("0:00 / 0:00")
        self._btn_play_pause.setText("▶")
        self._loop_checkbox.setChecked(False)
        self._loop_checkbox.setEnabled(False)
        self._loop_checkbox.setVisible(False)

        try:
            self._info = parse_tac_header(data)
            self._populate_info(self._info)
            has_loop = self._info.loop_sample is not None
            self._loop_checkbox.setEnabled(has_loop)
            self._loop_checkbox.setVisible(has_loop)
            self._seek_slider.setRange(0, self._duration_ms())
            self._seek_slider.set_loop_position(self._loop_position_ms())
            self._update_time_label(0, self._duration_ms())
            self._set_buttons(True)
        except Exception as exc:
            logger.warning(f"TAC header parse failed for {node.name}: {exc}")

    def get_modified_data(self) -> bytes:
        return self._raw

    def _set_buttons(self, enabled: bool) -> None:
        self._btn_export.setEnabled(enabled)
        self._btn_play_pause.setEnabled(enabled)
        self._btn_rebuild.setEnabled(enabled)

    def _populate_info(self, info: TacInfo) -> None:
        loop = "none" if info.loop_sample is None else f"sample {info.loop_sample}"
        self._info_rows["Sample rate"].setText(f"{TAC_SAMPLE_RATE} Hz")
        self._info_rows["Channels"].setText(str(TAC_CHANNELS))
        self._info_rows["Frames"].setText(str(info.frame_count))
        self._info_rows["Samples"].setText(str(info.total_samples))
        self._info_rows["Duration"].setText(f"{info.duration_seconds:.2f}s")
        self._info_rows["Loop"].setText(loop)
        self._info_rows["File size"].setText(hex(info.file_size))
        self._info_rows["Huffman offset"].setText(hex(info.huffman_offset))
        self._info_rows["Joint stereo"].setText(str(info.joint_stereo))

    def _decode(self, *, force_rebuild: bool = False) -> bytes | None:
        try:
            self._wav, info = decode_tac_to_wav(self._raw, force_rebuild=force_rebuild)
            self._info = info
            self._populate_info(info)
            has_loop = info.loop_sample is not None
            self._loop_checkbox.setEnabled(has_loop)
            self._loop_checkbox.setVisible(has_loop)
            self._load_pcm_from_wav(self._wav)
            self._seek_slider.setRange(0, self._duration_ms())
            self._seek_slider.set_loop_position(self._loop_position_ms())
            self._update_time_label(0, self._duration_ms())
            return self._wav
        except Exception as exc:
            QMessageBox.warning(self, "TAC decode failed", str(exc))
            logger.error(f"TAC decode failed: {exc}", exc_info=True)
            return None

    def _export_wav(self) -> None:
        node_name = self.current_node.name if self.current_node else "audio"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export WAV", f"{node_name}.wav", "WAV Audio (*.wav)"
        )
        if not path:
            return
        wav_data = self._wav or self._decode()
        if not wav_data:
            return
        Path(path).write_bytes(wav_data)
        logger.info(f"TAC exported WAV: {path}")

    def _toggle_playback(self) -> None:
        if not self._pcm and not self._decode():
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

    def _load_pcm_from_wav(self, wav_data: bytes) -> None:
        with wave.open(BytesIO(wav_data), "rb") as src:
            channels = src.getnchannels()
            sample_width = src.getsampwidth()
            sample_rate = src.getframerate()
            frame_count = src.getnframes()
            pcm = src.readframes(frame_count)

        if channels != TAC_CHANNELS or sample_width != 2 or sample_rate != TAC_SAMPLE_RATE:
            raise TacError(
                f"Unexpected decoded PCM format: {channels}ch, {sample_width * 8}-bit, {sample_rate} Hz"
            )

        self._pcm = pcm
        self._audio_device = PcmLoopDevice(
            pcm,
            sample_rate,
            channels,
            sample_width,
            self._info.loop_sample if self._info else None,
            self,
        )
        self._audio_device.set_loop_enabled(self._loop_checkbox.isChecked())

    def _start_playback(self) -> None:
        if not self._audio_device:
            return
        if not self._audio_device.isOpen():
            self._audio_device.open(QIODevice.OpenModeFlag.ReadOnly)
        if not self._audio_sink:
            self._audio_sink = QAudioSink(self._audio_format(), self)
            self._audio_sink.setVolume(self._volume_slider.value() / 100)
            self._audio_sink.stateChanged.connect(self._on_audio_state_changed)
        self._audio_sink.start(self._audio_device)
        self._is_playing = True
        self._btn_play_pause.setText("⏸")
        self._playback_timer.start()

    def _pause_playback(self) -> None:
        if self._audio_sink:
            self._audio_sink.suspend()
        self._is_playing = False
        self._btn_play_pause.setText("▶")
        self._playback_timer.stop()

    def _stop_playback(self) -> None:
        self._playback_timer.stop()
        if self._audio_sink:
            self._audio_sink.stop()
            self._audio_sink.deleteLater()
            self._audio_sink = None
        if self._audio_device and self._audio_device.isOpen():
            self._audio_device.close()
        self._is_playing = False
        if hasattr(self, "_btn_play_pause"):
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
        if self._audio_sink:
            self._audio_sink.setVolume(value / 100)

    def _on_audio_state_changed(self, state: QAudio.State) -> None:
        if state != QAudio.State.IdleState:
            return
        if not self._is_playing or not self._audio_device:
            return
        if self._loop_checkbox.isChecked():
            self._audio_device.set_loop_enabled(True)
            self._audio_sink.start(self._audio_device)
            return
        self._pause_playback()

    def _audio_format(self) -> QAudioFormat:
        audio_format = QAudioFormat()
        audio_format.setSampleRate(TAC_SAMPLE_RATE)
        audio_format.setChannelCount(TAC_CHANNELS)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        return audio_format

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

    def closeEvent(self, event) -> None:
        self._stop_playback()
        super().closeEvent(event)


def _format_ms(value: int) -> str:
    seconds = max(0, value // 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
