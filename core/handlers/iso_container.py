"""PhysicalHandler ISO related processing. Extraction, rebuilding, TOC parsing, disk verification"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import xxhash
from core.contracts import PhysicalHandler
from core.extension_overrides import lookup_extension
from core.name_overrides import generate_name_overrides
from core.node import VfsNode
from core.registry import Registry
from core.workers import TaskHandle

logger = logging.getLogger(f'radiata.{__name__}')

_VD_SECTOR = 16  # Primary Volume Descriptor sector
_VD_VOL_SPACE_OFF = 80  # Both-endian 'Volume Space Size' field ISO9660 §8.4.8
_KNOWN_BUILDS: dict[str, str] = {
    '7ee1ab6550739833f757ccc9db23cc36': 'Prototype',
    'afb46b880ee88e93b1f2ccb417e02977': 'USA release',
    'f5fbce42d0d943c01e506c7f7d7e24e2': 'JPN release',
}


@dataclass(frozen=True)
class RootDirectoryStructure:
    entry_length: int
    extended_attribute: int
    lba: int
    file_size: int
    date: bytes
    flag: int
    interleave_gap: int
    volume_sequence_number: int
    filename_length: int
    file_name: str

    @classmethod
    def from_bytes(cls, record_data: bytes) -> RootDirectoryStructure:
        # Unpack the entry
        entry_length = record_data[0]
        ext_attr = record_data[1]
        lba = struct.unpack('<I', record_data[2:6])[0] * 0x800
        file_size = struct.unpack('<I', record_data[10:14])[0]
        date_bytes = record_data[18:25]
        flags = record_data[25]
        gap_size = record_data[27]
        vol_seq = struct.unpack('<H', record_data[28:30])[0]
        name_len = record_data[32]
        raw_name = record_data[33 : 33 + name_len]
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

    @classmethod
    def to_bytes(cls, record: RootDirectoryStructure) -> bytes:

        return bytes()


###------------------------------ ISO HANDLER ------------------------------------###


@Registry.register(name='Radiata Stories ISO Handler', extensions=('.iso',))
class IsoHandler(PhysicalHandler):
    """Responsible for loading the ISO and TOC related operations"""

    @dataclass(slots=True)
    class IsoParameters:
        """Hardcoded disk parameters"""

        seed: int = 0x13578642
        signature: int = 0x27D51556  # raw scrambled TOC self-reference
        toc_offset: int = 0x3C6C1800
        total_entries: int = 0x1200
        sector_size: int = 0x800
        iso_9660_pvd: int = 16
        root_dir_record_offset: int = 0x9C

    def __init__(self, iso_path: Path, parent=None):
        """Initialize iso properties"""
        super().__init__(iso_path)
        logger.info(f'IsoHandler initialized for {iso_path.name}')
        self.source = iso_path
        self.params = self.IsoParameters()
        self.status = 'Unverified'
        self.toc = self._process_toc(self._load_toc())
        logger.debug(f'TOC loaded: {self.params.total_entries} entries')

    def __repr__(self) -> str:
        return f'Build:{self.status}'

    ###------------------------------------ Extract ISO ------------------------------------###

    def get_iso_dir(self) -> VfsNode:
        root = VfsNode(name='dummy')
        # Read descriptor volume for root dir location
        self.handle.seek(
            (self.params.iso_9660_pvd * self.params.sector_size)
            + self.params.root_dir_record_offset
        )
        pvd_len = self.handle.read(1)[0]
        self.handle.seek(-1, 1)
        pvd = RootDirectoryStructure.from_bytes(self.handle.read(pvd_len))

        # Read root dir for files
        bytes_read = 0
        self.handle.seek(pvd.lba)
        root_dir_mm = memoryview(self.handle.read(pvd.file_size))
        if root_dir_mm[0x2E : 0x2E + 7] != b'RADIATA':
            raise ValueError(
                f'Not a Radiata Stories disk. Found {root_dir_mm[0x2E : 0x2E + 7]}, expected {b"RADIATA"}'
            )
        while bytes_read < pvd.file_size:
            entry_length = root_dir_mm[bytes_read]
            bytes_read += 1
            if not entry_length:
                break
            # Check for sector padding between entries (probably pointless...)
            if entry_length == 0:
                padding_to_skip = (-bytes_read) & (self.params.sector_size - 1)
                if padding_to_skip < self.params.sector_size:
                    bytes_read += padding_to_skip
                continue
            record_slice = root_dir_mm[bytes_read - 1 : bytes_read - 1 + entry_length]
            bytes_read += entry_length - 1
            record = RootDirectoryStructure.from_bytes(record_slice.tobytes())
            if record.file_name in ('.', '..'):
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

        found_names = {child.name for child in root.children}
        required_files = {'IOPRP300', 'SYSTEM'}
        has_system_files = required_files.issubset(found_names)
        has_executable = bool({'SLUS_212', 'SLPM_658'}.intersection(found_names))
        if not (has_system_files and has_executable):
            raise ValueError(
                'ISO needs IOPRP300, SYSTEM, and SLUS/SLPM for proper execution.'
            )

        return root

    def get_file_tree(self) -> VfsNode:
        """Returns the root node of the VFS (the disk)"""
        logger.debug('Building VFS tree from TOC')
        root = VfsNode(name='Radiata Stories ISO')

        semantic_names: dict[int, str] = generate_name_overrides()

        for entry in self.toc:
            disk_index = entry['id']
            offset = entry['offset'] if disk_index != 0 else self.params.toc_offset
            self.handle.seek(offset)
            if entry['size'] == 0:  # Dummy node
                dummy_node = VfsNode(
                    name=f'sentinel_{disk_index:04d}',
                    offset=offset,
                    size=0,
                    parent=root,
                )
                dummy_node.is_hidden = True
                root.append_child(dummy_node)
                continue

            # Real node
            header: bytes = self.handle.read(32)
            ext: str = lookup_extension(header, self._check_pk(header))
            semantic_name: str | None = semantic_names.get(disk_index, entry['name'])

            node = VfsNode(
                name=semantic_name or 'Unknown',
                category=('',),
                offset=offset,
                size=(entry['size'] * self.params.sector_size),
                parent=root,
                header=header,
                extension=ext,
                target=None,
            )
            node.is_physical = True  # Set as reference node for all file processes
            root.append_child(node)
            if disk_index in [0, 5]:  # Hide file system nodes
                node.is_hidden = True
        logger.info(f'Tree built — {len(root.children)} valid files')
        return root

    def get_raw_node(self, node: VfsNode) -> bytes:
        """Called for the raw data of a physical node with a private handle"""
        with open(self.path, 'rb') as fh:
            fh.seek(node.offset)
            data = fh.read(node.size)

        logger.debug(
            f'Read {len(data) // self.params.sector_size} sectors from offset {hex(node.offset)}'
        )
        return data

    def _read_node_from(self, file_handle, node: VfsNode) -> None:
        """Called for raw data of a physical node with an already open handle"""
        file_handle.seek(node.offset)
        return file_handle.read(node.size)

    ###----------------------------- TOC Parsing ---------------------------------###

    def _load_toc(self) -> bytes:
        """Locate the TOC."""
        # Check for radiata ISO
        self.handle.seek(self.params.toc_offset)
        signature = struct.unpack('<I', self.handle.read(4))[0]
        if self.params.signature != signature:
            raise ValueError(
                f'Not a Radiata Stories Iso. Bad signature at TOC offset: {hex(signature)}'
            )
        self.handle.seek(self.params.toc_offset)
        return self.handle.read(self.params.total_entries * 3 * 4)

    def _process_toc(self, scrambled_toc: bytes) -> list[dict[str, Any]]:
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
        node: VfsNode,
        staged_nodes: list[VfsNode],
        output_path: Path,
        task_handle: TaskHandle,
    ) -> bool:
        """Rebuilds the ISO, preserving physical ordering, aliasing, and system file integrity."""
        if output_path.resolve() == self.source.resolve():
            raise ValueError('Cannot overwrite source ISO')
        staged_set = set(staged_nodes)
        try:
            with (
                open(self.path, 'rb') as src,
                open(output_path, 'wb') as dst,
            ):  # open private handle
                task_handle.progress.emit(0, 'Initialized ISO rebuild...')
                task_handle.log_message.emit('Initialized ISO rebuild...')
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
                for idx, child in enumerate(node.children):
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
                new_toc = self._build_toc(node.children, staged_set, new_lba_map)
                new_sig = struct.unpack_from('<I', new_toc, 0)[0]
                if new_sig != self.params.signature:
                    raise ValueError(
                        f'TOC signature mismatch. Expected {hex(self.params.signature)} got: {hex(new_sig)}. Entry 0 LBA reconstruction failed.'
                    )
                dst.seek(self.params.toc_offset)
                dst.write(new_toc)

            task_handle.progress.emit(100, 'Rebuild complete!')
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

    def _stream_copy(self, source_handle, output_obj, length, chunk_size=1024 * 1024):
        """Helper for writing out one segment or node at a time"""
        bytes_left = length
        while bytes_left > 0:
            chunk = source_handle.read(min(bytes_left, chunk_size))
            if not chunk:
                break
            output_obj.write(chunk)
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
        """Verify radiata iso. Check what version of the disk is running."""
        # Check hash against known hashes
        with open(self.path, 'rb') as fh:
            hasher = xxhash.xxh128()
            while chunk := fh.read(16 * 1024 * 1024):  # read in 16MB chunks
                task_handle.checkpoint()
                hasher.update(chunk)
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
