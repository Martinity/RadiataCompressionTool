"""TAC audio support for Radiata Stories audio streams."""

from __future__ import annotations
from os import name

import ctypes
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import wave
from typing import Any, NamedTuple

from core.node import VfsNode
from core.registry import Registry
from core.contracts import LeafHandler
from core.workers import ActionDef, ActionType
from core.native.native_registry import NativeLibrary, NativeRegistry

import logging
logger = logging.getLogger(f"radiata.{__name__}")

###----------------------------------------------- Constants -----------------------------------------------###

TAC_SAMPLE_RATE = 48000
TAC_CHANNELS = 2
TAC_FRAME_SAMPLES = 1024
TAC_BLOCK_SIZE = 0x4E000

TAC_PROCESS_OK = 0
TAC_PROCESS_NEXT_BLOCK = 1
TAC_PROCESS_DONE = 2


class TacError(RuntimeError):
    pass


###-------------------------------------------------- TAC info --------------------------------------------------###

@dataclass(frozen=True)
class TacInfo:
    huffman_offset: int
    unknown:        int
    loop_frame:     int
    loop_discard:   int
    frame_count:    int
    frame_last:     int
    loop_offset:    int
    file_size:      int
    joint_stereo:   int
    empty:          int

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
        ("unknown",        ctypes.c_uint32),
        ("loop_frame",     ctypes.c_uint16),
        ("loop_discard",   ctypes.c_uint16),
        ("frame_count",    ctypes.c_uint16),
        ("frame_last",     ctypes.c_uint16),
        ("loop_offset",    ctypes.c_uint32),
        ("file_size",      ctypes.c_uint32),
        ("joint_stereo",   ctypes.c_uint32),
        ("empty",          ctypes.c_uint32),
    ]

class TacEditorPayload(NamedTuple):
    '''Structured handler to payload result'''
    info:      TacInfo
    wav_bytes: bytes
    raw_bytes: bytes

###------------------------------------------------- TAC parsing / decoding ------------------------------------------------###

def parse_tac_header(data: bytes) -> TacInfo:
    if len(data) < 0x20:
        raise TacError("Buffer is too small for a TAC header")
    return TacInfo(
        huffman_offset=int.from_bytes(data[0x00:0x04], "little"),
        unknown=       int.from_bytes(data[0x04:0x08], "little"),
        loop_frame=    int.from_bytes(data[0x08:0x0A], "little"),
        loop_discard=  int.from_bytes(data[0x0A:0x0C], "little"),
        frame_count=   int.from_bytes(data[0x0C:0x0E], "little"),
        frame_last=    int.from_bytes(data[0x0E:0x10], "little"),
        loop_offset=   int.from_bytes(data[0x10:0x14], "little"),
        file_size=     int.from_bytes(data[0x14:0x18], "little"),
        joint_stereo=  int.from_bytes(data[0x18:0x1C], "little"),
        empty=         int.from_bytes(data[0x1C:0x20], "little"),
    )

###--------------------------------------------- Native Decoder ----------------------------------------------###

TAC_MODULE = NativeLibrary(
    name='tac_codec',
    root_dir=Path(__file__).resolve().parent.parent / 'native',
    sources=['tac_lib.c'],
    link_args=['-lm'],
)


def _bindings(lib: ctypes.CDLL) -> ctypes.CDLL:
    u8_ptr  = ctypes.POINTER(ctypes.c_uint8)
    s16_ptr = ctypes.POINTER(ctypes.c_int16)

    lib.tac_init.argtypes              = [u8_ptr, ctypes.c_int]
    lib.tac_init.restype               = ctypes.c_void_p
    lib.tac_get_header.argtypes        = [ctypes.c_void_p]
    lib.tac_get_header.restype         = ctypes.POINTER(TacHeader)
    lib.tac_reset.argtypes             = [ctypes.c_void_p]
    lib.tac_reset.restype              = None
    lib.tac_free.argtypes              = [ctypes.c_void_p]
    lib.tac_free.restype               = None
    lib.tac_decode_frame.argtypes      = [ctypes.c_void_p, u8_ptr]
    lib.tac_decode_frame.restype       = ctypes.c_int
    lib.tac_get_samples_pcm16.argtypes = [ctypes.c_void_p, s16_ptr]
    lib.tac_get_samples_pcm16.restype  = None
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


def decode_tac_to_wav(data: bytes) -> tuple[bytes, TacInfo]:
    lib = NativeRegistry.load(TAC_MODULE, _bindings)
    if not lib:
        raise TacError("Failed to load TAC decoder")

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

###--------------------------------------------- TAC Handler ----------------------------------------------------------###

@Registry.register(
    name='TAC Audio Handler',
    extensions=('.020',),
    supported_actions=(
        ActionDef('Properties', ActionType.DIALOG),
        ActionDef('Export as WAV', ActionType.EXPORT),
))
class TacHandler(LeafHandler):
    '''Leaf Handler for processing raw TAC audio streams'''
    def __init__(self, source: bytes, parent: VfsNode | None = None) -> None:
        super().__init__(source)
        self._raw = source

    def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> TacEditorPayload:
        wav_bytes, info = decode_tac_to_wav(raw_bytes)
        return TacEditorPayload(info=info, wav_bytes=wav_bytes, raw_bytes=raw_bytes)

    def execute_action(
        self,
        node:              VfsNode,
        action_name:       str,
        **kwargs,
    ) -> Any:
        if action_name == 'Properties':
            try:
                info: TacInfo = parse_tac_header(self._raw)
                loop = (
                    'none' if info.loop_sample is None
                    else f'sample {info.loop_sample}'
                )
                return (
                    f'Sample rate:    {TAC_SAMPLE_RATE} Hz\n'
                    f'Channels:       {TAC_CHANNELS}\n'
                    f'Frames:         {info.frame_count}\n'
                    f'Total samples:  {info.total_samples}\n'
                    f'Duration:       {info.duration_seconds:.2f}s\n'
                    f'Loop:           {loop}\n'
                    f'Joint stereo:   {info.joint_stereo}'
                )
            except Exception as e:
                return f'Parse error: {e}'
        if action_name == 'Export as WAV':
            file_path: Path | None = kwargs.get('file_path')
            if not file_path:
                return 'No output path provided'
            try:
                wav_bytes, info = decode_tac_to_wav(self._raw)
                file_path.write_bytes(wav_bytes)
                return f'Saved to {file_path.name}'
            except Exception as e:
                logger.error(f'TAC WAV export failed: {e}', exc_info=True)
                raise
        return None
