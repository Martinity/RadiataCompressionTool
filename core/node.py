
from enum import Enum, auto
from typing import Optional, Tuple


###--------------------------------------------- VFS Node ------------------------------------------------------###

class NodeStatus(Enum):
    '''Hold modification data'''
    UNMODIFIED = auto()
    MODIFIED = auto()

class VfsNode:
    '''Pure Data Container. All files whether iso, kods, or raw are all nodes. '''
    def __init__(
        self, 
        name: Optional[str] = 'Undefined', 
        category: str = 'Unknown', 
        offset: int = 0, 
        size: int = 0, 
        header: bytes = b'', 
        extension: str = '.bin', 
        parent: Optional['VfsNode'] = None, 
        hid: Tuple[int, ...] = (),
    ):
        self.name = name                                # semantic name from overrides
        self.category = category                        # semantic category derived from disk index
        self.parent = parent                            # parent node
        self.children: list[VfsNode] = []               # children node(s)

        self.offset = offset                            # Relative offset into parent
        self.size = size                                # Size of node (VirtualFile=disk[offset:offset+size])

        self.header = header                            # raw header
        self.extension = extension                      # extension from override

        self._id_path: Tuple[int, ...] = hid            # hierarchical id (root, sub, subsub)

        self.status = NodeStatus.UNMODIFIED             # node state
        self.pending_data: bytes | None = None          # cached data

        # Flags; Useful for rebuild and UI
        self.is_container = False
        self.is_unpacked = False 
        self.is_decompressed = False
        self.is_dirty = False
        self.is_target = False

        self._handler_data: dict = {}
    
    def append_child(self, child: 'VfsNode'):
        '''Allow children nodes'''
        self.children.append(child)
        child.parent = self
        child._id_path = self._id_path + (len(self.children) - 1,)

    @property
    def hierarchical_id(self) -> Tuple[int, ...]:
        '''Return tuple id'''
        return self._id_path
    
    @property
    def hierarchical_id_str(self) -> str:
        '''Return human readable id'''
        return '.'.join(map(str, self._id_path)) if self._id_path else '0'
    
    def row(self) -> int:
        '''Keep track of the children-parent links for tree view'''
        if self.parent:
            return self.parent.children.index(self)
        return 0
    
    def mark_dirty(self, new_data: bytes):
        self.pending_data = new_data
        self.status = NodeStatus.MODIFIED
        self.is_dirty = True

    def clear_pending(self):
        self.pending_data = None
        self.status = NodeStatus.UNMODIFIED
        self.is_dirty = False

    def __repr__(self) -> str:
        return (f"<VfsNode '{self.name}' "
                f"id={self._id_path} "
                f"size={self.size} "
                f"dirty={self.is_dirty}>")

###------------------------------------------------------- VFS Manager -----------------------------------------------------###

class VfsManager:
    '''Virtual File System Manager. Bridge between the dispatcher and node'''
    def __init__(self, root_node: VfsNode):
        self.root = root_node
        # Flat path lookup map
        self.nodes_by_id: dict[Tuple[int, ...], VfsNode] = {}
        self.abs_offset_map: dict[VfsNode, int] = {}
        # Track modified nodes
        self.dirty_nodes: set[VfsNode] = set()
        # Initialize root with offset 0
        self.register_node(self.root, 0)

    def register_node(self, node: VfsNode, parent_abs_offset: int = 0):
        abs_disk_offset = parent_abs_offset + node.offset

        self.abs_offset_map[node] = abs_disk_offset
        self.nodes_by_id[node.hierarchical_id] = node

        for child in node.children:
            self.register_node(child, abs_disk_offset)

    def get_absolute_offset(self, node: VfsNode) -> int:
        return self.abs_offset_map.get(node, 0)

    def get_node_by_id(self, hid: Tuple[int, ...]) -> Optional[VfsNode]:
        '''Node lookup: manager.get_node_by_id((0, 3, 1))'''
        return self.nodes_by_id.get(hid)
    
    def mark_dirty(self, node: VfsNode, new_data: bytes):
        node.pending_data = new_data
        node.status = NodeStatus.MODIFIED
        node.is_dirty = True
        self.dirty_nodes.add(node)
