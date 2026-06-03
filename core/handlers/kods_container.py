'''ContainerHandler for handling all kods format and datacenter'''
from __future__ import annotations

import struct
from io import BytesIO
from dataclasses import dataclass
from typing import Any, Callable

from core.contracts import ContainerHandler, RebuildResult
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
    supported_actions=(
        ActionDef('Unpack',     ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG),
    ))
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
            parent=self.handler_parent,        
        )

        is_internal = True if not self.datacenter_headers else False

        headers = self._collect_headers(include_internal=is_internal)
        if not headers:
            logger.error('No valid headers found for unpacking')
            return root
        
        master_map = self.archiver.get_kods_map(headers, is_internal)
        extensions = generate_ext_overrides()
        total = []
        for value in master_map.values():
            total += value

        for logical_type, meta in enumerate(total):
            if not meta.is_valid or meta.size == 0 or meta.size == -1:
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
            name = f'{meta.node_index:04d}' if is_internal else f'Entry {meta.node_index:02d}'
            # target = DatacenterTargets.get_target(self.handler_parent.hierarchical_id + (meta.node_index,))

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
        logger.debug(f'Read {node.size} from {node.offset}')
        return self.payload_view[node.offset : node.offset + node.size].tobytes()

    def _collect_headers(self, include_internal: bool = True) -> list[memoryview]:
        '''Scans all possible headers for validity'''
        all_headers: list[memoryview] = []
        if include_internal: # Check internal header
            magic = self.payload_view[:4].tobytes()
            if magic == b'Kods':
                logger.debug('Adding internal header...')
                all_headers.append(self.payload_view)
        if self.datacenter_headers: # Check datacenter headers
            logger.debug(f'Adding datacenter header... {self.datacenter_headers[0]}')
            all_headers.append(memoryview(self.datacenter_headers[0]))
        logger.debug(f'total number of headers= {len(all_headers)}')
        return all_headers

    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode], log_callback: Callable) -> RebuildResult:
        '''Routes to the correct rebuild strategy based on node state'''
        staged_set = set(staged_nodes)

        # Correctly determine if this is an internal container
        is_internal = not bool(self.datacenter_headers)
        header = self.datacenter_headers[0] if self.datacenter_headers else None
        header_view = memoryview(header) if header else memoryview(b'')

        # Parse header with correct internal flag
        header_obj = self.archiver.parse_header(header_view, is_internal)

        # Build map of child data (use pending if modified)
        child_data_map: dict[int, bytes] = {}
        for child in node.children:
            idx = child.hierarchical_id[-1]
            if child in staged_set and child.pending_data:
                child_data_map[idx] = child.pending_data
            else:
                child_data_map[idx] = self.get_raw_node(child)

        # Rebuild payload with proper alignment
        payload_stream = BytesIO()
        new_offsets: list[int] = []
        new_sizes: list[int] = []

        # For internal containers, payload starts after header + tables
        if is_internal:
            current_payload_offset = header_obj.header_size + (header_obj.num_entries * header_obj.stride)
            if header_obj.has_second_table:
                current_payload_offset += (header_obj.num_entries * header_obj.stride)
        else:
            current_payload_offset = 0  # Datacenter payload is separate

        for idx in range(header_obj.num_entries):
            data = child_data_map.get(idx, b'')
            if not data:
                new_offsets.append(-1)
                new_sizes.append(0)
                continue

            # Apply alignment padding
            pad_modulo = (1 << header_obj.shift)
            current_pos = payload_stream.tell()
            if current_pos % pad_modulo != 0:
                padding_needed = pad_modulo - (current_pos % pad_modulo)
                payload_stream.write(b'\x00' * padding_needed)

            assigned_offset = current_payload_offset + payload_stream.tell()
            new_offsets.append(assigned_offset)
            new_sizes.append(len(data))
            payload_stream.write(data)

        # Build the complete header (magic + control + offset table + optional size table)
        header_block = BytesIO()
        orig_header_view = self.payload_view[:header_obj.header_size] if is_internal else header_view
        base_header = self.archiver.build_header(orig_header_view, new_offsets, is_internal)
        header_block.write(base_header)

        # Write offset table
        for offset in new_offsets:
            if offset == -1:
                val = header_obj.sentinel
            elif offset == header_obj.payload_offset:
                val = 0
            else:
                val = (offset - header_obj.payload_offset) >> header_obj.shift
            header_block.write(struct.pack(header_obj.format, val))

        # Write size table if present
        if header_obj.has_second_table:
            for size in new_sizes:
                val = (size >> header_obj.shift) if size > 0 else header_obj.sentinel
                header_block.write(struct.pack(header_obj.format, val))

        # Final assembly
        if is_internal:
            final_payload = header_block.getvalue() + payload_stream.getvalue()
            target_header_data = None
        else:
            final_payload = payload_stream.getvalue()
            target_header_data = header_block.getvalue()

        log_callback(
            f'Container {node.name} rebuilt successfully. '
            f'Original size: {node.size} → New size: {len(final_payload)} '
            f'({"Internal" if is_internal else "Datacenter"} mode)'
        )
        if target_header_data:
            log_callback(f'Datacenter header rebuilt: {len(target_header_data)} bytes')
        log_callback(str(final_payload))
        return RebuildResult(final_payload, target_header_data)

    def get_properties(self) -> str:
        is_internal = True if not self.datacenter_headers else False
        headers_view = self._collect_headers(include_internal=is_internal)
        lines = [f"Number of Headers: {len(headers_view)}"]
        for i, header_view in enumerate(headers_view):
            p = self.archiver.parse_header(
                header_view,
                is_internal=is_internal
            )
            if p.num_entries == 0:
                continue
            mode_str = (
                "32bit aligned"
                if not p.mode
                else "16bit aligned"
            )
            header_title = (f"Datacenter Index {i}")
            lines.extend([
                f"Header: {header_title}",
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
            
    def execute_action(self, node: VfsNode, action_name: str, progress_callback, log_callback, **kwargs) -> Any:
        if action_name == 'Unpack':
            log_callback(f'Unpacking {node.name}...') if log_callback else None
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
        logger.warning(f'{len(headers)} header(s)')

        for header_idx, header_view in enumerate(headers): # Get Headers, offsets, and shifts
            header_obj = self.parse_header(header_view, is_internal)
            if len(header_view) <= 8: # WTF is this?
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
        return offsets

    def build_header(self, header_view: memoryview, new_offsets: list[int], is_internal: bool) -> bytes:
        '''Build the base header (magic + control word). The offset/size tables are built separately.'''
        if len(header_view) < 8:
            # Fallback: create minimal valid header
            magic = 0x73646F4B  # 'Kods'
            control_word = (len(new_offsets) & 0xFFFF) | (4 << 16)  # default shift=4
            if not is_internal:
                control_word |= (1 << 30)
            return struct.pack('<II', magic, control_word)

        magic, control_word = struct.unpack('<II', header_view[:8])

        # Preserve most fields, only update bit 30 (internal/datacenter flag)
        if is_internal:
            control_word &= ~(1 << 30)
        else:
            control_word |= (1 << 30)

        # Update num_entries if it changed
        num_entries = len(new_offsets) & 0xFFFF
        control_word = (control_word & ~0xFFFF) | num_entries

        return struct.pack('<II', magic, control_word)