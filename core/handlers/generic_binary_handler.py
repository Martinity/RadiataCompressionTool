import io
from core.contracts import LeafHandler
from core.node import VfsNode

class GenericBinaryHandler(LeafHandler):
    '''Generic Handler used to get raw bytes of node'''
    def __init__(self, source: io.BufferedIOBase | bytes):
        super().__init__(source)

    def get_raw_node(self, node: VfsNode) -> bytes:
        self.handle.seek(node.offset)
        return self.handle.read(node.size)

    def get_file_tree(self) -> VfsNode:
        return VfsNode(name='raw_data')
