'''Handle ISO related processing. Extraction, rebuilding, TOC parsing, disk verification'''
import struct
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


    def __init__(self, iso_path: Path, parent=None):
        '''Initialize iso properties'''
        super().__init__(iso_path)
        logger.info(f"IsoHandler initialized for {iso_path.name}")
        self.params = self.IsoParameters()
        self.status = self.verify_iso_integrity()

        self.toc = self._unscramble(self._load_toc())
        logger.debug(f"TOC signature verified, {self.params.total_entries} entries")

    def __repr__(self) -> str:
        return f'Build:{self.status}'
    
    def get_file_tree(self) -> VfsNode:
        '''Returns the root node of the VFS (the disk)'''
        logger.debug("Building VFS tree from TOC")
        root = VfsNode(name='Radiata Stories ISO')
        logger.info(f"Tree built — {len(self.toc)} valid files")

        semantic_names: dict[int, str] = generate_name_overrides()
        extension_dict: dict[bytes, str] = generate_ext_overrides()

        for entry in self.toc: # TOC entry -> Node
            disk_index = entry['id']
            self.handle.seek(entry['offset'])
            
            header = self.handle.read(32)
            ext = next((ext for signature, ext in extension_dict.items() if header.startswith(signature)), '.bin')
            category = self.FileCategories.get_category(disk_index)
            semantic_name = semantic_names.get(disk_index, entry['name'])
            
            node = VfsNode(
                name=semantic_name,
                category=category,
                offset=entry['offset'],
                size=(entry['size'] * self.params.sector_size),
                parent=root,
                header=header,
                extension=ext
            )
            node.is_physical = True  # Set as reference node for all file processes
            root.append_child(node)
        return root

    def _load_toc(self) -> bytes:
        """Locate the TOC."""
        self.handle.seek(self.params.toc_offset)
        return self.handle.read(self.params.total_entries * 3 * 4)

    def _unscramble(self, scrambled_toc: bytes) -> list[dict[str, Any]]:
        '''Unscramble and structure the TOC data'''
        total = self.params.total_entries
        key = self.params.seed
        flat = list(struct.unpack(f"<{total * 3}I", scrambled_toc))

        for i in range(total):
            flat[0*total + i] ^= key
            key ^= (key << 1) & 0xFFFFFFFF
            flat[1*total + i] ^= key
            key ^= (~self.params.seed) & 0xFFFFFFFF
            flat[2*total + i] ^= key    
            key ^= ((key << 2) ^ self.params.seed) & 0xFFFFFFFF

        structured = []
        for i in range(total):
            lba = flat[i]
            size = flat[total + i]
            if size > 0: # Only keep valid files
                structured.append({
                    "id": i,
                    "lba": lba,
                    "size": size,
                    "offset": lba * self.params.sector_size,
                    "name": f"FILE_{i:04d}.bin"
                })
        return structured

    def read_file_data(self, node: VfsNode, absolute_offset) -> bytes:
        """The UI calls this ONLY when it needs the bytes for Hex view/export."""
        self.handle.seek(absolute_offset)
        data = self.handle.read(node.size)

        logger.debug(f'Read {len(data)} bytes from offset {absolute_offset}')
        return data

    def rebuild_file_data(self, node: VfsNode) -> bytes:
        '''TODO'''
        return b''

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
    