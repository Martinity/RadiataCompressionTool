from __future__ import annotations

import matplotlib.pyplot as plt
import matplotlib.cm as cm
from collections import Counter

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
    def __init__(self, source: bytes, parent: VfsNode, datacenter_headers: list[bytes] | None = None) -> None:
        super().__init__(source)
        self.handler_parent = parent
        if hasattr(self.handle, 'read'):
            self.payload_view = memoryview(self.handle.read())
        else:
            self.payload_view = memoryview(self.handle)
        self.archiver = KodsArchiver(self.payload_view)

        self.datacenter_headers = datacenter_headers

    def get_file_tree(self) -> VfsNode:
        return self._create_nodes()

    def get_raw_node(self, node: VfsNode) -> bytes:
        logger.info(f'Requested offset {node.offset} of size {node.size}')
        if node.offset == -1 or node.size == 0:
            return b''
        return self.payload_view[node.offset : node.offset + node.size]

    def _collect_headers(self) -> list[memoryview]:
        '''Scans all possible headers for validity'''
        all_headers: list[memoryview] = []
        if len(self.payload_view) >= 8: # Check internal header
            magic = self.payload_view[:4].tobytes()
            if magic == b'Kods':
                all_headers.append(self.payload_view)
            else: # 0-length internal sentinel
                all_headers.append(memoryview(b''))
        if self.datacenter_headers: # Check datacenter headers
            for header in self.datacenter_headers:
                all_headers.append(memoryview(header))

        return all_headers

    def rebuild_node(self, root: VfsNode, staged_nodes: list[VfsNode]) -> bytes:
        """Rebuilds the Kods archive with modified child nodes."""
        logger.info(f"Rebuilding Kods archive: {root.name}")

        headers_view = self._collect_headers()
        if not headers_view or not headers_view[0]:
            logger.error(f"Cannot rebuild {root.name}: No internal Kods header found.")
            return b''

    def get_properties(self) -> None:
        headers_view = self._collect_headers()
        logger.info(f'Kods Archive Properties:\nNumber of Headers:{len(headers_view)}')
        for i, header_view in enumerate(headers_view):
            if header_view and i == 0: # Check for internal header
                p = self.archiver.parse_header(header_view, is_internal=True)
            elif header_view: # Check for external headers
                p = self.archiver.parse_header(header_view, is_internal=False)
            else: # 
                logger.info('No internal Header.')

            mode_str = '32bit aligned' if not p.mode else '16bit aligned'

            header = 'Inline' if i == 0 else f'Datacenter Index {i}'
            logger.info(f'Header {header}:\nNumber of Entries: {p.num_entries} '
                        f'| Compression shift: {p.shift} | Entry mode: {mode_str} '
                        f'| Secondary table present: {p.has_second_table} | Size of Pre-Payload data: {p.payload_offset} bytes')
    
    def execute_action(self, node: VfsNode, action_name: str) -> Any:
        if action_name == 'Unpack':
            return self.get_file_tree()
        elif action_name == 'Properties':
            return self.get_properties()
        return None
    
    def get_identity(self) -> str:
        return 'Kods Archive'
    
    def unpack_with_headers(self, node: VfsNode, header_bytes: list[bytes]) -> VfsNode:
        '''Called for datacenter unpacks'''
        self.datacenter_headers = header_bytes
        logger.info(f'KodsHandler received {len(header_bytes)} datacenter headers for {node.name}')
        return self._create_nodes()
    
    def _create_nodes(self) -> VfsNode:
        '''helper for creating nodes out of kods data'''
        logger.debug(f'Unpacking Kods archive from {getattr(self.handler_parent, 'name',  'unknown')}')

        headers = self._collect_headers()
        self.archiver.analyze_offset_distribution(headers, len(self.payload_view))
        self.archiver.visualize_kods_coverage_discrete(headers, len(self.payload_view))
        if not headers:
            logger.error(f'No valid "Kods" header found for {self.handler_parent.name}')
            return VfsNode(name='Invalid', parent=self.handler_parent)
        
        master_map = self.archiver.get_kods_map(headers)
        extensions = generate_ext_overrides()

        root = VfsNode(
            name=f"{getattr(self.handler_parent, 'name', 'KODS')}_contents",
            category=getattr(self.handler_parent, 'category', 'Unknown'),
            parent=self.handler_parent,
        )

        for meta in master_map:
            if not meta.is_valid or meta.size == 0:
                name = 'Internal sentinel' if meta.header_index == 0 else f'sentinel {meta.node_index:03d}'
                dummy_node = VfsNode(
                    name=name,
                    offset=-1,
                    size=0,
                    extension='.sent',
                    parent=root
                )
                dummy_node.is_hidden = True
                root.append_child(dummy_node)
                continue

            header_bytes = bytes(self.payload_view[meta.offset : meta.offset + 8])
            ext = next((ext for sig, ext in extensions.items() if header_bytes.startswith(sig)), '.bin')
            name = 'Internal header' if meta.header_index == 0 else f'Datacenter header:{meta.header_index - 1} idx:{meta.node_index:03d}'

            node = VfsNode(
                name=name,#f'H{meta.header_index} {meta.node_index:04d}',
                offset=meta.offset,
                size=meta.size,
                header=header_bytes,
                extension=ext,
                parent=root,
            )
            root.append_child(node)
            node.is_hidden = True if not node.size else False

        logger.info(f'Successfully unpacked {len(root.children)} kods nodes from {len(headers)} headers')
        return root

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
        header_index: int # 0 = internal header -> 1+ = datacenter header
        node_index: int
        offset: int       # absolute offset into payload
        size: int
        is_valid: bool

    def __init__(self, payload: bytes | memoryview | bytearray):
        self.payload_view = memoryview(payload)
        self.payload_length = len(self.payload_view) # Includes header size if internal header present

    
    def get_kods_map(self, headers: list[memoryview]) -> list[KodsArchiver.FileNodeMeta]:
        '''Generate a single offset map into the payload from all provided headers'''
        kods_map: list[KodsArchiver.FileNodeMeta] = []
        valid_nodes: list[KodsArchiver.FileNodeMeta] = []
        header_shifts = {}

        for header_idx, header_view in enumerate(headers): # Get Headers, offsets, and shifts
            is_internal = True if header_idx == 0 else False
            header_obj = self.parse_header(header_view, is_internal)
            if len(header_view) <= 8:
                continue
            offsets = self._get_offsets(header_view, header_obj)
            header_shifts[header_idx] = header_obj.shift

            header_nodes: list[KodsArchiver.FileNodeMeta] = []
            for i, offset in enumerate(offsets): # Get Basic Segment metadata (missing size)
                is_valid = (offset != -1) & (offset < self.payload_length)
                node_metadata = self.FileNodeMeta(header_idx, i, offset, 0, is_valid)
                header_nodes.append(node_metadata)

            valid_nodes = [header for header in header_nodes if header.is_valid]
            # Sort metadata to solve size
            valid_nodes.sort(key=lambda header: header.offset)
            valid_nodes.append(self.FileNodeMeta(-1, -1, self.payload_length, 0, False)) # EOF sentinel

            for current_node, next_node in zip(valid_nodes, valid_nodes[1:]): # Calculate valid node sizes
                if current_node.offset == next_node.offset:
                    # TODO ALIAS...
                    continue

                start = current_node.offset
                end = next_node.offset

                # shift = header_shifts[current_node.header_index]
                # align_mask = (1 << shift) - 1
                # while end > start and self.payload_view[end - 1] == 0:
                #     if shift > 0 and (end - 1) & align_mask == 0: # boundary check
                #         break
                #     end -= 1
                
                current_node.size = end - start
                if current_node.size <= 0:
                    current_node.is_valid = False
                    current_node.offset = -1
                    current_node.size = 0

            kods_map.extend(header_nodes)

        valid_nodes.pop()
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
                abs_offset = -1
            elif raw_offset == 0:
                abs_offset = header_obj.payload_offset
            else:
                abs_offset = header_obj.payload_offset + (raw_offset << header_obj.shift)
            offsets.append(abs_offset)
        return offsets


    def analyze_offset_distribution(self, headers: list[memoryview], payload_length: int) -> None:
        '''Generates visualizations for offset usage across all interchangeable headers.'''
        all_offsets = []
        
        # 1. Collect every valid offset from every header
        for header_idx, header_view in enumerate(headers):
            if len(header_view) <= 8:
                continue
                
            is_internal = True if header_idx == 0 else False
            header_obj = self.parse_header(header_view, is_internal)
            offsets = self._get_offsets(header_view, header_obj)
            
            # Ignore -1 (empty/invalid nodes)
            valid_offsets = [off for off in offsets if off != -1]
            all_offsets.extend(valid_offsets)

        if not all_offsets:
            print("No valid offsets found to analyze.")
            return

        # 2. Count references per exact offset
        offset_counts = Counter(all_offsets)
        unique_offsets = sorted(offset_counts.keys())
        reference_counts = [offset_counts[off] for off in unique_offsets]

        # 3. Create the Visualization Figure
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
        fig.canvas.manager.set_window_title('KODS Offset Analysis')

        # --- Plot 1: Exact Reference Counts (Scatter/Stem) ---
        # This shows EXACTLY which offsets are reused and how many times.
        ax1.scatter(unique_offsets, reference_counts, color='#e74c3c', alpha=0.7, s=20)
        ax1.vlines(unique_offsets, 0, reference_counts, color='#e74c3c', alpha=0.3)
        ax1.set_title('Exact Offset Reference Counts (Cross-Header)')
        ax1.set_ylabel('Number of References')
        ax1.set_xlabel('Payload Offset (Bytes)')
        ax1.grid(True, alpha=0.3)

        # --- Plot 2: Spatial Density Histogram ---
        # This shows WHERE in the payload the data is most heavily clustered.
        bins = 100 # Adjust this to change the chunk size
        ax2.hist(all_offsets, bins=bins, color='#3498db', edgecolor='black', alpha=0.7)
        ax2.set_title(f'Data Density (Distribution across {bins} Payload Bins)')
        ax2.set_ylabel('Total References in Bin')
        ax2.set_xlabel('Payload Offset (Bytes)')
        ax2.set_xlim(0, payload_length) # Lock to exact payload size
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        plt.show()

    def visualize_kods_coverage_discrete(self, headers: list[memoryview], payload_length: int) -> None:
        '''Visualizes the container coverage with unique colors for every file entry.'''
        fig, ax = plt.subplots(figsize=(14, 8))
        fig.canvas.manager.set_window_title('KODS Discrete Coverage Map')
        
        # Use tab20 colormap for high-contrast categorical coloring
        cmap = cm.get_cmap('tab20')

        for h_idx, header_view in enumerate(headers):
            if len(header_view) <= 8:
                continue
                
            # 1. Generate the map for THIS header using the per-header logic
            node_map = self.get_kods_map([header_view]) 
            valid_nodes = [n for n in node_map if n.is_valid and n.size > 0]
            
            # 2. Plot each node individually to give it a unique color
            for i, node in enumerate(valid_nodes):
                # i % 20 ensures we cycle through the distinct tab20 colors
                node_color = cmap(i % 20)
                
                # (start, width), (y_base, height)
                ax.broken_barh([(node.offset, node.size)], 
                            (h_idx * 10, 8), 
                            facecolors=node_color, 
                            edgecolor='black', # Adds a thin line between adjacent files
                            linewidth=0.5,
                            alpha=0.9)

        # 3. Formatting
        ax.set_ylim(-5, len(headers) * 10 + 5)
        ax.set_xlim(0, payload_length)
        ax.set_xlabel('Payload Offset (Bytes)')
        ax.set_ylabel('Header Index (Lane)')
        ax.set_title('Discrete File Coverage: Every Block is a Unique Node Reference')
        ax.grid(True, axis='x', linestyle='--', alpha=0.3)
        
        # Background "Dead Zone" indicator
        ax.axvspan(0, payload_length, color='#f0f0f0', zorder=-1, label='Total Container Size')

        plt.tight_layout()
        plt.show()