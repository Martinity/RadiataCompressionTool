from core.contracts import LeafHandler

class GenericBinaryHandler(LeafHandler):
    '''Generic Handler used to get raw bytes of node'''
    def __init__(self, source: bytes, parent_node):
        super().__init__(source)

