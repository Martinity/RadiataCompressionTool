"""
PhysicalHandler ISO related processing. Extraction, rebuilding, TOC parsing, disk verification

Currently filesystems are static (non-mutable). If in the future adding and removing files
is added then this will need some serious refactoring. I think the smarter approach would be
to create some object representations of the filesystems and have the rebuild process
translate the nodes into the appropriate format for the ISO, similar to how RootDirectoryStructure works.

Something to look more closely at in the future is the assignment of logical IDs to nodes.
Currently I am aware of overlays getting ID 0, but for other types of new files research is needed.
"""

from __future__ import annotations

import sys
import abc
import logging
import array
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, BinaryIO, Any

import xxhash
from core.contracts import PhysicalHandler
from core.node import VfsNode
from core.registry import Registry
from core.workers import TaskHandle
from core.native.block_device import BlockDevice

logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------------- Constants -------------------------------------###

_KNOWN_BUILDS: dict[str, str] = {
    '7ee1ab6550739833f757ccc9db23cc36': 'Prototype',
    'afb46b880ee88e93b1f2ccb417e02977': 'USA release',
    'f5fbce42d0d943c01e506c7f7d7e24e2': 'JPN release',
}

###------------------------------------- Structs -------------------------------------###

def pack_both_endian_32(val):
    '''LBA and File Size root directory structs'''
    return struct.pack('<I', val) + struct.pack('>I', val)

def pack_both_endian_16(val):
    '''Volume Sequence Numbers for root directory structs'''
    return struct.pack('<H', val) + struct.pack('>H', val)

@dataclass(frozen=True)
class RootDirectoryStructure:
    entry_length:           int
    extended_attribute:     int
    lba:                    int
    file_size:              int
    date:                   bytes
    flag:                   int
    interleave_gap:         int
    volume_sequence_number: int
    filename_length:        int
    file_name:              str

    @classmethod
    def from_bytes(cls, record_data: bytes) -> RootDirectoryStructure:
        # Unpack the entry
        entry_length = record_data[0]
        ext_attr     = record_data[1]
        lba          = struct.unpack('<I', record_data[2:6])[0] * 0x800
        file_size    = struct.unpack('<I', record_data[10:14])[0]
        date_bytes   = record_data[18:25]
        flags        = record_data[25]
        gap_size     = record_data[27]
        vol_seq      = struct.unpack('<H', record_data[28:30])[0]
        name_len     = record_data[32]
        raw_name     = record_data[33 : 33 + name_len]
        if raw_name == b'\x00':
            file_name = '.'
        elif raw_name == b'\x01':
            file_name = '..'
        else:
            file_name = raw_name.decode('ascii', errors='replace').split(';')[0]
        logger.debug(f'file_name: {file_name}')
        return cls(
            entry_length=entry_length,
            extended_attribute=ext_attr,
            lba=lba,
            file_size=file_size,
            date=date_bytes,
            flag=flags,
            interleave_gap=gap_size,
            volume_sequence_number=vol_seq,
            filename_length=name_len,
            file_name=file_name,
        )

    def to_bytes(self) -> bytes:
        '''Convert a RootDirectoryStructure into its binary representation.'''
        # Handle file names
        if self.file_name == '.':
            name = b'\x00'
        elif self.file_name == '..':
            name = b'\x01'
        else:
            name = self.file_name.encode('ascii', errors='replace') + b';1'
        # Calculate lengths
        name_len    = len(name)
        name_padding = 1 if name_len & 1 else 0 # ISO 9660 name alignment
        system_use = 14 if self.file_name not in ('.', '..') else 0 # Sony CDVDGEN system use area
        record_len  = 33 + name_len + name_padding + system_use
        if record_len & 1:
            record_len += 1
        data        = bytearray(record_len)
        data[0]     = record_len
        data[1]     = self.extended_attribute
        lba         = self.lba // 0x800
        data[2:10]  = pack_both_endian_32(lba)
        data[10:18] = pack_both_endian_32(self.file_size)
        data[18:25] = self.date[:7]
        data[25]    = self.flag
        data[26]    = 0
        data[27]    = self.interleave_gap
        data[28:32] = pack_both_endian_16(self.volume_sequence_number)
        data[32]    = name_len
        data[33:33 + name_len] = name
        return bytes(data)


class DiskRegion(abc.ABC):
    '''One contiguous region of the final disk image.'''
    start_offset: int = 0
    @property
    @abc.abstractmethod
    def size(self) -> int:
        '''Byte size of the region'''
    def label(self) -> str:
        '''Human-readable iditification for logging/progress.'''
        return self.__class__.__name__
    def write_to(self, dst: BinaryIO) -> int:
        '''Write contents directly to the destination stream, return number of bytes written.'''

@dataclass(slots=True)
class RawCopyRegion(DiskRegion):
    '''A raw copy of a region from the source disk image.'''
    source_offset: int
    length:        int
    name:          str
    src_handle:    BlockDevice
    @property
    def size(self) -> int:
        return self.length
    def label(self) -> str:
        return self.name or f'RawCopy@{self.source_offset:#x}'
    def write_to(self, dst: BinaryIO) -> int:
        chunk_size = 24 * 1024 * 1024 # 24MB chunks
        bytes_left = self.length
        curent_offset = self.source_offset
        while bytes_left > 0:
            read_size = min(chunk_size, bytes_left)
            chunk = self.src_handle.pread(curent_offset, read_size)
            dst.write(chunk)
            bytes_left -= read_size
            curent_offset += read_size
        return self.length

@dataclass(slots=True)
class StagedDataRegion(DiskRegion):
    '''Modified data region that has been staged for writing to the disk image.'''
    node:        VfsNode
    sector_size: int
    @property
    def size(self) -> int:
        if self.node.pending_data is None: return 0
        raw = len(self.node.pending_data)
        return raw + ((-raw) & (self.sector_size - 1))
    def label(self) -> str:
        return f'Staged:{self.node.name}'
    def write_to(self, dst: BinaryIO) -> int:
        if self.node.pending_data is None: return 0
        dst.write(self.node.pending_data)
        padding = self.size - len(self.node.pending_data)
        if padding:
            dst.write(b'\x00' * padding)
        return self.size

@dataclass(slots=True)
class ZeroFillRegion(DiskRegion):
    '''Padding region filled with zero bytes.'''
    length: int
    name:   str = 'padding'
    @property
    def size(self) -> int:
        return self.length
    def label(self) -> str:
        return self.name
    def write_to(self, dst: BinaryIO) -> int:
        dst.write(b'\x00' * self.length)
        return self.length

@dataclass(slots=True)
class SentinelRegion(DiskRegion):
    '''A genuinely empty region used as a sentinel pointer.'''
    node: VfsNode
    @property
    def size(self) -> int:
        return 0
    def label(self) -> str:
        return f'Sentinel:{self.node.name}'
    def write_to(self, dst: BinaryIO) -> int:
        return 0

@dataclass(slots=True)
class TocRegion(DiskRegion):
    '''A final scrambled TOC region.'''
    total_entries: int
    sector_size:   int
    signature:     int
    scramble_fn:   Callable[list[int], list[int]]
    entries:       list[tuple[VfsNode, DiskRegion | None]] = field(default_factory=list)
                            # Node, Self-reference or sentinel
    @property
    def size(self) -> int:
        return self.total_entries * 3 * 4
    def label(self) -> str:
        return 'TOC'
    def write_to(self, dst: BinaryIO,) -> int:
        flat = [0] * (self.total_entries * 3)
        for i, (node, region) in enumerate(self.entries[:self.total_entries]):
            if region is None or isinstance(region, SentinelRegion):
                flat[i] = -1
                flat[self.total_entries + i] = 0
            else:
                flat[i] = region.start_offset // self.sector_size
                flat[self.total_entries + i] = region.size // self.sector_size
            flat[2 * self.total_entries + i] = getattr(node, 'logical_id', 0)
        scrambled = self.scramble_fn(flat)
        toc_array = array.array('I', (x & 0xFFFFFFFF for x in scrambled))
        actual_sig = toc_array[0]
        if actual_sig != self.signature:
            logger.warning(
                f'TOC signature mismatch: expected {hex(self.signature)}, got {hex(actual_sig)}. '
                f'Entry 0 self-reference reconstruction failed, manually overwritting with correct signature.'
            )
            toc_array[0] = self.signature
        if sys.byteorder != 'little':
            toc_array.byteswap()
        dst.write(toc_array.tobytes())
        return len(toc_array.tobytes())

@dataclass(slots=True)
class RootDirectoryRegion(DiskRegion):
    '''Represents the root directory region of an ISO filesystem.'''
    fixed_size: int
    original_bytes: bytes = b''
    front_section_entries: list[tuple[VfsNode, DiskRegion]] = field(default_factory=list)
    @property
    def size(self) -> int:
        return self.fixed_size
    def label(self) -> str:
        return 'RootDirectory'
    def write_to(self, dst: BinaryIO) -> int:
        if not self.original_bytes:
            raise ValueError('original_bytes must be set before writing')
        output = bytearray()
        bytes_read = 0
        while bytes_read < self.fixed_size:
            entry_length = self.original_bytes[bytes_read]
            if not entry_length:
                bytes_read += 1
                continue
            record_slice = self.original_bytes[bytes_read : bytes_read + entry_length]
            record = RootDirectoryStructure.from_bytes(bytes(record_slice))

            if record.file_name in ('.', '..'):
                output.extend(record_slice)
            else:
                name, sep, ext = record.file_name.rpartition('.')
                match = next(((n, r) for n, r in self.front_section_entries if n.name == name and n.extension == sep + ext), None)
                if match:
                    node, region = match
                    updated_record = RootDirectoryStructure(
                        entry_length=record.entry_length,
                        extended_attribute=record.extended_attribute,
                        lba=region.start_offset,
                        file_size=len(node.pending_data) if node.pending_data is not None else node.size,
                        date=record.date,
                        flag=record.flag,
                        interleave_gap=record.interleave_gap,
                        volume_sequence_number=record.volume_sequence_number,
                        filename_length=record.filename_length,
                        file_name=record.file_name,
                    )
                    output.extend(updated_record.to_bytes())
            bytes_read += entry_length
        if len(output) < self.fixed_size:
            output.extend(b'\x00' * (self.fixed_size - len(output)))
        dst.write(output)
        return len(output)

@dataclass(slots=True)
class ExecutablePatchRegion(DiskRegion):
    '''Specifically for slimmed rebuild.
    I feel like having to do it this way shows that the system is not the best design.'''
    node:           VfsNode
    src_handle:     BlockDevice
    sector_size:    int
    new_toc_offset: int = -1
    @property
    def size(self) -> int:
        if self.node.pending_data is not None:
            raw = len(self.node.pending_data)
            return (raw + ((-raw) & (self.sector_size - 1)))
        return self.node.size
    def write_to(self, dst: BinaryIO) -> int:
        logger.debug(f'Writing executable patch region: {self.node.name}')
        if self.node.pending_data is not None:
            data = bytearray(self.node.pending_data)
            padding = self.size - len(data)
        else:
            data = bytearray(self.src_handle.pread(self.node.offset, self.node.size))
            padding = self.size - len(data)
        if self.new_toc_offset != -1:
            separator  = b'\x2D\x20\x20\x02'
            static_lui = b'\x02\x3C'
            static_ori = b'\x42\x34'
            pos = 0
            while pos < (len(data) - 8):
                pos = data.find(separator, pos)
                if pos == -1:
                    break
                if pos % 4 != 0:
                    pos += 1
                    continue
                # LUI/ORI neighbor check
                if (data[pos - 2 : pos] == static_lui and data[pos + 6 : pos + 8 ] == static_ori):
                    hi_val = (self.new_toc_offset >> 16) & 0xFFFF
                    lo_val = self.new_toc_offset & 0xFFFF

                    data[pos - 4 : pos - 2] = struct.pack('<H', hi_val)
                    data[pos + 4 : pos + 6] = struct.pack('<H', lo_val)
                    logger.debug(f'Found TOC offsets @ {pos}, new offset {self.new_toc_offset}')
                    break
                pos += 4
        dst.write(data)
        if padding > 0:
            dst.write(b'\x00' * padding)
        return len(data) + padding

###------------------------------ Rebuild Layout ------------------------------------###

class DiskLayoutPlanner:
    '''
    Pass1: accumulate regions in disk order
    resolve_offsets(): assign every region's start_offset by cumulative sum.
    pass2: write_all() emits every region's bytes sequentially
    '''
    def __init__(self) -> None:
        self.regions: list[DiskRegion] = []
        self._resolved: bool = False

    def add(self, region: DiskRegion) -> DiskRegion:
        '''Append and return the region so callers can hold a reference for cross-referencing'''
        if self._resolved:
            raise RuntimeError('Cannot add regions after resolve_offsets() has been called')
        self.regions.append(region)
        return region

    def resolve_offsets(self) -> None:
        '''The lba_map concept collapsed into a simple cumulative offset calculation'''
        cursor = 0
        for region in self.regions:
            region.start_offset = cursor
            cursor += region.size
        self._resolved = True
        logger.debug(f'DiskLayoutPlanner: resolved {len(self.regions)} regions, total size {cursor:#x}')

    def total_size(self) -> int:
        return sum(r.size for r in self.regions)

    def write_all(self, dst: BinaryIO, task_handle: TaskHandle, progress_every: int = 1) -> None:
        '''one pass - strict sequential write'''
        if not self._resolved:
            raise RuntimeError('resolve_offsets() must be called before write_all().')
        total = len(self.regions)
        for i, region in enumerate(self.regions):
            task_handle.checkpoint()
            region.write_to(dst)
            # task_handle.log_message.emit(f'Current position: {dst.tell():#x}')
            if i % progress_every == 0:
                pct = int((i / total) * 100) if total else 100
                task_handle.progress.emit(pct)
        logger.info(f'DiskLayoutPlanner: wrote {total} regions ({self.total_size():,} bytes)')


###------------------------------ ISO HANDLER ------------------------------------###


@Registry.register(name='Radiata Stories ISO Handler', extensions=('.iso',))
class IsoHandler(PhysicalHandler):
    """
    Responsible for managing and keeping the ISOfs and Vfs consistent and in sync.
    """

    @dataclass(slots=True)
    class IsoParameters:
        """Hardcoded disk parameters"""
        seed:                   int = 0x13578642
        signature:              int = 0x27D51556  # raw scrambled TOC self-reference
        toc_offset:             int = -1
        total_entries:          int = 0x1200
        sector_size:            int = 0x800
        iso_9660_pvd:           int = 16
        pvd_byte_offset: int = 0x9C

    def __init__(self, source: BlockDevice, parent=None):
        """Initialize iso properties"""
        super().__init__(source, parent_node=parent)
        logger.info(f'IsoHandler initialized for {source.name}')
        self.params            = self.IsoParameters()
        self.toc: list         = []
        self.toc_location      = -1
        self.pvd: RootDirectoryStructure | None = None
        self.cnf: tuple[int, int] | None = None
        self.system_areas      = VfsNode()

    def get_raw_node(self, node: VfsNode) -> bytes:
        """Public call for the raw data of a physical node"""
        return self.handle.pread(node.offset, node.size)

    ###------------------------------------ Extract ISO ------------------------------------###

    def _get_iso_dir(self) -> VfsNode:
        '''
        Returns the root node of the ISO directory tree containing all files.
        '''
        SIGNATURE        = b'RADIATA'
        SIGNATURE_OFFSET = 0x28
        SIGNATURE_LENGTH = 7
        offset = (self.params.iso_9660_pvd * self.params.sector_size) + SIGNATURE_OFFSET
        title = self.handle.pread(offset, SIGNATURE_LENGTH)
        if title != SIGNATURE:
            raise ValueError(
                f'Not a Radiata Stories disk. Found {title}, expected {SIGNATURE} @{offset}({hex(offset)})'
            )

        root = VfsNode(name='root')
        # Read descriptor volume for root dir location
        offset = (self.params.iso_9660_pvd * self.params.sector_size) + self.params.pvd_byte_offset
        pvd_len = self.handle.pread(offset, 1)[0]
        self.system_areas.append_child(VfsNode(name='System Area 1', offset=0, size=offset))
        self.pvd = RootDirectoryStructure.from_bytes(self.handle.pread(offset, pvd_len))
        self.system_areas.append_child(VfsNode(name='System Area 2', offset=offset + pvd_len, size=self.pvd.lba))
        # Read root dir for files
        bytes_read = 0
        root_dir_view = memoryview(self.handle.pread(self.pvd.lba, self.pvd.file_size))
        logger.debug(f'PVD file size: {self.pvd.file_size}')
        while bytes_read < self.pvd.file_size:
            entry_length = root_dir_view[bytes_read]
            bytes_read  += 1
            if not entry_length:
                break
            # Check for sector padding between entries (probably pointless...)
            if entry_length == 0:
                padding_to_skip = (-bytes_read) & (self.params.sector_size - 1)
                if padding_to_skip < self.params.sector_size:
                    bytes_read += padding_to_skip
                continue
            record_slice = root_dir_view[bytes_read - 1 : bytes_read - 1 + entry_length]
            bytes_read += entry_length - 1
            record = RootDirectoryStructure.from_bytes(record_slice.tobytes())
            if record.file_name in ('.', '..'): # Skip self-references
                continue
            name, sep, ext = record.file_name.rpartition('.')
            node = VfsNode(
                name=name,
                category=('ISO',),
                extension=sep + ext,
                offset=record.lba,
                size=record.file_size,
                parent=root,
                target=None,
            )
            node.is_physical = True
            root.append_child(node)
            if ext == 'CNF':
                self.cnf = (record.lba * self.params.sector_size, record.file_size)

        # ISO Integrity Check
        found_names = {child.name for child in root.children}
        required_files = {'IOPRP300', 'SYSTEM'}
        has_system_files = required_files.issubset(found_names)
        has_executable = bool({'SLUS_212', 'SLPM_658'}.intersection(found_names))
        if not (has_system_files and has_executable):
            raise ValueError(
                'ISO needs IOPRP300, SYSTEM, and SLUS/SLPM for proper execution.'
            )

        # Root directory to end of ISO filesystem system areas - for rebuilding
        physical_files = sorted(root.children, key=lambda n: n.offset)
        pos = self.pvd.lba + self.pvd.file_size
        for node in physical_files:
            gap = node.offset - pos
            if gap > 0:
                gap_node = VfsNode(
                    name=f'AreaBefore{node.name}',
                    size=gap,
                    offset=pos,
                )
                self.system_areas.append_child(gap_node)
            pos = max(pos, node.offset + node.size)

        return root

    def _get_vfs_dir(self, toc: list[dict[str, Any]]) -> VfsNode:
        '''
        Returns the root node of the VFS (the toc/gameassets)
        '''
        logger.debug('Building VFS tree from TOC')
        # Root node
        root = VfsNode(name='VFS Mount-point', size=-1, offset=-1)
        root.is_hidden   = False
        root.is_boundary = True

        for entry in toc:
            disk_index = entry['id']
            # Sentinel nodes
            if entry['size'] == 0:
                sentinel = VfsNode(
                    name=f'sentinel {disk_index}',
                    offset=-1,
                    size=-1,
                    parent=root,
                )
                sentinel.is_hidden = True
                sentinel.logical_id = entry['logical_id']
                root.append_child(sentinel)
                continue
            # Valid nodes
            node = VfsNode(
                name='Unknown',
                category=('',),
                offset=entry['offset'],
                size=(entry['size'] * self.params.sector_size),
                parent=root,
                target=None,
            )
            node.is_physical = True
            node.logical_id = entry['logical_id']
            root.append_child(node)
            if disk_index in [0, 5]:  # Hide file system nodes
                node.is_hidden = True
        logger.info(f'Tree built - {len(root.children)} total files - {sum(1 for valid in root.children if valid.is_hidden is True)} hidden files')
        return root

    def get_file_tree(self) -> VfsNode:
        """
        Return the entire ISO file tree required for runtime.

        The final iso_root entry is always the vfs_root node.
        This is crucial to ensure the VfsManager tracks relational data correctly.

        Dynamically locate the TOC offset and process the TOC data.
        """
        iso_root = self._get_iso_dir()
        self._locate_toc_offset(iso_root)
        structured_toc = self._process_toc(self._load_toc())
        iso_root.append_child(self._get_vfs_dir(structured_toc))
        return iso_root

    ###----------------------------- TOC Parsing ---------------------------------###

    def _locate_toc_offset(self, root: VfsNode) -> None:
        """
        Locate the TOC offset and update the IsoParameters.
        To locate the TOC offset we scan the main ELF for a masked signature.
        """
        elf_bytes: bytes | None = None
        for child in root.children:
            if child.name.startswith('SLUS') or child.name.startswith('SLPM'):
                elf_bytes = self.handle.pread(child.offset, child.size)
                break
        if elf_bytes is None:
            raise ValueError('No main ELF found.')

        separator  = b'\x2D\x20\x20\x02'
        static_lui = b'\x02\x3C'
        static_ori = b'\x42\x34'
        toc_offset = 0
        pos = 0
        while pos < (len(elf_bytes) - 8):
            pos = elf_bytes.find(separator, pos)
            if pos == -1:
                break
            if pos % 4 != 0: # Alignement
                pos += 1
                continue
            # Check for LUI/ORI mask signature in the previous and subsequent words' low nibbles
            if (elf_bytes[pos - 2 : pos] == static_lui and
                elf_bytes[pos + 6 : pos + 8] == static_ori
            ):
                hi_val = int.from_bytes(elf_bytes[pos - 4 : pos - 2], 'little')
                lo_val = int.from_bytes(elf_bytes[pos + 4 : pos + 6], 'little')
                toc_offset = (hi_val << 16) | lo_val
                break
            pos += 4
        if toc_offset == 0:
            raise ValueError('Could not locate TOC offset in main ELF.')
        self.toc_location = pos # To be reused for ISO rebuilding to change the TOC offset
        self.params.toc_offset = toc_offset
        logger.debug(f'Toc location: {self.toc_location:#x}, Toc offset: {toc_offset:#x}')

    def _load_toc(self) -> memoryview:
        """Locate the TOC."""
        # Check for radiata ISO
        offset = self.params.toc_offset * self.params.sector_size
        toc_view = memoryview(self.handle.pread(offset, self.params.total_entries * 3 * 4))
        # if self.params.signature != int.from_bytes(toc_view[:4], 'little'):
        #     raise ValueError(
        #         f'Not a Radiata Stories TOC. Got TOC signature: {int.from_bytes(toc_view[:4], 'little'):#x} Expected: {hex(self.params.signature)}'
        #     )
        return toc_view

    def _process_toc(self, scrambled_toc: memoryview) -> list[dict[str, Any]]:
        """Unscramble and structure the TOC data"""
        total = self.params.total_entries
        toc = list(struct.unpack(f'<{total * 3}I', scrambled_toc))
        toc = self._scramble(toc[:])

        structured = []
        for i in range(total):
            lba = toc[i]
            size = toc[total + i]
            logical_id = toc[(total * 2) + i]
            structured.append(
                {
                    'id': i,
                    'lba': lba,
                    'size': size,
                    'offset': lba * self.params.sector_size,
                    'logical_id': logical_id,
                    'name': f'FILE_{i:04d}.bin',
                }
            )
        return structured

    ###----------------------------------- Build ISO ------------------------------------------###

    def rebuild_node(
        self,
        root:         VfsNode,
        staged_nodes: list[VfsNode],
        output_path:  Path,
        slimmed_rebuild_requested: bool,
        task_handle:  TaskHandle,
    ) -> bool:
        '''
        Rebuilds the ISO using DiskLayoutPlanner sequentially.
        slimmed_rebuild_requested shifts the TOC location hardcode sector.

        Because the tool currently doesn't support mutable file systems the way the first
        three areas are built is redundant and can be collapsed into one copy. I wrote it
        this way to be easier to expand to adding/removing files in the future.
        '''
        src_path = self.handle.path
        if output_path.resolve() == src_path.resolve():
            raise ValueError('Cannot overwrite source ISO')
        if self.pvd is None:
            raise ValueError('PVD not found')
        staged_set = set(staged_nodes)
        vfs_root = root.children[-1] if root.children[-1].is_boundary else self._locate_boundary(root)
        if vfs_root is None:
            raise ValueError('No boundary node found')
        # Merge VFS nodes with system areas (padding and sony disk markers)
        iso_nodes = [child for child in root.children if not child.is_boundary]
        iso_nodes += [child for child in self.system_areas.children[2:]]
        iso_nodes.sort(key=lambda node: node.offset)
        try:
            planner = DiskLayoutPlanner()
            ### System Area 1 - start to PVD
            sys_area_1_len = (self.params.iso_9660_pvd * self.params.sector_size) + self.params.pvd_byte_offset
            planner.add(RawCopyRegion(self.system_areas.children[0].offset, self.system_areas.children[0].size, 'System Area', self.handle))
            logger.debug(f'System Area 1: {planner.total_size()//2048}')
            ### PVD - a single record pointing to the root directory
            pvd_record_region = RawCopyRegion(
                source_offset=sys_area_1_len,
                length=self.pvd.entry_length,
                name='PVD Record',
                src_handle=self.handle
            )
            planner.add(pvd_record_region)
            logger.debug(f'PVD: {planner.total_size()//2048}')
            ### System Area 2 - between PVD and root directory
            pvd_end = sys_area_1_len + self.pvd.entry_length
            planner.add(RawCopyRegion(pvd_end, self.pvd.lba - pvd_end, 'System Area 2', self.handle))
            logger.debug(f'System Area 2: {planner.total_size()//2048}')
            ### Root Directory - Root directory records
            original_dir_bytes = self.handle.pread(self.pvd.lba, self.pvd.file_size)
            root_dir_region = RootDirectoryRegion(
                fixed_size=self.pvd.file_size,
                original_bytes=original_dir_bytes
            )
            planner.add(root_dir_region)
            logger.debug(f'Root Directory: {planner.total_size()//2048}')
            ### ISO filesystem - root directory records files
            exec_patch_region = None
            for node in iso_nodes:
                logger.debug(f'{node.name}: {(node.offset + node.size)//2048:#x}')
                if not node.name.endswith(('IOPRP300', 'SLUS_212', 'SLPM_658', 'SYSTEM')): # Only write valid runtime files
                    continue
                if node.name.startswith('AreaBefore'):
                    region = RawCopyRegion(node.offset, node.size, node.name, self.handle)
                    planner.add(region)
                    continue
                if node.name.startswith('SLUS') or node.name.startswith('SLPM'):
                    logger.debug(f'Found executable: {node.name}, setting exec_patch_region')
                    exec_patch_region = ExecutablePatchRegion(node, self.handle, self.params.sector_size)
                    planner.add(exec_patch_region)
                    root_dir_region.front_section_entries.append((node, exec_patch_region))
                    continue
                elif node in staged_set and node.pending_data is not None:
                    region = StagedDataRegion(node, self.params.sector_size)
                else:
                    region = RawCopyRegion(node.offset, node.size, node.name, self.handle)
                planner.add(region)
                root_dir_region.front_section_entries.append((node, region))
            logger.debug(f'ISO filesystem: {planner.total_size()//2048}, TOC offset: {int(self.params.toc_offset)}')
            ### System Area 4 - (slimmed_requested dependent) End of ISO filesystem to VFS TOC
            current_size = planner.total_size()
            if current_size > self.params.toc_offset * self.params.sector_size:
                raise ValueError(
                    f'ISO filesystem exceeded hardcoded TOC offset! '
                    f'({current_size:#x} > {self.params.toc_offset:#x})'
                )
            if not slimmed_rebuild_requested:
                padding = (self.params.toc_offset * self.params.sector_size) - current_size
                if padding > 0:
                    planner.add(ZeroFillRegion(padding, 'TOC_offset_Padding'))
            else:
                ### Slimmed rebuild - no Area 4 padding + elf patch
                padding = (-current_size) & (self.params.sector_size - 1)
                if padding > 0:
                    planner.add(ZeroFillRegion(padding, 'TOC_alignment_padding'))
                    current_size += padding
                new_toc_sector = current_size // self.params.sector_size
                if exec_patch_region:
                    exec_patch_region.new_toc_offset = new_toc_sector
                    logger.debug(f'Slimmed rebuild: Patched executable TOC offset to {new_toc_sector:#x}')
                else:
                    raise ValueError(f'Could not patch executable TOC offset - exec_patch_region {type(exec_patch_region)}')
            ### TOC - table of contents for the VFS of the game data
            toc_region = TocRegion(
                total_entries=self.params.total_entries, # to modify total entries main ELF needs to be patched
                sector_size=self.params.sector_size,
                signature=self.params.signature,
                scramble_fn=self._scramble
            )
            planner.add(toc_region)
            ### Virtual File System - Game data
            for idx, child in enumerate(vfs_root.children):
                if idx == 0: # self-reference
                    toc_region.entries.append((child, None))
                    continue
                orig_lba = self.toc[idx]['lba'] if idx < len(self.toc) else 0
                if (child.size == -1 and child not in staged_set) or orig_lba == -1: # Sentinel
                    region = SentinelRegion(child)
                    toc_region.entries.append((child, region))
                    continue
                # Data nodes
                if child in staged_set and child.pending_data is not None:
                    region = StagedDataRegion(child, self.params.sector_size)
                else:
                    region = RawCopyRegion(child.offset, child.size, child.name, self.handle)
                toc_region.entries.append((child, region))
                planner.add(region)
            ### Resolve
            task_handle.log_message.emit(f'Resolving physical disk layout offsets for {len(planner.regions)} regions.')
            planner.resolve_offsets()
            ### Write
            with open(output_path, 'wb') as dst:
                task_handle.log_message.emit('Starting sequential write...')
                planner.write_all(dst, task_handle)
            return True

        except Exception as e:
            logger.error(f'Rebuild failed: {e}', exc_info=True)
            if output_path.exists() and output_path != src_path:
                try:
                    output_path.unlink()
                    logger.info(f'Removed partial output: {output_path.name}')
                except OSError as unlink_error:
                    logger.error(f'Failed to remove partial output: {unlink_error}', exc_info=True)
            return False

    ###---------------------------------- Utility -------------------------------------------###

    def get_build(self, root: VfsNode) -> str:
        """Return the str name of the build inferred from the ISO level files"""
        iso_file_names = {child.name for child in root.children}

        if 'SLPM_658' in iso_file_names:
            return 'JPN release'
        if 'SLUS_212' in iso_file_names:
            if 'DEV9' in iso_file_names:
                return 'Prototype'
            return 'USA release'
        logger.warning(
            f'Unknown build registered. Could not locate SLPM_658 or SLUS_212. '
            f'ISO contents: {iso_file_names}'
        )
        return 'Unknown'

    def verify_iso_integrity(self, task_handle: TaskHandle) -> str:
        '''
        Hash the ISO with xxhash and compare against known hashes.
        BlockDevice natively handles the pre-allocation of a zero-copy buffer.
        '''
        file_size = getattr(self.source, 'size', 0)
        hasher = xxhash.xxh128()
        chunk_size = (24 * 1024 * 1024) # 24MB chunks seemed to load the fastest I have tested
        bytes_read = 0
        raise RuntimeError('Test failed.')
        while (chunk := self.handle.pread(size=chunk_size, offset=bytes_read)):
            task_handle.checkpoint()
            hasher.update(chunk)
            bytes_read += len(chunk)
            task_handle.progress.emit(int(bytes_read / file_size * 100))
        digest = hasher.hexdigest()
        build = _KNOWN_BUILDS.get(digest, f'Modified/Unknown: {digest}')
        return build

    def _scramble(self, flat_toc: list[int]) -> list[int]:
        """scramble or unscramble the toc"""
        total = self.params.total_entries
        key = self.params.seed
        scramble = flat_toc[:]

        for i in range(total):
            scramble[0 * total + i] ^= key
            key ^= (key << 1) & 0xFFFFFFFF
            scramble[1 * total + i] ^= key
            key ^= (~self.params.seed) & 0xFFFFFFFF
            scramble[2 * total + i] ^= key
            key ^= ((key << 2) ^ self.params.seed) & 0xFFFFFFFF

        return scramble

    def _locate_boundary(self, node: VfsNode) -> VfsNode | None:
        '''Manually locate the boundary node'''
        for child in node.children:
            if child.is_boundary:
                return child
