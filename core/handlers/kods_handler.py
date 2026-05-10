from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

from core.contracts import ContainerHandler
from core.extension_overrides import generate_ext_overrides
from core.registry import Registry
from core.node import VfsNode
from core.workers import ActionDef, ActionType

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###-------------------------------------------- Wrapper ----------------------------------------###

@Registry.register(
    name='Kods Archiver',
    extensions=('.kods',),
    supported_actions={
        'Unpack': ActionDef('Unpack', ActionType.TREE_EXPAND, 'Unpack archive'),
        'Properties': ActionDef('Properties', ActionType.DIALOG, 'Properties')
})
class KodsHandler(ContainerHandler):
    '''Wrapper for Kods archiver class'''
    def __init__(self, source: bytes, parent: VfsNode, datacenter_headers: list[bytes] | None = None) -> None:
        super().__init__(source)
        self.handler_parent = parent
        self.payload_view = memoryview(self.handle.read())
        self.archiver = KodsArchiver(self.payload_view)
        self.datacenter_headers = datacenter_headers

    def get_file_tree(self) -> VfsNode:
        '''System unifies all unpacks to one header thus can unpack generically'''
        root = VfsNode(
            name=f'{getattr(self.handler_parent, "name", "KODS")} contents',
            category=getattr(self.handler_parent, 'category', 'Unknown'),
            parent=self.handler_parent,        
        )

        has_external_mapping = bool(self.datacenter_headers)
        is_internal = not has_external_mapping and self.payload_view[:4].tobytes() == b'Kods'

        headers = self._collect_headers(include_internal=is_internal)
        if not headers:
            logger.error('No valid headers found for unpacking')
            return root
        
        master_map = self.archiver.get_kods_map(headers, is_internal)
        extensions = generate_ext_overrides()
        primary_nodes = master_map.get(0, [])

        for logical_type, meta in enumerate(primary_nodes):
            if not meta.is_valid or meta.size == 0:
                dummy_node = VfsNode(
                    name=f'sentinel {logical_type:03d}',
                    offset=meta.offset,
                    size=0,
                    parent=root
                )
                dummy_node.is_hidden = True
                root.append_child(dummy_node)
                continue
            
            header = bytes(self.payload_view[meta.offset : meta.offset + 8])
            ext: str = next((match for sig, match in extensions.items() if header.startswith(sig)), '.bin')
            name = f'{meta.node_index:04d}{ext}' if is_internal else f'Entry {meta.node_index:02d}{ext}'

            node = VfsNode(
                name=name,
                offset=meta.offset,
                size=meta.size,
                header=header,
                extension=ext,
                parent=root,
            )
            root.append_child(node)

        logger.info(f'Successfully unpacked {len(root.children)} sections')
        return root

    def get_raw_node(self, node: VfsNode) -> bytes:
        return self.payload_view[node.offset : node.offset + node.size].tobytes()

    def _collect_headers(self, include_internal: bool = True) -> list[memoryview]:
        '''Scans all possible headers for validity'''
        all_headers: list[memoryview] = []
        if include_internal: # Check internal header
            magic = self.payload_view[:4].tobytes()
            if magic == b'Kods':
                all_headers.append(self.payload_view)
        if self.datacenter_headers: # Check datacenter headers
            for header in self.datacenter_headers:
                all_headers.append(memoryview(header))
        return all_headers

    def rebuild_node(self, parent: VfsNode, staged_nodes: list[VfsNode]) -> bytes:
        '''Routes to the correct rebuild strategy based on node state'''
        if getattr(parent, 'is_composite_buffer', False):
            return self._rebuild_composite_buffer(staged_nodes)
        return self._rebuild_static_archive(staged_nodes)

    def _rebuild_composite_buffer(self, staged_nodes: list[VfsNode]) -> bytes:
        '''Layer 1: Patching modified variant (.bin) bytes into the raw decompressed buffer'''
        logger.info('Rebuilding decompressed composite buffer...')
        buffer = bytearray(self.payload_view)
        for child in staged_nodes:
            buffer[child.offset : child.offset + child.size] = child.pending_data
        return bytes(buffer)

    def _rebuild_static_archive(self, staged_nodes: list[VfsNode]) -> bytes:
        '''Layer 3: Patching newly compressed (.slz) chunks back into the physical Kods archive'''
        logger.info('Rebuilding physical Kods archive...')
        new_kods = bytearray(self.payload_view)
        
        for child in staged_nodes:
            # Note: Offset/Size shift correction logic will go here eventually
            new_kods[child.offset : child.offset + len(child.pending_data)] = child.pending_data
            
        return bytes(new_kods)
    
    def get_properties(self) -> str:
        headers_view = self._collect_headers()
        lines = [f'Kods Archive Properties:\nNumber of Headers: {len(headers_view)}\n']
        for i, header_view in enumerate(headers_view):
            is_internal =(i==0) and header_view
            p = self.archiver.parse_header(header_view, is_internal=is_internal)
            if p.num_entries == 0:
                continue

            mode_str = '32bit aligned' if not p.mode else '16bit aligned'
            header_title = 'Inline' if i == 0 else f'Datacenter Index {i}'

            lines.append(
                f'--- Header: {header_title} ---\n'
                f'Number of Entries: {p.num_entries}\n'
                f'Compression shift: {p.shift}\n'
                f'Entry mode: {mode_str}\n'
                f'Secondary table present: {p.has_second_table}\n'
                f'Size of Pre-Payload data: {p.payload_offset} bytes\n'
            )
        return '\n'.join(lines)

    def execute_action(self, node: VfsNode, action_name: str, progress_callback, log_callback, **kwargs) -> Any:
        if action_name == 'Unpack':
            log_callback(f'Unpacking {node.name}...')
            return self.get_file_tree()
        elif action_name == 'Properties':
            return self.get_properties()
        return None
    
###----------------------------------------------- Archiver ----------------------------------------------------###

class KodsArchiver:
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
        is_internal: bool
        header_size: int = 0
        payload_offset: int = 0

    @dataclass(slots=True)
    class FileNodeMeta:
        ''''Represents a mapped file from any header source'''
        header_index: int
        node_index: int
        offset: int       # relative offset into payload
        size: int
        is_valid: bool
        is_pointer: bool = False

    def __init__(self, payload: bytes | memoryview | bytearray):
        self.payload_view = memoryview(payload)
        self.payload_length = len(self.payload_view) # Includes header size if internal header present
    
    def get_kods_map(self, headers: list[memoryview], is_internal: bool) -> dict[int, list[KodsArchiver.FileNodeMeta]]:
        '''Generate a single offset map into the payload from all provided headers'''
        kods_map: dict[int, list[KodsArchiver.FileNodeMeta]] = {}

        for header_idx, header_view in enumerate(headers): # Get Headers, offsets, and shifts
            header_obj = self.parse_header(header_view, is_internal)
            if len(header_view) <= 8:
                continue
            offsets = self._get_offsets(header_view, header_obj)
            header_nodes: list[KodsArchiver.FileNodeMeta] = []
            for i, offset in enumerate(offsets): # Get Basic Segment metadata (missing size)
                is_valid = (offset != -1)
                if is_valid and is_internal: 
                    is_valid = offset < self.payload_length
                node_metadata = self.FileNodeMeta(header_idx, i, offset, 0, is_valid)
                header_nodes.append(node_metadata)

            valid_nodes = [node for node in header_nodes if node.is_valid]
            if is_internal:
                valid_nodes.append(self.FileNodeMeta(-1, -1, self.payload_length, 0, False)) # EOF sentinel
            else: # Boundary not yet known for datacenter headers 1-9
                valid_nodes.append(self.FileNodeMeta(-1, -1, -1, 0, True))

            for current_node, next_node in zip(valid_nodes, valid_nodes[1:]): # Calculate valid node sizes
                if current_node.offset == next_node.offset:
                    # TODO ALIAS...
                    continue

                if next_node.offset == -1:
                    current_node.size = -1
                else:
                    current_node.size = next_node.offset - current_node.offset

            kods_map[header_idx] = header_nodes

        return kods_map

    ###-------------------------------------------- Helpers --------------------------------------------###

    def parse_header(self, header_view: memoryview, is_internal: bool) -> KodsHeader:
        '''Return dataclass with all header definitions'''
        if len(header_view) < 8:
            return self.KodsHeader(num_entries=0,shift=0,mode=False,stride=0,has_second_table=False,bit30=False,sentinel=0,format='None',is_internal=True)

        magic, control_word = struct.unpack_from("<II", header_view, 0)
        if magic != 0x73646F4B: # 'Kods'
            return self.KodsHeader(num_entries=0,shift=0,mode=False,stride=0,has_second_table=False,bit30=False,sentinel=0,format='None',is_internal=True)

        # Bit Field Extraction
        num_entries = control_word & 0xFFFF              # Bits 0-15
        shift = (control_word >> 16) & 0x0F              # Bits 16-19
        mode = (control_word >> 20) & 0x01               # Bits 20
        has_second_table = (control_word >> 29) & 0x01   # Bit 29
        bit30 = (control_word >> 30) & 0x01              # Bit 30
        
        # Data alignement
        data_format = "<H" if mode else "<I"
        stride = 2 if mode else 4
        sentinel = 0xFFFF if mode else 0xFFFFFFFF
        header_size = 8
        table_count = 2 if has_second_table else 1
        payload_offset =  header_size + (num_entries * stride * table_count) if is_internal else 0

        return self.KodsHeader(
            num_entries,
            shift,
            bool(mode),
            stride,
            bool(has_second_table),
            bool(bit30),
            sentinel,
            data_format,
            is_internal,
            header_size,
            payload_offset
        )

    def _get_offsets(self, header_view: memoryview, header_obj: KodsArchiver.KodsHeader) -> list[int]:
        '''Return list of (offset, alias) for files inside the Kods container'''
        offsets = []
        for i in range(header_obj.num_entries):
            offset_pos = header_obj.header_size + (i * header_obj.stride)
            raw_offset = struct.unpack_from(header_obj.format, header_view, offset_pos)[0]
            # Collect offset data
            if raw_offset == header_obj.sentinel:
                offset = -1
            elif raw_offset == 0:
                offset = header_obj.payload_offset
            else:
                offset = header_obj.payload_offset + (raw_offset << header_obj.shift)
            offsets.append(offset)
        return offsets
