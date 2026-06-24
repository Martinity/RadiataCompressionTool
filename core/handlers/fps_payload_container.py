'''ContainerHandler for limited fps unpacking currently supports only FIS extraction'''
from __future__ import annotations

from core.contracts import ContainerHandler
from core.workers import ActionDef, ActionType
from core.node import VfsNode

from typing import Any

from core.extension_overrides import generate_ext_overrides
from core.registry import Registry

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------- Chain handler -----------------------------------###

'''
fps payload [28:32] offset to FIS payload + header size (0x40)
[20:24] 6 = fis is contained?
[34:36] seems to be C0 B4 or similar
'''
@Registry.register(
    name='FPS Handler', 
    extensions=('.fps-Segment',), 
    supported_actions=(
        ActionDef('Extract FIS', ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG)
))
class FpsPayloadHandler(ContainerHandler):
    def __init__(self, source: bytes, parent: VfsNode):
        super().__init__(source)
        self.handler_parent = parent
        self.data = memoryview(source)

    def get_raw_node(self, node: VfsNode) -> bytes:
        return bytes(self.data[node.offset : node.offset + node.size])

    def get_file_tree(self) -> VfsNode:
        root = VfsNode(name='dummy')
        if not self.data[0x10] != 6:
            return root
        extensions = generate_ext_overrides()

        fis_offset = int.from_bytes(self.data[0x18:0x1B], 'little') + 0x40
        fis_payload = self.data[fis_offset:]

        pos = 0
        while pos < len(fis_payload):
            raw_header = bytes(fis_payload[pos:pos+16])
            if len(fis_payload[pos:]) < 0x30 or fis_payload[pos:pos+4] != b'FIS\00':
                break
            ext: str = next((match for sig, match in extensions.items() if raw_header.startswith(sig)), '.bin')
            size = self.parse_fis(bytes(fis_payload[pos:]))
            node = VfsNode(
                name=f'FIS texture ({self.handler_parent.hierarchical_id_str}.*)',
                category=self.handler_parent.category,
                offset=fis_offset + pos,
                size=size,
                header=raw_header,
                extension=ext,
                parent=root
            )
            root.append_child(node)
            pos += size

            logger.debug(f'Successfully extracted FIS from offset {fis_offset}, size: {size}bytes')
        return root

    def parse_fis(self, data: bytes) -> int:
        '''Parse FIS header and determine size'''
        pre_image_size      = int.from_bytes(data[0x1C:0x1C+4], 'little')
        pal_storage_size    = int.from_bytes(data[0x20:0x20+4], 'little')
        img_hdr_off         = pre_image_size + 0x10
        if img_hdr_off + 0x10 > len(data):
            logger.warning('Invalid FIS payload')
        image_size = int.from_bytes(data[img_hdr_off + 4:img_hdr_off+8], 'little')
        image_offset = int.from_bytes(data[img_hdr_off+0x08:img_hdr_off+0x08+4], 'little')
        pal_off = img_hdr_off - pal_storage_size
        return pal_off + image_offset + image_size + 0x10

    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode]) -> bytes:
        if not self.task_handle:
            raise RuntimeError(f'No Task Handle for {self.__class__.__name__}')
        fis_offset = int.from_bytes(self.data[0x18:0x1B], 'little') + 0x40
        new_node = bytearray(self.data[:fis_offset])
        for child in node.children:
            self.task_handle.log_message.emit(f'Rebuilding FPS with {len(node.children)} children. Old texture size:{len(self.data[fis_offset:])}')
            if child.pending_data:
                new_node.extend(child.pending_data)
                self.task_handle.log_message.emit(f'{node.hierarchical_id} New FPS container built with new texture size:{len(child.pending_data)}. Original size:{node.size} New size:{len(new_node)}')
                return bytes(new_node)
            else:
                self.task_handle.log_message.emit(f'No FPS changes size stays the same: {node.size}={len(self.data)}')
                return bytes(self.data)
        return b''

    def get_properties(self, node: VfsNode):
        return 'Not yet Implemented'

    def execute_action(self, node: VfsNode, action_name: str, **kwargs) -> Any:
        if action_name == 'Extract FIS':
            return self.get_file_tree()
        elif action_name == 'Properties':
            return self.get_properties(node)
        return None
