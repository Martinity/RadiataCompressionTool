from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from core.contracts import BaseHandler
from core.extension_overrides import generate_ext_overrides
from core.registry import Registry
from core.node import VfsNode

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###-------------------------------------------- Wrapper ----------------------------------------###

@Registry.register(
    name='Kods Archiver',
    extensions=('.kods',),
    supported_actions=('Properties', 'Unpack'))
class KodsHandler(BaseHandler):
    '''Wrapper for Kods archiver class'''
    def __init__(self, source, parent):
        super().__init__(source)
        self.handler_parent = parent
        self.handle.seek(0)
        self.raw_kods = self.handle.read()
        self.archiver = KodsArchiver(self.raw_kods)


    def get_file_tree(self) -> VfsNode:
        logger.debug(f'Unpacking Kods archive from {getattr(self.handler_parent, 'name',  'unknown')}')

        new_nodes = self.archiver.unpack_kods()
        offsets = self.archiver.get_offsets()
        extensions = generate_ext_overrides()

        root = VfsNode(
            name=f"{getattr(self.handler_parent, 'name', 'KODS')}_contents",
            category=getattr(self.handler_parent, 'category', 'Unknown'),
            parent=self.handler_parent,
        )

        for i, file in enumerate(new_nodes):
            if not file or offsets[i] == -1:
                continue

            header = file[:8]
            ext = next((ext for signature, ext in extensions.items() if header.startswith(signature)), '.bin')

            node = VfsNode(
                name=f"{getattr(self.handler_parent, 'name', 'Unknown')} {i:04d} Unpacked",
                offset=offsets[i],
                size=len(file),
                header=header,
                extension=ext,
                parent=root,
            )
            root.append_child(node)

        logger.info(f'Kods unpacked successfully {len(root.children)} files created')
        return root
        
    
    def get_raw_node(self, node: VfsNode) -> bytes:
        offset = node.offset
        end = offset + node.size
        logger.info(f'Requested offset {offset} of size {end}')
        return self.raw_kods[offset:end]

    def rebuild_node(self, node: VfsNode) -> bytes:
        return b''

    def get_properties(self):
        p = self.archiver.parse_header()
        mode_str = '32bit aligned' if not p.mode else '16bit aligned'

        logger.info(f'Kods Archive Properties:\nNumber of Entries: {p.num_entries} '
                    f'| Compression shift: {p.shift} | Entry mode: {mode_str} '
                    f'| Secondary table present: {p.has_second_table} | Size of Pre-Payload data: {p.data_region_start}')
    
    def execute_action(self, node: VfsNode, action_name: str) -> Any:
        if action_name == 'Unpack':
            return self.get_file_tree()
        elif action_name == 'Properties':
            return self.get_properties()
        return None
    
    def get_identity(self) -> str:
        return 'Kods Archive'

###-------------------------------- Archiver -------------------------------------------###

class KodsArchiver():
    '''Archiver class for all kods archive related processing.'''
    @dataclass(slots=True)
    class KodsHeader:
        num_entries: int
        shift: int
        mode: bool
        stride: int
        has_second_table: bool
        bit30: bool
        sentinel: int
        format: str
        header_size: int = 0
        data_region_start: int = 0

    def __init__(self, data: bytes, target: int | None = None):
        self.raw_kods = data
        self.target = target
        self.header = self.parse_header()

    ###--------------------------------- Pack --------------------------------###

    def pack_archive(self):
        '''TODO: Implement for ISO building support'''
        raise NotImplementedError('Kodes repacking not implemented yet')

    ###-------------------------------- Unpack --------------------------------###

    def unpack_kods(self) -> list[bytes]:
        '''Unpack kods container into list of raw data nodes'''
        offsets = self.get_offsets()
        new_files: list[bytes] = []

        for i, offset in enumerate(offsets[:-1]):
            if offset == -1: 
                continue
            end = -1
            for j in range(i+1, len(offsets)):
                if offsets[j] != -1:
                    end = offsets[j]
                    break
        
            if end == -1:
                data_end = len(self.raw_kods)
                while data_end > offset + 1 and self.raw_kods[data_end - 1] == 0:
                    data_end -= 1
                if data_end > offset:
                    align_mask = (1 << self.header.shift) - 1
                    aligned = (data_end + align_mask) & ~align_mask
                    end = self.header.data_region_start + aligned
                    end = min(end, len(self.raw_kods))
                else:
                    end = offset

            if offset >= end:
                # No reason to keep or show 0 entry that I know of. Repack happens based on the original.
                continue

            file = self.raw_kods[offset:end]
            new_files.append(file)
        
        return new_files

    def get_offsets(self) -> list[int]:
        '''Return list of (offset, alias) for files inside the Kods container'''
        offsets = []
        for i in range(self.header.num_entries):
            offset_pos = self.header.header_size + (i * self.header.stride)
            raw_offset = struct.unpack_from(self.header.format, self.raw_kods, offset_pos)[0]
            # Collect offset data
            if raw_offset == self.header.sentinel:
                abs_offset = -1
            elif raw_offset == 0:
                abs_offset = self.header.data_region_start
            else:
                abs_offset = self.header.data_region_start + (raw_offset << self.header.shift)
            offsets.append(abs_offset)
        offsets.append(len(self.raw_kods))
        return offsets

###-------------------------------------- Utility ----------------------------------------###

    def get_aliases(self) -> list[int]:
        '''Return ordered list of aliases'''
        aliases = []
        for i in range(self.header.num_entries):
            # Collect Secondary table data
            secondary_id = None
            if self.header.has_second_table:
                secondary_table_start = ((self.header.data_region_start - 8) // 2) + 8
                secondary_pos = secondary_table_start + (i * self.header.stride)
                secondary_id = struct.unpack_from(self.header.format, self.raw_kods, secondary_pos)[0]
            aliases.append(secondary_id)
        return aliases

    def parse_header(self) -> KodsHeader:
        '''Return dataclass with all header definitions'''
        if len(self.raw_kods) < 8:
            raise ValueError('Only partial Kods')

        magic, control_word = struct.unpack("<II", self.raw_kods[:8])
        if magic != 0x73646F4B: # 'Kods'
            raise ValueError('Not Kods archive')

        # Bit Field Extraction
        num_entries = control_word & 0xFFFF              # Bits 0-15
        shift = (control_word >> 16) & 0x0F              # Bits 16-19
        mode = (control_word >> 20) & 0x01               # Bits 20
        has_second_table = (control_word >> 29) & 0x01   # Bit 29
        bit30 = (control_word >> 30) & 0x01              # Bit 30
        data_format = "<H" if mode else "<I"
        stride = 2 if mode else 4
        sentinel = 0xFFFF if mode else 0xFFFFFFFF

        # Data alignement
        header_size = 8
        table_count = 2 if has_second_table else 1
        data_region_start =  header_size + (num_entries * stride * table_count)

        return self.KodsHeader(
            num_entries,
            shift,
            bool(mode),
            stride,
            bool(has_second_table),
            bool(bit30),
            sentinel,
            data_format,
            header_size,
            data_region_start
        )





