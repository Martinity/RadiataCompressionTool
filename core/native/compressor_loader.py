"""On-demand native build + ctypes binding for the SLZ/SLE compressor.

Resolves the native compressor in three tiers: a prebuilt library shipped with the
app, otherwise the C source compiled on first use with the system compiler,
otherwise None. Any failure (missing compiler, build error, load error) is
reported once and returns None, so callers fall back to the pure-Python compressor
in RadiCompressor.
"""

from __future__ import annotations

import ctypes
from pathlib import Path
import threading
from core.native.native_registry import NativeRegistry, NativeLibrary

import logging
logger = logging.getLogger(f"radiata.{__name__}")

_lib: ctypes.CDLL | None = None
_load_attempted = False
_lock = threading.Lock()


###-------------------------------------------- C-Types Bindings ---------------------------------------------###

_LIBRARY_DEF = NativeLibrary(
    name="radiata_compressor",
    root_dir=Path(__file__).resolve().parent,
    sources=['radiata_compressor.c'],
)

def _bindings(lib: ctypes.CDLL) -> ctypes.CDLL:
    u8_ptr = ctypes.POINTER(ctypes.c_uint8)

    lib.radiata_decompress.argtypes = [
        u8_ptr, ctypes.c_size_t,
        u8_ptr, ctypes.c_size_t,
        ctypes.c_int,
    ]
    lib.radiata_decompress.restype = ctypes.c_int64

    lib.radiata_compress.argtypes = [
        u8_ptr, ctypes.c_size_t,
        u8_ptr, ctypes.c_size_t,
        ctypes.c_int,
    ]
    lib.radiata_compress.restype = ctypes.c_int64

    lib.sle_unscramble.argtypes = [u8_ptr, ctypes.c_size_t]
    lib.sle_unscramble.restype = None
    lib.sle_scramble.argtypes = [u8_ptr, ctypes.c_size_t]
    lib.sle_scramble.restype = None

    return lib

def get_compressor() -> ctypes.CDLL | None:
    """Return the loaded native compressor, or None if it is unavailable.

    Result is cached; resolution is attempted only once per process.
    """
    global _lib, _load_attempted
    if _load_attempted:
        return _lib
    with _lock:
        if _load_attempted:
            return _lib
        _load_attempted = True
        _lib = NativeRegistry.load(_LIBRARY_DEF, _bindings)
        if _lib is None:
            logger.warning("Using pure-Python SLZ/SLE compressor (native unavailable).")
        return _lib

# def _load_native() -> ctypes.CDLL | None:
#     """Resolve the native compressor across three tiers: prebuilt -> compile -> none."""
#     # Tier 1: a CI-built lib bundled with the app (zero setup, no compiler).
#     for cand in _prebuilt_lib_paths():
#         if cand.exists():
#             try:
#                 lib = _bind(cand)
#                 logger.info("Native SLZ/SLE compressor loaded (prebuilt): %s", cand)
#                 return lib
#             except Exception as exc:  # noqa: BLE001 - try the next candidate/tier
#                 logger.warning("Prebuilt compressor %s failed to load: %s", cand.name, exc)

#     # Tier 2: compile from the shipped source (developers running from source).
#     # Skipped in a frozen bundle, which has no compiler and a read-only payload.
#     if not getattr(sys, "frozen", False):
#         try:
#             library_path = _ensure_built()
#             lib = _bind(library_path)
#             logger.info("Native SLZ/SLE compressor compiled and loaded: %s", library_path.name)
#             return lib
#         except Exception as exc:  # noqa: BLE001 - fall through to Python
#             logger.warning("Could not build native compressor: %s", exc)

#     # Tier 3: caller falls back to the pure-Python compressor.
#     return None

###----------------------------------------- High-level wrappers -----------------------------------------###

def native_decompress(payload, expected_size: int, mode: int) -> bytes | None:
    """Decompress an SLZ payload (bytes after the 16-byte header).

    Returns the decompressed bytes (truncated to the produced length), or None
    if the native compressor is unavailable so the caller can fall back to Python.
    """
    lib = get_compressor()
    if lib is None or mode not in (1, 2, 3):
        return None

    src = bytes(payload)
    src_buf = (ctypes.c_uint8 * len(src)).from_buffer_copy(src) if src else (ctypes.c_uint8 * 0)()
    dst_cap = max(expected_size, 0)
    dst_buf = (ctypes.c_uint8 * dst_cap)() if dst_cap else (ctypes.c_uint8 * 0)()

    written = lib.radiata_decompress(src_buf, len(src), dst_buf, dst_cap, mode)
    if written < 0:
        return None
    return bytes(dst_buf[:written])


def native_compress(data, mode: int) -> bytes | None:
    """Compress already-padded input into an SLZ token stream (no header).

    `data` must be word-align-padded by the caller for mode 3, matching the
    Python loop. Returns the token-stream bytes, or None if the native compressor
    is unavailable / the output would overflow (caller falls back to Python).
    """
    lib = get_compressor()
    if lib is None or mode not in (1, 2, 3):
        return None

    src = bytes(data)
    n = len(src)
    src_buf = (ctypes.c_uint8 * n).from_buffer_copy(src) if n else (ctypes.c_uint8 * 0)()
    cap = n + n // 4 + 64  # worst-case literal expansion + sentinel + slack
    dst_buf = (ctypes.c_uint8 * cap)()

    written = lib.radiata_compress(src_buf, n, dst_buf, cap, mode)
    if written < 0:
        return None
    return bytes(dst_buf[:written])


def native_unscramble(payload) -> bytes | None:
    """Decrypt an SLE payload (bytes after the header) in place.

    Returns the unscrambled bytes, or None if the native compressor is unavailable.
    """
    return _crypt(payload, scramble=False)


def native_scramble(payload) -> bytes | None:
    """Encrypt an SLZ payload (bytes after the header) in place.

    Returns the scrambled bytes, or None if the native compressor is unavailable.
    """
    return _crypt(payload, scramble=True)


def _crypt(payload, scramble: bool) -> bytes | None:
    lib = get_compressor()
    if lib is None:
        return None

    data = bytearray(payload)
    if not data:
        return bytes(data)
    buf = (ctypes.c_uint8 * len(data)).from_buffer(data)
    (lib.sle_scramble if scramble else lib.sle_unscramble)(buf, len(data))
    return bytes(data)
