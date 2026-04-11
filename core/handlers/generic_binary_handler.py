import io
from pathlib import Path
from typing import Union
from core.contracts import BaseHandler
from core.node import VfsNode

class GenericBinaryHandler(BaseHandler):
    '''Generic Handler used to get raw bytes of node'''
    def __init__(self, source: Union[Path, io.BufferedIOBase, bytes]):
        super().__init__(source)

    def read_file_data(self, node: VfsNode) -> bytes:
        self.handle.seek(node.offset)
        return self.handle.read(node.size)

    def get_file_tree(self) -> VfsNode:
        return VfsNode(name='raw_data')

    def rebuild_file_data(self, output_path: Path, virtual_tree: VfsNode):
        pass
