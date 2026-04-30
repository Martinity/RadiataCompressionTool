'''Handle ISO related processing. Extraction, rebuilding, TOC parsing, disk verification'''
from __future__ import annotations

import struct
import math
from pathlib import Path
from dataclasses import dataclass
from hashlib import sha1
from typing import Any
from core.name_overrides import generate_name_overrides
from core.extension_overrides import generate_ext_overrides
from core.node import VfsNode
from core.contracts import BaseHandler 
from core.registry import Registry
import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------ ISO HANDLER ------------------------------------###

@Registry.register(name='Radiata Stories ISO Handler', extensions=('.iso',))
class IsoHandler(BaseHandler):
    '''Responsible for loading the ISO and TOC related operations'''
    @dataclass(slots=True)
    class IsoParameters:
        '''Hardcoded disk parameters'''
        seed: int = 0x13578642
        signature: int = 0x27D51556
        toc_offset: int = 0x3C6C1800
        total_entries: int = 0x1200
        sector_size: int = 0x800

    @dataclass(slots=True)
    class IsoHashes:
        '''Known Iso hashes SHA-1'''
        full_builds = {
            'a246683053bad605a59d9977c52005f99a4e7482':'Prototype Build',
            '33d789469fa09d39c9ea34d19ea676409de525f9':'USA release Build',
            }
        toc_builds = {
            '9d7caf77ec6e354a79586a07772f0628d44318ab':'USA release Build',
        }

    class FileCategories:
        '''Known File Categories'''
        _CATEGORIES = {
            range(8, 17):       'FMV',
            range(42, 176):     'Audio',
            range(207, 1206):   'Map',
            range(1206, 1511):  'Character',
            range(1511, 1688):  'Monster',
            range(1688, 1939):  'Prop',
            range(1939, 2126):  'Equipment',
            range(2126, 2426):  'VFX',
            range(2426, 3426):  'Scene Setup',
            range(3426, 3629):  'Animation',
            range(3730, 4151):  'Battle Animation',

            range(0, 8):        'System', # boot
            range(18, 42):      'System', # core
            range(177, 207):    'System'  # game
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
            185: (5, 7),  181: (5, 8),  187: (5, 9),
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
        def get_target(cls, disk_index: int) -> list[tuple] | None:
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
        self.params = self.IsoParameters()
        self.status = self.verify_iso_integrity()

        self.toc = self._process_toc(self._load_toc())
        logger.debug(f"TOC signature verified, {self.params.total_entries} entries")

    def __repr__(self) -> str:
        return f'Build:{self.status}'
    
###------------------------------------ Extract ISO ------------------------------------###

    def get_file_tree(self) -> VfsNode:
        '''Returns the root node of the VFS (the disk)'''
        logger.debug("Building VFS tree from TOC")
        root = VfsNode(name='Radiata Stories ISO')
        logger.info(f"Tree built — {len(self.toc)} valid files")

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
            ext: str = next((ext for signature, ext in extension_dict.items() if header.startswith(signature)), '.bin')
            category: str = self.FileCategories.get_category(disk_index)
            semantic_name: str | None = semantic_names.get(disk_index, entry['name'])
            target: list[tuple] | None = self.DatacenterTargets.get_target(disk_index)

            node = VfsNode(
                name=semantic_name,
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
        return root
    
    def get_raw_node(self, node: VfsNode) -> bytes:
        """Called for the raw data of a physical node"""
        self.handle.seek(node.offset)
        data = self.handle.read(node.size)

        logger.debug(f'Read {len(data) // self.params.sector_size} sectors from offset {hex(node.offset)}')
        return data

    def _load_toc(self) -> bytes:
        """Locate the TOC."""
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
 
    def rebuild_node(self, root: VfsNode, staged_nodes: list[VfsNode], output_path: Path) -> bool:
        '''Rebuild the root node, stream segments directly to disk. Only pre-toc is in place'''
        logger.info(f'Rebuilding ISO to {output_path}')

        try:
            toc_size = self.params.total_entries * 3 * 4
            start_data_offset = self.params.toc_offset + toc_size
            new_lba_map = {}
            current_offset = start_data_offset
            for child in root.children: # map LBAs
                new_lba_map[child] = current_offset // self.params.sector_size

                if child in staged_nodes and child.pending_data:
                    size = len(child.pending_data)
                else:
                    size = child.size

                if size > 0:
                    sector_size = (size + (self.params.sector_size - 1)) & ~(self.params.sector_size - 1)
                    current_offset += sector_size     
        
            with open(output_path, 'wb') as f: # write the new ISO
                self.handle.seek(0)
                self._stream_copy(self.handle, f, self.params.toc_offset)

                new_toc_bytes = self._build_toc(root.children, staged_nodes, new_lba_map)
                f.write(new_toc_bytes)

                for child in root.children:
                    if child.size == 0 and not child.pending_data:
                        continue
                    data = child.pending_data if child in staged_nodes else self.get_raw_node(child)
                    f.write(data)
                    padding_size = (-len(data)) & (self.params.sector_size -1)
                    f.write(b'\x00' * padding_size)

            return True
        
        except Exception as e:
            logger.error(f'Rebuild failed: {e}', exc_info=True)
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

    def _build_toc(self, children: list[VfsNode], staged_nodes: list[VfsNode], lba_map: dict) -> bytes:
        '''Scan Nodes to build new toc'''
        total = self.params.total_entries
        toc = [0] * (total * 3)

        for i, child in enumerate(children):
            if i >= total:
                break
            
            final_lba = lba_map.get(child, 0)

            if child in staged_nodes and child.pending_data:
                size_bytes = len(child.pending_data)
            else:
                size_bytes = child.size

            final_sectors = 0 if size_bytes == 0 else math.ceil(size_bytes / self.params.sector_size)
            toc[i] = final_lba
            toc[total + i] = final_sectors
            toc[2 * total + i] = self.toc[i]['logical_id']

        scrambled = self._scramble(toc)
        return struct.pack(f'<{total * 3}I', *scrambled)

###---------------------------------- Utility -------------------------------------------###
 
    def verify_iso_integrity(self) -> str:
        '''Verify radiata iso. Check what version of the disk is running.'''
        logger.debug("Verifying ISO integrity (SHA-1)")
        # Check for radiata ISO
        self.handle.seek(self.params.toc_offset)
        signature = struct.unpack('<I', self.handle.read(4))[0]
        if self.params.signature != signature:
            return 'Not a Radiata Stories Iso'
        # Check hash against known hashes
        self.handle.seek(self.params.toc_offset)
        sha1_hash = sha1()
        i = 0
        while chunk := self.handle.read(4096):
            sha1_hash.update(chunk)
            if i > 4: 
                break
            i += 1
        final_hash = sha1_hash.hexdigest()
        if final_hash in self.IsoHashes().toc_builds:
            logger.info(f"ISO identified as: {self.IsoHashes.toc_builds[final_hash]}")
            return self.IsoHashes.toc_builds[final_hash]
        logger.info("ISO identified as: Modified/Unknown ISO Build")
        return 'Modified/Unknown ISO build'

    def get_identity(self) -> str:
        return 'ISO detected'
    
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