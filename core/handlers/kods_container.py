'''ContainerHandler for handling all kods format and datacenter'''
from __future__ import annotations
import struct
from io import BytesIO
from dataclasses import dataclass
from typing import Any

from core.contracts import ContainerHandler, RebuildResult
from core.extension_overrides import lookup_extension
from core.registry import Registry
from core.node import VfsNode
from core.workers import ActionDef, ActionType

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###-------------------------------------------- KodsHandler -------------------------------------------###

@Registry.register(
    name='Kods Archiver',
    extensions=('.kods',),
    supported_actions=(
        ActionDef('Unpack',     ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG),
    ))
class KodsHandler(ContainerHandler):
    '''Wrapper for Kods archiver class'''
    def __init__(self, source: bytes, parent: VfsNode, datacenter_header: bytes | None = None) -> None:
        super().__init__(source)
        self.handler_parent     = parent
        self.payload_view       = memoryview(self.handle.read())
        self.archiver           = KodsArchiver(self.payload_view)
        self.datacenter_header  = datacenter_header

    def get_file_tree(self) -> VfsNode:
        '''System unifies all unpacks to one header thus can unpack generically'''
        root = VfsNode(
            name=f'{getattr(self.handler_parent, "name", "KODS")} contents',
            parent=self.handler_parent,        
        )
        is_internal = not bool(self.datacenter_header)
        if not self.datacenter_header and self.payload_view[:4] != b'Kods':
            logger.error('No valid headers found for unpacking')
            return root

        if self.datacenter_header:
            header_view = memoryview(self.datacenter_header)
            header_obj = self.archiver.parse_header(header_view, False)
        else:
            header_obj = self.archiver.parse_header(self.payload_view, True)
            header_view = self.payload_view[:header_obj.payload_offset]
        master_map = self.archiver.get_kods_map(header_view, is_internal)
        for meta in master_map:
            if not meta.is_valid or meta.size == 0 or meta.size == -1:
                dummy_node = VfsNode(
                    name=f'sentinel {meta.node_index:03d}',
                    offset=meta.offset,
                    size=0,
                    parent=root
                )
                dummy_node.is_hidden = True
                root.append_child(dummy_node)
                continue
            
            header = bytes(self.payload_view[meta.offset : meta.offset + 8])
            ext: str = lookup_extension(header)
            name = f'{meta.node_index:04d}' if is_internal else f'Entry {meta.node_index:02d}'

            node = VfsNode(
                name=name,
                offset=meta.offset,
                size=meta.size,
                header=header,
                extension=ext,
                parent=root,
            )
            root.append_child(node)

        logger.debug(f'Unpacked {len(root.children)} sections from {root.name}')
        return root

    def get_raw_node(self, node: VfsNode) -> bytes:
        return self.payload_view[node.offset : node.offset + node.size].tobytes()

    ###------------------------------------- Rebuild ---------------------------------------------------###

    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode]) -> RebuildResult:
        '''Routes to the correct rebuild strategy based on node state'''
        if not self.task_handle:
            raise RuntimeError(f'No active Task Handle for {self.__class__.__name__}')     
        is_internal = not bool(self.datacenter_header)
        # Check original structure
        self.header_view  = (
            memoryview(self.datacenter_header)
            if self.datacenter_header
            else self.payload_view
        )
        self.header_obj = self.archiver.parse_header(self.header_view, is_internal)
        # Build the payload and header
        payload, header = self.build_payload(node.children)
        if self.header_obj.is_internal:
            payload = header + payload
            header = None
        # Sector align payload
        padding = (-len(payload)) & (0x800 - 1)
        payload += b'\x00' * padding
        self.task_handle.log_message.emit(f'{node.hierarchical_id} Rebuilt Kods Archive. Original size:{node.size} New size:{len(payload)}')
        return RebuildResult(payload, header)

    def build_payload(self, children: list[VfsNode]) -> tuple[bytes, bytes]:
        '''Build a new payload from all children'''
        # Build new payload, collect new offsets (preshifted)
        new_payload = BytesIO()
        orig_offsets = self.archiver._get_offsets(self.header_view, self.header_obj)
        offsets = []
        for i, child in enumerate(children):
            if i < len(orig_offsets) and orig_offsets[i] == -1:
                offsets.append(-1)
                continue
            offsets.append(
                (len(new_payload.getbuffer()) >> self.header_obj.shift)
                if len(new_payload.getbuffer()) != 0
                else 0
                )
            new_payload.write(child.pending_data if child.pending_data is not None else self.get_raw_node(child))
            align_mask  = (1 << self.header_obj.shift) - 1
            padding = (-len(new_payload.getbuffer())) & align_mask
            new_payload.write(padding * b'\00')
        # Build new header
        new_header = BytesIO()
        if self.datacenter_header and not self.header_obj.is_internal: # Header
            new_header.write(self.datacenter_header[:self.header_obj.header_size])
        else:
            new_header.write(self.payload_view[:self.header_obj.header_size])
        for offset in offsets: # Offsets
            try:
                new_header.write(
                    offset.to_bytes(self.header_obj.stride, 'little')
                    if offset != -1
                    else self.header_obj.sentinel.to_bytes(self.header_obj.stride, 'little')
                )
            except Exception as e:
                raise ValueError(f'Child container could not fit in the alloted size. {e}')
        if self.header_obj.has_second_table: # Secondary table
            second_table_start = self.header_obj.header_size + (self.header_obj.num_entries * self.header_obj.stride)
            if self.datacenter_header and not self.header_obj.is_internal:
                new_header.write(self.datacenter_header[second_table_start:self.header_obj.payload_offset])
            else:
                new_header.write(self.payload_view[second_table_start:self.header_obj.payload_offset])

        return new_payload.getvalue(), new_header.getvalue()

    ### ------------------ Properties and Execute actions ------------------------ ###

    def get_properties(self) -> str:
        is_internal = not bool(self.datacenter_header)
        if self.datacenter_header:
            header_view = memoryview(self.datacenter_header)
        else:
            header_obj = self.archiver.parse_header(self.payload_view, is_internal=True)
            header_view = self.payload_view[:header_obj.header_size] # Captures only up to the offset table

        lines = []
        p = self.archiver.parse_header(
            header_view,
            is_internal=is_internal
        )
        mode_str = (
            "32bit aligned"
            if not p.mode
            else "16bit aligned"
        )
        lines.extend([
            f"Number of Entries: {p.num_entries}",
            f"Compression shift: {p.shift}",
            f"Entry mode: {mode_str}",
            f"Secondary table present: {p.has_second_table}",
        ])
        if p.payload_offset:
            lines.append(
                f"Size of Pre-Payload data: {p.payload_offset} bytes"
            )
        lines.append("")
        return "\n".join(lines)
            
    def execute_action(self, node: VfsNode, action_name: str, **kwargs) -> Any:
        if action_name == 'Unpack':
            return self.get_file_tree()
        elif action_name == 'Properties':
            return self.get_properties()
        return None
    
###----------------------------------------------- Archiver ----------------------------------------------------###

class KodsArchiver:
    '''Archiver class for all kods archive related processing.'''
    @dataclass(slots=True)
    class KodsHeader:
        num_entries:      int
        shift:            int
        mode:             bool
        stride:           int
        has_second_table: bool
        bit30:            bool
        sentinel:         int
        format:           str
        is_internal:      bool
        header_size:      int = 0
        payload_offset:   int = 0

    @dataclass(slots=True)
    class FileNodeMeta:
        ''''Represents a mapped file from any header source'''
        node_index:   int
        offset:       int  # relative offset into payload
        size:         int
        is_valid:     bool

    def __init__(self, payload: bytes | memoryview | bytearray):
        self.payload_view   = memoryview(payload)
        self.payload_length = len(self.payload_view) # Includes header size if internal header present
    
    ###---------------------------------------- Unpack ----------------------------------------------###

    def get_kods_map(self, header: memoryview, is_internal: bool) -> list[KodsArchiver.FileNodeMeta]:
        '''Generate a single offset map into the payload from from the provided header'''
        # Get offsets, and shifts
        header_obj = self.parse_header(header, is_internal)
        offsets = self._get_offsets(header, header_obj)
        kods_map: list[self.FileNodeMeta] = []
        for i, offset in enumerate(offsets): # Get Basic Segment metadata (missing size)
            is_valid = offset != -1
            if is_valid and is_internal: 
                is_valid = offset < self.payload_length
            node_metadata = self.FileNodeMeta(i, offset, 0, is_valid)
            kods_map.append(node_metadata)

        valid_nodes = [node for node in kods_map if node.is_valid]
        valid_nodes.append(self.FileNodeMeta(-1, self.payload_length, -1, False)) # EOF sentinel

        for current_node, next_node in zip(valid_nodes, valid_nodes[1:]): # Calculate valid node sizes
            if current_node.offset == next_node.offset:
                # TODO ALIAS...
                continue
            if next_node.offset == -1:
                current_node.size = -1
            else:
                current_node.size = next_node.offset - current_node.offset
        return kods_map

    ###-------------------------------------------- Unpack Helpers --------------------------------------------###

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
        '''Return list of offsets for files inside the Kods container'''
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
        # offsets.append(header_obj.payload_offset + (len(self.payload_view) << header_obj.shift))
        return offsets
