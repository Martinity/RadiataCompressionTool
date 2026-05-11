'''Handle ISO related processing. Extraction, rebuilding, TOC parsing, disk verification'''
from __future__ import annotations

import struct
import xxhash
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable
from core.name_overrides import generate_name_overrides
from core.extension_overrides import generate_ext_overrides
from core.node import VfsNode
from core.contracts import PhysicalHandler 
from core.registry import Registry
import logging
logger = logging.getLogger(f'radiata.{__name__}')

_VD_SECTOR        = 16  # Primary Volume Descriptor sector
_VD_VOL_SPACE_OFF = 80  # Both-endian 'Volume Space Size' field ISO9660 §8.4.8
_KNOWN_BUILDS: dict[str, str] = {
    '7ee1ab6550739833f757ccc9db23cc36':'Prototype',
    'afb46b880ee88e93b1f2ccb417e02977':'USA release',
    'f5fbce42d0d943c01e506c7f7d7e24e2':'JPN release',
}

###------------------------------ ISO HANDLER ------------------------------------###

@Registry.register(name='Radiata Stories ISO Handler', extensions=('.iso',))
class IsoHandler(PhysicalHandler):
    '''Responsible for loading the ISO and TOC related operations'''
    @dataclass(slots=True)
    class IsoParameters:
        '''Hardcoded disk parameters'''
        seed: int = 0x13578642
        signature: int = 0x27D51556 # raw scrambled TOC self-reference
        toc_offset: int = 0x3C6C1800
        total_entries: int = 0x1200
        sector_size: int = 0x800

    class FileCategories:
        '''Known File Categories'''
        _CATEGORIES = {
            range(8, 17):       'FMV',
            range(3, 4):        'Audio',
            range(42, 181):     'Audio',
            range(188, 189):    'Audio',
            range(184, 188):    'Script',
            range(204, 205):    'Script',
            range(4, 5):        'Texture',
            range(26, 27):      'Texture',
            range(189, 204):    'Texture',
            range(205, 207):    'Texture',
            range(207, 1206):   'Map',
            range(1206, 1511):  'Character',
            range(1511, 1688):  'Monster',
            range(1688, 1939):  'Prop',
            range(1939, 2126):  'Equipment',
            range(2126, 2426):  'VFX',
            range(2426, 3426):  'Scene Setup',
            range(3426, 3629):  'Animation',
            range(3730, 4151):  'Battle Animation',

            range(0, 3):        'System', # boot
            range(5, 8):        'System', # datacenter/SO3
            range(18, 26):      'System', # stats
            range(27, 42):      'System', # core/debug
            range(182, 184):    'System', # game
        }
        @classmethod
        def get_category(cls, index: int) -> str:
            '''Return semantic file category'''
            for index_range, name in cls._CATEGORIES.items():
                if index in index_range:
                    return name
            return "Unknown"
        
    class DatacenterTargets:
        '''Kods datacenter targets'''
        _TARGET_STATIC = { # format: [disk index]:[Datacenter header HIDs]
            186: (5, 0), 204: (5, 1),
            185: (5, 7),  187: (5, 9),
            203: (5, 10), 189: (5, 11), 190: (5, 12),
            191: (5, 13), 192: (5, 14), 193: (5, 15),
            194: (5, 16), 195: (5, 17), 196: (5, 18),
            197: (5, 19), 198: (5, 20), 199: (5, 21),
            200: (5, 22), 201: (5, 23), 206: (5, 25),
            179: (5, 26), 178: (5, 27), 177: (5, 28),
            176: (5, 29), 180: (5, 30), 188: (5, 31)
        }
        _TARGET_MAP = [ # format: [Start Idx, End Idx, HID prefix, Number of Header Idxs]
            (1206, 1511, (5, 2, 0), 10),
            (1511, 1682, (5, 3, 0), 10),
            (1688, 1939, (5, 4, 0), 10),
            (1939, 2126, (5, 5, 0), 10),
            (2126, 2426, (5, 6, 0), 10),
        ]
        @classmethod
        def get_target(cls, disk_index: int) -> list[tuple[int,...]] | None:
            '''Return the datacenter header HID(s) for the datacenter target'''
            # Single datacenter header
            if disk_index in cls._TARGET_STATIC:
                return [cls._TARGET_STATIC[disk_index]]

            # Multiple datacenter headers
            for start, end, hid_prefix, steps in cls._TARGET_MAP:
                if start <= disk_index < end:
                    base_val = (disk_index - start) * steps
                    return [hid_prefix  + (j + base_val,) for j in range(steps)]
        
            return None

    def __init__(self, iso_path: Path, parent=None):
        '''Initialize iso properties'''
        super().__init__(iso_path)
        logger.info(f"IsoHandler initialized for {iso_path.name}")
        self.source = iso_path
        self.params = self.IsoParameters()
        self.status = 'Unverified'
        self.toc = self._process_toc(self._load_toc())
        logger.debug(f'TOC loaded: {self.params.total_entries} entries')

    def __repr__(self) -> str:
        return f'Build:{self.status}'
    
###------------------------------------ Extract ISO ------------------------------------###

    def get_file_tree(self) -> VfsNode:
        '''Returns the root node of the VFS (the disk)'''
        logger.debug("Building VFS tree from TOC")
        root = VfsNode(name='Radiata Stories ISO')

        semantic_names: dict[int, str] = generate_name_overrides()
        extension_dict: dict[bytes, str] = generate_ext_overrides()

        for entry in self.toc:
            disk_index = entry['id']
            offset = entry['offset'] if disk_index != 0 else self.params.toc_offset
            self.handle.seek(offset)
            if entry['size'] == 0: # Dummy node
                dummy_node = VfsNode(
                    name=f'sentinel_{disk_index:04d}',
                    offset=offset,
                    size=0,
                    parent=root
                )
                dummy_node.is_hidden = True
                root.append_child(dummy_node)
                continue

            # Real node
            header: bytes = self.handle.read(32)
            ext: str = next((match for sig, match in extension_dict.items() if header.startswith(sig)), '.bin')
            category: str = self.FileCategories.get_category(disk_index)
            semantic_name: str | None = semantic_names.get(disk_index, entry['name'])
            target: list[tuple] | None = self.DatacenterTargets.get_target(disk_index)
            ext = '.kods' if target else ext

            node = VfsNode(
                name=f'{semantic_name}{ext}',
                category=category,
                offset=offset,
                size=(entry['size'] * self.params.sector_size),
                parent=root,
                header=header,
                extension='.kods' if target else ext,
                target=target
            )
            node.is_physical = True  # Set as reference node for all file processes
            root.append_child(node)
            if disk_index in [0, 5]: # Hide file system nodes
                node.is_hidden = True
        logger.info(f"Tree built — {len(root.children)} valid files")
        return root
    
    def get_raw_node(self, node: VfsNode) -> bytes:
        """Called for the raw data of a physical node"""
        self.handle.seek(node.offset)
        data = self.handle.read(node.size)

        logger.debug(f'Read {len(data) // self.params.sector_size} sectors from offset {hex(node.offset)}')
        return data
    
    ###----------------------------- TOC Parsing ---------------------------------###

    def _load_toc(self) -> bytes:
        """Locate the TOC."""
        # Check for radiata ISO
        self.handle.seek(self.params.toc_offset)
        signature = struct.unpack('<I', self.handle.read(4))[0]
        if self.params.signature != signature:
            raise ValueError(f'Not a Radiata Stories Iso. Bad signature at TOC offset: {hex(signature)}')
        self.handle.seek(self.params.toc_offset)
        return self.handle.read(self.params.total_entries * 3 * 4)

    def _process_toc(self, scrambled_toc: bytes) -> list[dict[str, Any]]:
        '''Unscramble and structure the TOC data'''
        total = self.params.total_entries
        toc = list(struct.unpack(f"<{total * 3}I", scrambled_toc))
        toc = self._scramble(toc[:])

        structured = []
        for i in range(total):
            lba = toc[i]
            size = toc[total + i]
            logical_id = toc[(total * 2) + i]
            structured.append({
                "id": i,
                "lba": lba,
                "size": size,
                "offset": lba * self.params.sector_size,
                'logical_id': logical_id,
                "name": f"FILE_{i:04d}.bin"
            })
        return structured

###----------------------------------- Build ISO ------------------------------------------###

    def rebuild_node(self, root: VfsNode, staged_nodes: list[VfsNode], output_path: Path, progress_callback: Callable[[int, str], None] | None = None) -> bool:
        '''Rebuilds the ISO, preserving physical ordering, aliasing, and system file integrity.'''
        if output_path.resolve() == self.source.resolve():
            raise ValueError('Cannot overwrite source ISO')
        logger.info(f'Rebuilding ISO to {output_path}')
        toc_lba = self.params.toc_offset // self.params.sector_size
        toc_size = self.params.total_entries * 3 * 4
        staged_set = set(staged_nodes)
        try:
            with open(output_path, 'wb') as f:
                if progress_callback:
                    progress_callback(0, 'Initialized ISO rebuild...')
                # Copy pre-TOC
                self.handle.seek(0)
                self._stream_copy(self.handle, f, self.params.toc_offset)
                # Reserve TOC space
                f.write(b'\x00' * toc_size)
                # Start sequential build
                new_lba_map: dict[VfsNode, int] = {}
                current_offset = self.params.toc_offset + toc_size
                for idx, child in enumerate(root.children):
                    orig_lba = self.toc[idx]['lba'] if idx < len(self.toc) else 0
                    # TOC self-reference, built in _build_toc
                    if idx == 0:
                        new_lba_map[child] = toc_lba
                        continue
                    # NULL entries
                    if child.size == 0 and not (child in staged_set and child.pending_data):
                        new_lba_map[child] = 0
                        continue
                    # Sentinel entries
                    if orig_lba == -1:
                        new_lba_map[child] = orig_lba
                    # Entries with data
                    data = (
                        child.pending_data 
                        if child in staged_set and child.pending_data
                        else self.get_raw_node(child)
                    )
                    if not data:
                        logger.warning(f'No data for {child.name} (idx {idx})')
                        new_lba_map[child] = 0
                        continue
                    new_lba_map[child] = current_offset // self.params.sector_size
                    f.write(data)
                    padding = (-len(data)) & (self.params.sector_size - 1)
                    if padding:
                        f.write(b'\x00' * padding)
                    current_offset += len(data) + padding

                    if progress_callback and idx % 50 == 0:
                        pct = int((idx / self.params.total_entries) * 90)
                        progress_callback(pct, f'Writing file {idx}/{self.params.total_entries}')
                # Verify the TOC
                new_toc = self._build_toc(root.children, staged_set, new_lba_map)
                new_sig = struct.unpack_from('<I', new_toc, 0)[0]
                if new_sig != self.params.signature:
                    raise RuntimeError(f'TOC signature mismatch. Expected {hex(self.params.signature)} got: {hex(new_sig)}. Entry 0 LBA reconstruction failed.')
                f.seek(self.params.toc_offset)
                f.write(new_toc)

                # Patch ISO9660 Volume Descriptor
                total_sectors = current_offset // self.params.sector_size
                f.seek(_VD_SECTOR * self.params.sector_size + _VD_VOL_SPACE_OFF)
                f.write(total_sectors.to_bytes(4, 'little') + total_sectors.to_bytes(4, 'big'))
            
            if progress_callback:
                progress_callback(100, 'Rebuild complete!')
            
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

    def _stream_copy(self, source_handle, output_obj, length, chunk_size = 1024*1024):
        '''Helper for writing out one segment or node at a time'''
        bytes_left = length
        while bytes_left > 0:
            chunk = source_handle.read(min(bytes_left, chunk_size))
            if not chunk:
                break
            output_obj.write(chunk)
            bytes_left -= len(chunk)

    def _build_toc(self, children: list[VfsNode], staged_set: set[VfsNode], lba_map: dict) -> bytes:
        '''Scan Nodes to build new toc'''
        total = self.params.total_entries
        toc = [0] * (total * 3)

        for i, child in enumerate(children):
            if i >= total:
                break
            
            if i == 0: # filter out toc entry (self reference)
                toc[i] = self.params.signature ^ self.params.seed
            else:
                toc[i] = lba_map.get(child, 0)

            if child in staged_set and child.pending_data: # use new or existing data
                size_bytes = len(child.pending_data)
            else:
                size_bytes = child.size

            toc[total + i] = 0 if size_bytes == 0 else -(-size_bytes // self.params.sector_size)
            toc[2 * total + i] = self.toc[i]['logical_id']

        scrambled = self._scramble(toc)
        return struct.pack(f'<{total * 3}I', *scrambled)

###---------------------------------- Utility -------------------------------------------###
 
    def verify_iso_integrity(self) -> str:
        '''Verify radiata iso. Check what version of the disk is running.'''
        logger.debug('Verifying ISO integrity')
        # Check hash against known hashes
        self.handle.seek(0)
        hasher = xxhash.xxh128()
        while chunk := self.handle.read(16 * 1024 * 1024): # read in 16MB chunks
            hasher.update(chunk)
        digest = hasher.hexdigest()
        build = _KNOWN_BUILDS.get(digest, 'Modified/Unknown')
        return build
    
    def _scramble(self, flat_toc: list) -> list:
        '''scramble or unscramble the toc'''
        total = self.params.total_entries
        key = self.params.seed
        scramble = flat_toc[:]

        for i in range(total):
            scramble[0*total + i] ^= key
            key ^= (key << 1) & 0xFFFFFFFF
            scramble[1*total + i] ^= key
            key ^= (~self.params.seed) & 0xFFFFFFFF
            scramble[2*total + i] ^= key    
            key ^= ((key << 2) ^ self.params.seed) & 0xFFFFFFFF

        return scramble