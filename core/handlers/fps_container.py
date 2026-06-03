'''ContainerHandler for limited fps unpacking currently supports only FIS extraction'''
from __future__ import annotations

from core.contracts import ContainerHandler
from core.workers import ActionDef, ActionType
from core.node import VfsNode

from typing import Any, Callable

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
    extensions=('.Un fps',), 
    supported_actions=(
        ActionDef('Extract FIS', ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG)
))
class ChainHandler(ContainerHandler):
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

        raw_header = bytes(fis_payload[:16])
        ext: str = next((match for sig, match in extensions.items() if raw_header.startswith(sig)), '.bin')

        node = VfsNode(
            name='FIS texture',
            category=self.handler_parent.category,
            offset=fis_offset,
            size=len(fis_payload),
            header=raw_header,
            extension=ext,
            parent=root
        )
        root.append_child(node)

        logger.info(f'Successfully extracted FIS from {fis_offset}, size: {len(fis_payload)}')
        return root

    def rebuild_node(self, node: VfsNode, staged_nodes: list[VfsNode], log_callback: Callable) -> bytes:
        fis_offset = int.from_bytes(self.data[0x18:0x1B], 'little') + 0x40
        new_node = bytearray(self.data[:fis_offset])
        for child in node.children:
            log_callback(f'Rebuilding FPS with {len(node.children)} children. Old texture size:{len(self.data[fis_offset:])}')
            if child.pending_data:
                new_node.extend(child.pending_data)
                log_callback(f'New FPS container built with new texture size:{len(child.pending_data)}. Original size:{node.size} New size:{len(new_node)}')
                return bytes(new_node)
            else:
                log_callback(f'No FPS changes size stays the same: {node.size}={len(self.data)}')
                return bytes(self.data)




    
    def get_properties(self, node: VfsNode):
        return 'Not yet Implemented'

    def execute_action(self, node: VfsNode, action_name: str, progress_callback: Callable[[int, str], None], log_callback: Callable[[str], None], **kwargs) -> Any:
        if action_name == 'Extract FIS':
            return self.get_file_tree()
        elif action_name == 'Properties':
            return self.get_properties(node)
        return None
