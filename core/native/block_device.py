'''
Block device + ctypes bindings. Mimics standard python io api, allowing for pure python fallback.

Three tier resolution (inspired by a GOAT):
    1. Precompiled library
    2. Compiled on demand first run
    3. IOBase pure python fallback (no physical media support)
'''

from __future__ import annotations
from asyncio import Handle
from logging.handlers import MemoryHandler

import os
import sys
import io
import ctypes
from pathlib import Path
import platform
import subprocess
import threading
import shutil
import weakref

import logging
logger = logging.getLogger(f'radiata.{__name__}')

_SOURCE_NAME = 'block_device.c'
_BUILD_DIR_NAME = '.block_device_build'

_lib: ctypes.CDLL | None = None
_load_attempted = False
_lock = threading.Lock()

##--------------------------------- BUILD ------------------------------------###

def _native_root() -> Path:
    return Path(__file__).resolve().parent

def _library_name() -> str:
    if sys.platform.startswith('win'):
        return 'block_device.dll'
    if sys.platform == 'darwin':
        return 'libblock_device.dylib'
    return 'libblock_device.so'

def _platform_tag() -> tuple[str, str]:
    if sys.platform.startswith('win'):
        osname = 'windows'
    elif sys.platform == 'darwin':
        osname = 'macos'
    else:
        osname = 'linux'
    return osname, platform.machine().lower()


def _prebuilt_lib_paths() -> list[Path]:
    name = _library_name()
    osname, arch = _platform_tag()
    dirs: list[Path] = []
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        base = Path(meipass)
        dirs += [base / 'native', base]
    root = _native_root()
    dirs += [root / 'prebuilt' / f'{osname}-{arch}', root / 'prebuilt' / osname]
    return [d / name for d in dirs]

def _find_c_compiler() -> str | None:
    if sys.platform.startswith('win'):
        return shutil.which('gcc')
    return shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')

def _build_command(compiler: str, source_path: Path, output_path: Path) -> list[str]:
    platform_flags = ['-static-libgcc'] if sys.platform.startswith('win') else ['-fPIC']
    return [
        compiler,
        '-shared',
        '-O2',
        '-std=c99',
        '-Wall',
        '-Wextra',
        *platform_flags,
        '-o',
        str(output_path),
        str(source_path)
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
        raise FileNotFoundError(f'Missing block device source: {source_path}')
    if not _build_needed(library_path, source_path, force_rebuild):
        return library_path
    compiler = _find_c_compiler()
    if not compiler:
        raise RuntimeError(
            'No C Compiler found on PATH (install Xcode CLT on macOS, '
            'build-essential on Linux, or MinGW/MYSYS2 on Windows). '
            'Not all features are supported in pure python.'
        )
    build_dir.mkdir(exist_ok=True)
    staged_source = build_dir / _SOURCE_NAME
    shutil.copyfile(source_path, staged_source)
    if library_path.exists():
        library_path.unlink()
    cmd = _build_command(compiler, staged_source, library_path)
    result = subprocess.run(cmd, cwd=build_dir, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f'Failed to build native block device:\n{details}')
    return library_path

def _bind(library_path: Path) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(library_path))

    # alloc_aligned_buffer(size_t size, uint32_t alignment) -> void*
    lib.alloc_aligned_buffer.argtypes = [ctypes.c_size_t, ctypes.c_uint32]
    lib.alloc_aligned_buffer.restype = ctypes.c_void_p

    # free_aligned_buffer(void* ptr) -> void
    lib.free_aligned_buffer.argtypes = [ctypes.c_void_p]
    lib.free_aligned_buffer.restype = None

    # open_device(const char* path, uint32_t sector_size) -> void*
    lib.open_device.argtypes = [ctypes.c_char_p, ctypes.c_uint32]
    lib.open_device.restype = ctypes.c_void_p

    # get_device_size(NativeBlockDevice* device) -> int64_t
    lib.get_device_size.argtypes = [ctypes.c_void_p]
    lib.get_device_size.restype = ctypes.c_uint64

    # read_sectors(NativeBlockDevice* device, int64_t byte_offset, void* out_buffer, size_t byte_count) -> int64_t
    lib.read_sectors.argtypes = [ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_size_t]
    lib.read_sectors.restype = ctypes.c_int64

    # read_slice(NativeBlockDevice* device, uint64_t offset, size_t size, void* aligned_buffer, size_t buffer_capacity) -> int64_t
    lib.read_slice.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    lib.read_slice.restype = ctypes.c_int64

    # close_device(NativeBlockDevice* device) -> void
    lib.close_device.argtypes = [ctypes.c_void_p]
    lib.close_device.restype = None

    return lib

def _load_native() -> ctypes.CDLL:
    for cand in _prebuilt_lib_paths():
        if cand.exists():
            try:
                lib = _bind(cand)
                logger.info('Native BlockDevice loaded (prebuilt): %s', cand)
                return lib
            except Exception as e:
                logger.warning('Failed to load prebuilt native BlockDevice: %s', cand.name, e)

    if not getattr(sys, 'frozen', False):
        try:
            library_path = _ensure_built()
            lib = _bind(library_path)
            logger.info('Native BlockDevice compiled and loaded: %s', library_path.name)
            return lib
        except Exception as e:
            logger.warning('Cound not build native block device: %s', e)
    raise RuntimeError('Failed to load or build native BlockDevice library.')

def get_block_device_lib() -> ctypes.CDLL:
    global _lib, _load_attempted
    with _lock:
        if _load_attempted:
            if _lib is None:
                raise RuntimeError('Native BlockDevice library is unavailable.')
            return _lib
        _load_attempted = True
        _lib = _load_native()
        return _lib

###------------------------------------- BlockDevice -----------------------------------###

class ThreadLocalAlignedBuffer:
    '''
    RAII wrapper around an aligned buffer allocated for each thread.
    Destructor frees the buffer when the weakref is finalized better than __del__
    since it even catches after interpreter shutdown.

    Current scratch preallocated: 2MB + 64KB
    '''
    def __init__(self, lib, capacity: int = (1024 * 1024 * 64) + (1024 * 64)) -> None:
        self._lib = lib
        self.capacity = capacity
        self.ptr = self._lib.alloc_aligned_buffer(capacity, 2048)
        if not self.ptr:
            raise MemoryError(f'Failed to allocate {capacity} aligned bytes')
        weakref.finalize(self, self._lib.free_aligned_buffer, self.ptr)

class BlockDevice:
    '''
    Represents a native block device, providing read operations via a thread-local allocation.
    If the pre-allocated scratchpad size is exceeded, a fallback to a dynamic buffer is used.
    '''
    def __init__(self, path: str | Path, sector_size: int = 2048) -> None:
        self.path        = Path(path)
        self.sector_size = sector_size
        self._lib        = get_block_device_lib()
        path_bytes       = str(self.path).encode(sys.getfilesystemencoding() or 'utf-8')
        self._dev_handle = self._lib.open_device(path_bytes, sector_size)
        if not self._dev_handle:
            raise FileNotFoundError(f'Failed to open native block device: {self.path}')
        self._size  = self._lib.get_device_size(self._dev_handle)
        self.closed = False
        self._tls = threading.local()

        self.scratch_reads = 0
        self.large_reads = 0

    @property
    def _scratch_buffer(self) -> ThreadLocalAlignedBuffer:
        '''Fetch or create a persistent C-allocated buffer for the current thread'''
        if not hasattr(self._tls, 'buf'):
            self._tls.buf = ThreadLocalAlignedBuffer(self._lib)
        return self._tls.buf

    def pread(self, offset: int, size: int) -> bytes:
        '''Stateless, thread-safe, and zero-allocation read.'''
        if self.closed or not self._dev_handle:
            raise ValueError('I/O operation on closed BlockDevice')
        size = min(size, self._size - offset)
        if size <= 0:
            return b''
        # Fetch the thread-local scratch buffer
        tls_buf = self._scratch_buffer
        if (size + (offset & (self.sector_size - 1)) + self.sector_size - 1) & ~(self.sector_size - 1) > tls_buf.capacity: # dynamic buffer fallback
            return self._pread_large(offset, size)
        skip_val = ctypes.c_size_t(0)
        # Get the slice
        available = self._lib.read_slice(
            self._dev_handle,
            offset,
            size,
            tls_buf.ptr,
            tls_buf.capacity,
            ctypes.byref(skip_val)
        )
        if available < 0:
            raise OSError(f'Failed to read {size} bytes at offset {offset}')
        skip = skip_val.value
        self.scratch_reads += 1
        return ctypes.string_at(tls_buf.ptr + skip, available) # Moving the ptr forward itself saves a copy

    def _pread_large(self, offset: int, size: int) -> bytes:
        '''
        Fallback for reads that exceed the TLS scratchpad capacity.
        Wastes resources recalculating alignments if triggered often increase scratchpad capacity.
        '''
        skip_bytes = offset & (self.sector_size - 1)
        aligned_offset = offset - skip_bytes
        aligned_size = (size + skip_bytes + self.sector_size - 1) & ~(self.sector_size - 1)
        temp_buf = self._lib.alloc_aligned_buffer(aligned_size, self.sector_size)
        if not temp_buf:
            raise MemoryError('Failed to allocate large aligned buffer.')
        try:
            bytes_read = self._lib.read_sectors(
                self._dev_handle,
                aligned_offset,
                temp_buf,
                aligned_size
            )
            if bytes_read < 0:
                raise OSError('Large read failed.')
            raw_data = ctypes.string_at(temp_buf, bytes_read)
            self.large_reads += 1
            return raw_data[skip_bytes : skip_bytes + size]
        finally:
            self._lib.free_aligned_buffer(temp_buf)

    @property
    def size(self) -> int:
        return self._size

    def close(self) -> None:
        if not self.closed and getattr(self, '_dev_handle', None):
            self._lib.close_device(self._dev_handle)
            self._dev_handle = None
        logger.debug(
            f'Closed: {self.closed}, _dev_handle: {self._dev_handle}.\n'
            f'    Scratch reads: {self.scratch_reads}, Large reads: {self.large_reads}'
        )

    def name(self) -> str:
        '''Same functionality as Path.name'''
        return str(self.path.name)

    def __len__(self) -> int:
        return self._size

    def __str__(self) -> str:
        return str(self.path.name)

    def __repr__(self) -> str:
        return f'<BlockDevice {self.path}>'
