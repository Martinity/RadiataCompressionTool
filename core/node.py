from __future__ import annotations
from enum import Enum, auto
from typing import Optional, Tuple
from PyQt6.QtCore import pyqtSignal, QObject

import logging
logger = logging.getLogger(f'radiata.{__name__}')


###--------------------------------------------- VFS Node ------------------------------------------------------###

class NodeStatus(Enum):
    '''Hold modification data'''
    UNMODIFIED = auto()
    MODIFIED = auto()
    STAGED = auto()

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
        parent: Optional[VfsNode] = None, 
        hid: Tuple[int, ...] = (),
        target: Optional[list[tuple]] = None,
    ):
        self.name = name                                # semantic name from overrides
        self.category = category                        # semantic category derived from disk index
        self.parent = parent                            # parent node (None = Root)
        self.children: list[VfsNode] = []               # children node(s)

        self.offset = offset                            # Relative offset into parent
        self.size = size                                # Size of node in bytes (VirtualFile=disk[offset:offset+size])
        self.target = target                            # Datacenter for the node

        self.header = header                            # raw header
        self.extension = extension                      # extension from override

        self._id_path: Tuple[int, ...] = hid            # hierarchical id (root, sub, subsub)

        self.status = NodeStatus.UNMODIFIED             # node state
        self.pending_data: bytes | None = None          # cached data

        # Flags; Useful for rebuild and UI
        self.is_physical = False                        # Has physical address
        self.is_unpacked = False                        # Kods
        self.compressed_header: bytes = b''             # SLZ
        self.is_hidden = False                          # Hide node in UI (file system related or null nodes by default)
    
    def append_child(self, child: VfsNode):
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
    
    @property
    def visible_children(self) -> list[VfsNode]:
        return [child for child in self.children if not getattr(child, 'is_hidden', False)]

    def row(self) -> int:
        '''Keep track of the children-parent links for tree view'''
        if self.parent is None:
            return 0
        try:
            return self.parent.children.index(self)
        except (ValueError, AttributeError):
            return 0    
        
    def clear_pending(self):
        self.pending_data = None
        self.status = NodeStatus.UNMODIFIED

    def __repr__(self) -> str:
        return (f"<VfsNode '{self.name}' "
                f"id={self._id_path} "
                f"size={self.size}>")

###------------------------------------------------------- VFS Manager -----------------------------------------------------###

class VfsManager(QObject):
    '''Virtual File System Manager. Bridge between the dispatcher and node'''
    insert_start = pyqtSignal(VfsNode, int, int) # (VfsNode, start_row, end_row)
    insert_finished = pyqtSignal()

    def __init__(self, root_node: VfsNode):
        super().__init__()

        self.root = root_node
        # Flat path lookup map
        self.nodes_by_id: dict[Tuple[int, ...], VfsNode] = {}
        # Physical disk map
        self.physical_offsets: dict[VfsNode, int] = {}
        # Track modified nodes
        self.dirty_nodes: set[VfsNode] = set()
        # Initialize root with offset 0
        self.register_node(self.root, 0)

    def register_node(self, node: VfsNode, relative_offset: int = 0, is_physical: bool = False):
        '''Register node with HID map'''
        self.nodes_by_id[node.hierarchical_id] = node

        if is_physical:
            abs_disk_offset = relative_offset + node.offset
            self.physical_offsets[node] = abs_disk_offset

        for child in node.children:
            self.register_node(child, relative_offset=0)

    def insert_children(self, parent_node: VfsNode, new_children: list[VfsNode], relative_offset: int = 0) -> None:
        '''Update the node and signal to the tree model'''
        if not new_children:
            return
        
        start_row = len(parent_node.children)
        end_row = start_row + len(new_children) - 1

        self.insert_start.emit(parent_node, start_row, end_row)
        for child in new_children:
            parent_node.append_child(child)
            self._silent_register(child, relative_offset)
        self.insert_finished.emit()
        
    def _silent_register(self, node: VfsNode, relative_offset: int = 0) -> None:
        '''Register node with HID map'''
        self.nodes_by_id[node.hierarchical_id] = node
        if node.is_physical:
            self.physical_offsets[node] = relative_offset + node.offset
        for child in node.children:
            self._silent_register(child, relative_offset=0)
        
    def get_offset(self, node: VfsNode) -> int:
        '''Get physical disk offsets'''
        return self.physical_offsets.get(node, 0)

    def get_node_by_id(self, hid: Tuple[int, ...]) -> Optional[VfsNode]:
        '''Node lookup for known registered nodes.'''
        return self.nodes_by_id.get(hid)
    
    def resolve_nodes(self, hids: list[Tuple[int, ...]], expansion_callback=None) -> list[VfsNode]:
        '''Resolve list of HIDs. expansion_callback for resolving yet registered nodes recursively'''
        resolved: list[VfsNode] = []
        logger.debug(f'Resolving {hids} with {expansion_callback}')

        for hid in hids:
            node = self._resolve_single_hid(hid, expansion_callback)
            if node:
                resolved.append(node)
        if not resolved:
            logger.warning(f'No nodes resolved for {hids}')
        return resolved

    def _resolve_single_hid(self, hid: Tuple[int, ...], expansion_callback=None) -> Optional[VfsNode]:
        '''Recursively expand physical -> target'''
        if hid in self.nodes_by_id:
            return self.nodes_by_id[hid]
        
        current = self.root
        for i in range(1, len(hid) + 1):
            path = hid[:i]
            next_node = self._find_child_by_path(current, path)

            if not next_node:
                if expansion_callback is None:
                    logger.warning(f'Cannot expand path {path}, no callback')
                    return None
                
                expansion_callback(current)
                next_node = self._find_child_by_path(current, path)
                if not next_node:
                    logger.warning(f'Expansion of {current.name}({current.hierarchical_id_str}) did not create the target child {path}')
                    return None
               
            current = next_node
        logger.debug(f'Resolved {current.name} from {hid}')
        return current

    def _find_child_by_path(self, parent: VfsNode, target_path: Tuple[int,...]) -> Optional[VfsNode]:
        for child in parent.children:
            if child.hierarchical_id == target_path:
                return child
        return None

###------------------------------------ Status Tracker ---------------------------------------###

class ModTracker(QObject):
    '''Modification state tracker'''
    node_modified = pyqtSignal(VfsNode)
    node_staged = pyqtSignal(VfsNode)
    node_unstaged = pyqtSignal(VfsNode)
    node_reverted = pyqtSignal(VfsNode)

    state_changed = pyqtSignal(int, int) # format: (unstaged_count, staged_count)
    rebuild_initiated = pyqtSignal(list)

    def __init__(self) -> None:
        super().__init__()
        self.modified_nodes: set[VfsNode] = set()
        self.rebuild_queue: set[VfsNode] = set()

    def _emit_state(self):
        self.state_changed.emit(len(self.modified_nodes), len(self.rebuild_queue))

    def mark_modified(self, node: VfsNode, new_data: bytes) -> None:
        node.pending_data = new_data
        node.status = NodeStatus.MODIFIED
        self.modified_nodes.add(node)

        self.node_modified.emit(node)
        self._emit_state()

    def stage_node(self, node: VfsNode) -> None:
        '''Move from cache to staging area'''
        if node in self.modified_nodes:
            self.modified_nodes.remove(node)
            self.rebuild_queue.add(node)
            node.status = NodeStatus.STAGED
            self.node_staged.emit(node)
            self._emit_state()

    def unstage_node(self, node: VfsNode) -> None:
        '''Move from staging back to cache'''
        if node in self.rebuild_queue:
            self.rebuild_queue.remove(node)
            self.modified_nodes.add(node)
            node.status = NodeStatus.MODIFIED
            self.node_unstaged.emit(node)
            self._emit_state()

    def revert_node(self, node: VfsNode) -> None:
        '''Discard changes'''
        self.modified_nodes.discard(node)
        self.rebuild_queue.discard(node)
        node.clear_pending()
        node.status = NodeStatus.UNMODIFIED

        logger.info(f'Reverted changes for node: {node.name}')
        self.node_reverted.emit(node)
        self._emit_state()

    def confirm_and_rebuild(self) -> None:
        '''Triggered by Confirm button in staging page'''
        if not self.rebuild_queue:
            # TODO can't trigger button when no edits
            return
        
        staged_nodes = list(self.rebuild_queue)
        logger.info(f'Initiating rebuild with {len(staged_nodes)} staged files.')
        self.rebuild_initiated.emit(staged_nodes)

    def clear(self) -> None:
        '''Clear state when closing an ISO'''
        self.modified_nodes.clear()
        self.rebuild_queue.clear()
        self._emit_state()