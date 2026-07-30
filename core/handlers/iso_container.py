"""PhysicalHandler ISO related processing. Extraction, rebuilding, TOC parsing, disk verification"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import xxhash
from core.contracts import PhysicalHandler
from core.node import VfsNode
from core.registry import Registry
from core.workers import TaskHandle
from core.native.block_device import BlockDevice

logger = logging.getLogger(f'radiata.{__name__}')

_KNOWN_BUILDS: dict[str, str] = {
    '7ee1ab6550739833f757ccc9db23cc36': 'Prototype',
    'afb46b880ee88e93b1f2ccb417e02977': 'USA release',
    'f5fbce42d0d943c01e506c7f7d7e24e2': 'JPN release',
}

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
        root_dir_record_offset: int = 0x9C

    def __init__(self, source: BlockDevice, parent=None):
        """Initialize iso properties"""
        super().__init__(source, parent_node=parent)
        logger.info(f'IsoHandler initialized for {source.name}')

        self.params            = self.IsoParameters()
        self.toc: list         = []
        self.toc_location      = -1
        self.pvd: RootDirectoryStructure | None = None
        self.cnf: tuple[int, int] | None = None

    def get_raw_node(self, node: VfsNode) -> bytes:
        """Called for the raw data of a physical node with a private handle"""
        data = self.handle.pread(node.offset, node.size)
        logger.debug(
            f'Read {len(data) // self.params.sector_size} sectors from offset {hex(node.offset)}'
        )
        return data

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
        offset = (self.params.iso_9660_pvd * self.params.sector_size) + self.params.root_dir_record_offset
        pvd_len = self.handle.pread(offset, 1)[0]
        self.pvd = RootDirectoryStructure.from_bytes(self.handle.pread(offset, pvd_len))

        # Read root dir for files
        bytes_read = 0
        root_dir_view = memoryview(self.handle.pread(self.pvd.lba, self.pvd.file_size))
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
    #
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
            raise ValueError('Failed to locate TOC offset in main ELF.')
        self.toc_location = pos # To be reused for ISO rebuilding to change the TOC offset
        self.params.toc_offset = toc_offset

    def _load_toc(self) -> memoryview:
        """Locate the TOC."""
        # Check for radiata ISO
        offset = self.params.toc_offset * self.params.sector_size
        toc_view = memoryview(self.handle.pread(offset, self.params.total_entries * 3 * 4))
        if self.params.signature != int.from_bytes(toc_view[:4], 'little'):
            raise ValueError(
                f'Not a Radiata Stories TOC. Got TOC signature: {int.from_bytes(toc_view[:4], 'little')} Expected: {hex(self.params.signature)}'
            )
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
        task_handle:  TaskHandle,
        slimmed_rebuild_requested: bool,
    ) -> bool:
        """
        Rebuild the iso file system and the games virtual filesystem independently.

        Starts with the iso file system (front of the disk) before rebuilding the virtual filesystem.
        If the slimmed_rebuild_requested flag is set the toc location hardcode is moved to the next
        available sector after the iso file system.

        The virtual filesystem is rebuilt after the iso file system to preserve physical ordering and aliasing.
        """
        src_path: Path = self.source.path
        if output_path.resolve() == src_path.resolve():
            raise ValueError('Cannot overwrite source ISO')
        # Split the staged nodes into two sets, one for ISO and VFS
        staged_iso_set = set()
        staged_vfs_set = set()
        for child in root.children:
            if child in staged_nodes and child.is_boundary is False:
                staged_iso_set.add(child)
            elif child in staged_nodes and child.is_boundary is True:
                staged_vfs_set.add(child)
                staged_nodes.remove(child)
        for node in staged_nodes:
            staged_vfs_set.add(node)
        # locate the boundary node - could change to next((child for child in root.children if child.is_boundary), None)
        vfs_root = root.children[-1] if root.children[-1].is_boundary else None
        if vfs_root is None:
            raise ValueError('No boundary node found')
        try:
            with (
                open(output_path, 'wb') as dst,
            ):  # open private handle
                task_handle.progress.emit(0)
                task_handle.log_message.emit('Starting to write ISO to disk...')
                self._rebuild_iso_fs(dst, root, staged_iso_set, slimmed_rebuild_requested, task_handle)
                self._rebuild_vfs(dst, vfs_root, staged_vfs_set, task_handle)
            task_handle.progress.emit(100)
            return True

        except Exception as e:
            logger.error(f'Rebuild failed: {e}', exc_info=True)
            if output_path.exists() and output_path != self.source:
                try:
                    output_path.unlink()
                    logger.info(f'Removed partial output: {output_path.name}')
                except OSError as err:
                    logger.error(f'Could not remove partial output: {err}')
            return False


    def _rebuild_vfs(
        self,
        dst,
        root: VfsNode,
        staged_set: set[VfsNode],
        task_handle: TaskHandle,
    ) -> None:
        '''Rebuilds the virtual filesystem (the section of the ISO after the TOC)'''

        toc_lba = self.params.toc_offset // self.params.sector_size
        toc_size = self.params.total_entries * 3 * 4
        # Copy pre-TOC
        src.seek(0)
        self._stream_copy(src, dst, self.params.toc_offset)
        # Reserve TOC space
        dst.write(b'\x00' * toc_size)
        # Start sequential build
        new_lba_map: dict[VfsNode, int] = {}
        current_offset = self.params.toc_offset + toc_size
        for idx, child in enumerate(root.children):
            task_handle.checkpoint()
            orig_lba = self.toc[idx]['lba'] if idx < len(self.toc) else 0
            # TOC self-reference, built in _build_toc
            if idx == 0:
                new_lba_map[child] = toc_lba
                continue
            # NULL entries
            if child.size == 0 and not (
                child in staged_set and child.pending_data is not None
            ):
                new_lba_map[child] = 0
                continue
            # Sentinel entries
            if orig_lba == -1:
                new_lba_map[child] = orig_lba
            # Entries with data
            data = (
                child.pending_data
                if child in staged_set and child.pending_data is not None
                else self._read_node_from(src, child)
            )
            if not data:
                logger.warning(f'No data for {child.name} (idx {idx})')
                new_lba_map[child] = 0
                continue
            new_lba_map[child] = current_offset // self.params.sector_size
            dst.write(data)
            padding = (-len(data)) & (self.params.sector_size - 1)
            if padding:
                dst.write(b'\x00' * padding)
            current_offset += len(data) + padding

            if idx % 50 == 0:
                pct = int((idx / self.params.total_entries) * 90)
                task_handle.progress.emit(
                    pct, f'Writing file {idx}/{self.params.total_entries}'
                )
        # Verify the TOC
        new_toc = self._build_toc(root.children, staged_set, new_lba_map)
        new_sig = struct.unpack_from('<I', new_toc, 0)[0]
        if new_sig != self.params.signature: # Honestly could probably just overwrite the signature to force it to match
            raise ValueError(
                f'TOC signature mismatch. Expected {hex(self.params.signature)} got: {hex(new_sig)}. Entry 0 LBA self-reference reconstruction failed.'
            )
        dst.seek(self.params.toc_offset)
        dst.write(new_toc)


    def _rebuild_iso_fs(
        self,
        dst,
        root:  VfsNode,
        staged_set: set[VfsNode],
        slimmed_rebuild_requested: bool,
        task_handle: TaskHandle
    ) -> None:
        '''Rebuilds the ISO filesystem (the section of the ISO before the TOC)'''
        if self.pvd is None:
            raise ValueError('PVD not found during rebuild.')
        # Copy everything up to the PVD
        dst.write(src(0, self.pvd.lba * self.params.sector_size))
        # Copy the PVD as a pre-allocation (will need to rewrite this with new lbas and sizes)
        root_dir_offset = dst.tell()
        dst.write(b'\x00' * self.pvd.file_size)
        # Write the PVD entries (iso fs) to disk, in physical order not index order
        iso_nodes = [child for child in root.children if not child.is_boundary]
        iso_nodes.sort(key=lambda node: node.offset)
        new_lba_map: dict[VfsNode, int] = {}
        new_size_map: dict[VfsNode, int] = {}

        for node in iso_nodes:
            task_handle.checkpoint()
            current_lba = dst.tell() / self.params.sector_size
            new_lba_map[node] = current_lba
            if node in staged_set and node.pending_data is not None:
                data = node.pending_data
            else:
                data = self.get_raw_node(node)
            dst.write(data)
            new_lba_map[node] = current_lba
            new_size_map[node] = len(data)
            padding = (-len(data)) & (self.params.sector_size - 1)
            if padding:
                dst.write(b'\x00' * padding)
        current_offset = dst.tell()
        if slimmed_rebuild_requested:
            new_toc_offset = current_offset
            self.params.toc_offset = new_toc_offset
            elf_node = next((n for n in iso_nodes if n.name.startswith('SLUS') or n.name.startswith('SLPM')), None)
            if elf_node and self.toc_location != -1:
                hi_val = (new_toc_offset >> 16) & 0xFFFF
                lo_val = new_toc_offset & 0xFFFF

                elf_new_offset = new_lba_map[elf_node] * self.params.sector_size
                dst.seek(elf_new_offset + self.toc_location - 4)
                dst.write(struct.pack('<H', hi_val))
                dst.seek(elf_new_offset + self.toc_location + 4)
                dst.write(struct.pack('<H', lo_val))
            dst.seek(0, 2) # seek EOF
        else:
            if current_offset < self.params.toc_offset:
                src.seek(current_offset)
                self._stream_copy(src, dst, self.params.toc_offset - current_offset)
            else:
                logger.warning('ISO data exceeded original TOC offset. Forcing slimmed build behavior.')
                self.params.toc_offset = current_offset
        self._update_root_dir(src, dst, root_dir_offset, iso_nodes, new_lba_map, new_size_map)

    def _update_root_dir(
        self,
        src,
        dst,
        root_dir_offset: int,
        iso_nodes: list[VfsNode],
        lba_map: dict[VfsNode, int],
        size_map: dict[VfsNode, int]) -> None:
        if not self.pvd:
            raise ValueError('PVD not found')
        src.seek(self.pvd.lba * self.params.sector_size)
        dir_data = bytearray(src.read(self.pvd.file_size))
        bytes_read = 0
        while bytes_read < self.pvd.file_size:
            entry_length = dir_data[bytes_read]
            if not entry_length:
                bytes_read += 1
                continue
            name_len = dir_data[bytes_read + 32]
            raw_name = dir_data[bytes_read + 33:bytes_read + 33 + name_len]

            if raw_name not in (b'\x00', b'\x01'):
                file_name = raw_name.decode('ascii', errors='replace').split(';')[0]
                name, sep, ext = file_name.rpartition('.')
                matching_node = next((n for n in iso_nodes if n.name == name and n.extension == sep + ext), None)
                if matching_node:
                    new_lba = lba_map.get(matching_node, 0)
                    new_size = size_map.get(matching_node, 0)
                    struct.pack_into('<I', dir_data, bytes_read + 2, new_lba)
                    struct.pack_into('>I', dir_data, bytes_read + 6, new_lba)
                    struct.pack_into('<I', dir_data, bytes_read + 10, new_size)
                    struct.pack_into('>I', dir_data, bytes_read + 14, new_size)
            bytes_read += entry_length
        dst.seek(root_dir_offset)
        dst.write(dir_data)
        dst.seek(0, 2)

    def _stream_copy(self, src, dst, length: int, chunk_size: int = 1024 * 1024):
        """Helper for writing out one segment or node at a time"""
        bytes_left = length
        while bytes_left > 0:
            chunk = src.read(min(bytes_left, chunk_size))
            if not chunk:
                break
            dst.write(chunk)
            bytes_left -= len(chunk)

    def _build_toc(
        self,
        children: list[VfsNode],
        staged_set: set[VfsNode],
        lba_map: dict[VfsNode, int],
    ) -> bytes:
        """Scan Nodes to build new toc"""
        total = self.params.total_entries
        toc = [0] * (total * 3)

        for i, child in enumerate(children):
            if i >= total:
                break

            if i == 0:  # filter out toc entry (self reference)
                toc[i] = self.params.signature ^ self.params.seed
            else:
                toc[i] = lba_map.get(child, 0)

            if child in staged_set and child.pending_data:  # use new or existing data
                size_bytes = len(child.pending_data)
            else:
                size_bytes = child.size

            toc[total + i] = (
                0 if size_bytes == 0 else -(-size_bytes // self.params.sector_size)
            )
            toc[2 * total + i] = self.toc[i]['logical_id']

        scrambled = self._scramble(toc)
        return struct.pack(f'<{total * 3}I', *scrambled)

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
        chunk_size = (16 * 1024 * 1024) # 16MB chunks
        bytes_read = 0
        while (chunk := self.handle.pread(size=chunk_size, offset=bytes_read)):
            task_handle.checkpoint()
            hasher.update(chunk)
            bytes_read += len(chunk)
            task_handle.progress.emit(int(bytes_read / file_size * 100))
        digest = hasher.hexdigest()
        build = _KNOWN_BUILDS.get(digest, f'Modified/Unknown: {digest}')
        return build

    def _scramble(self, flat_toc: list) -> list:
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

    def _check_pk(self, header: bytes) -> str:
        offset_header = int.from_bytes(header[0x10:0x14], 'little')
        pk3_magic = 0x004E000
        if offset_header % pk3_magic == 0:  # header is pk3 divisible
            return '.pk3'  # pk3 header
        return 'bin'
