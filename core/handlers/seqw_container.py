from __future__ import annotations

import io
from core.contracts import ContainerHandler
from core.node import VfsNode
from core.registry import Registry
from core.workers import ActionDef, ActionType

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###----------------------------- FIS Handler -------------------------------------###
'''
Create dict for types of textures


'''

@Registry.register(
    name='SEQW wrapper handler',
    extensions=('.seqw',),
    supported_actions=(
        ActionDef('Unwrap', ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG)
    ))
class SEQWHandler(ContainerHandler):
    '''Unpack SEQW to raw sound data.'''
    def __init__(self, source: io.BufferedIOBase | bytes):
        super().__init__(source)

    def get_raw_node(self, node: VfsNode) -> bytes:
        self.handle.seek(node.offset)
        return self.handle.read(node.size)

    def get_file_tree(self) -> VfsNode:
        return VfsNode(name='raw_data')
