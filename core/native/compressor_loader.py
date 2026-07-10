"""On-demand native build + ctypes binding for the SLZ/SLE compressor.

Resolves the native compressor in three tiers: a prebuilt library shipped with the
app, otherwise the C source compiled on first use with the system compiler,
otherwise None. Any failure (missing compiler, build error, load error) is
reported once and returns None, so callers fall back to the pure-Python compressor
in RadiCompressor.
"""

from __future__ import annotations

import ctypes
import platform
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import logging

logger = logging.getLogger(f"radiata.{__name__}")

_SOURCE_NAME = "radiata_compressor.c"
_BUILD_DIR_NAME = ".compressor_build"

_lib: ctypes.CDLL | None = None
_load_attempted = False
_lock = threading.Lock()


###--------------------------------------------- Build ---------------------------------------------###

def _native_root() -> Path:
    return Path(__file__).resolve().parent


def _library_name() -> str:
    if sys.platform.startswith("win"):
        return "radiata_compressor.dll"
    if sys.platform == "darwin":
        return "libradiata_compressor.dylib"
    return "libradiata_compressor.so"


def _platform_tag() -> tuple[str, str]:
    if sys.platform.startswith("win"):
        osname = "windows"
    elif sys.platform == "darwin":
        osname = "macos"
    else:
        osname = "linux"
    return osname, platform.machine().lower()


def _prebuilt_lib_paths() -> list[Path]:
    """Candidate locations for a CI-built lib, in priority order.

    Covers both the frozen app bundle (PyInstaller's _MEIPASS) and a
    prebuilt/ directory shipped alongside the source. macOS ships a single
    universal2 lib, so the arch-less directory is also searched.
    """
    name = _library_name()
    osname, arch = _platform_tag()
    dirs: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:  # running inside a PyInstaller bundle
        base = Path(meipass)
        dirs += [base / "native", base]

    root = _native_root()
    dirs += [root / "prebuilt" / f"{osname}-{arch}", root / "prebuilt" / osname]

    return [d / name for d in dirs]


def _find_c_compiler() -> str | None:
    if sys.platform.startswith("win"):
        return shutil.which("gcc")
    return shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


def _build_command(compiler: str, source_path: Path, output_path: Path) -> list[str]:
    platform_flags = ["-static-libgcc"] if sys.platform.startswith("win") else ["-fPIC"]
    return [
        compiler,
        "-shared",
        "-O2",
        "-std=c99",
        "-Wall",
        "-Wextra",
        *platform_flags,
        "-o",
        str(output_path),
        str(source_path),
    ]


def _build_needed(library_path: Path, source_path: Path, force: bool) -> bool:
    if force or not library_path.exists():
        return True
    return source_path.stat().st_mtime > library_path.stat().st_mtime


def _ensure_built(force_rebuild: bool = False) -> Path:
    root = _native_root()
    source_path = root / _SOURCE_NAME
    build_dir = root / _BUILD_DIR_NAME
    library_path = build_dir / _library_name()

    if not source_path.exists():
        raise FileNotFoundError(f"Missing compressor source: {source_path}")

    if not _build_needed(library_path, source_path, force_rebuild):
        return library_path

    compiler = _find_c_compiler()
    if not compiler:
        raise RuntimeError(
            "No C compiler found on PATH (install Xcode CLT on macOS, "
            "build-essential on Linux, or MinGW/MSYS2 on Windows)."
        )

    build_dir.mkdir(exist_ok=True)
    staged_source = build_dir / _SOURCE_NAME
    shutil.copyfile(source_path, staged_source)

    if library_path.exists():
        library_path.unlink()

    cmd = _build_command(compiler, staged_source, library_path)
    result = subprocess.run(cmd, cwd=build_dir, text=True, capture_output=True)
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Failed to build native compressor:\n{details}")

    return library_path


def _bind(library_path: Path) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(library_path))
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


def _load_native() -> ctypes.CDLL | None:
    """Resolve the native compressor across three tiers: prebuilt -> compile -> none."""
    # Tier 1: a CI-built lib bundled with the app (zero setup, no compiler).
    for cand in _prebuilt_lib_paths():
        if cand.exists():
            try:
                lib = _bind(cand)
                logger.info("Native SLZ/SLE compressor loaded (prebuilt): %s", cand)
                return lib
            except Exception as exc:  # noqa: BLE001 - try the next candidate/tier
                logger.warning("Prebuilt compressor %s failed to load: %s", cand.name, exc)

    # Tier 2: compile from the shipped source (developers running from source).
    # Skipped in a frozen bundle, which has no compiler and a read-only payload.
    if not getattr(sys, "frozen", False):
        try:
            library_path = _ensure_built()
            lib = _bind(library_path)
            logger.info("Native SLZ/SLE compressor compiled and loaded: %s", library_path.name)
            return lib
        except Exception as exc:  # noqa: BLE001 - fall through to Python
            logger.warning("Could not build native compressor: %s", exc)

    # Tier 3: caller falls back to the pure-Python compressor.
    return None


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
        _lib = _load_native()
        if _lib is None:
            logger.warning("Using pure-Python SLZ/SLE compressor (native unavailable).")
        return _lib


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
