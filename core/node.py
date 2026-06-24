'''Node metadata. Contains three supporting classes, VfsNode (File data), VfsManager (Relational data), ModTracker (Mutation tracking)'''
from __future__ import annotations

import threading
from enum import Enum, auto
from typing import Tuple, NamedTuple, Callable, TYPE_CHECKING
from PyQt6.QtCore import pyqtSignal, QObject
if TYPE_CHECKING:
    from core.handlers.compression_container import CompressorHandler

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
        name:      str = 'Undefined', 
        category:  Tuple[str, ...] = ('Unknown',), 
        offset:    int = 0, 
        size:      int = 0, 
        header:    bytes = b'', 
        extension: str = '.bin', 
        parent:    VfsNode | None = None,
        hid:       Tuple[int, ...] = (),
        target:    Tuple[int, ...] | None = None,
    ):
        self.name     = name                                # semantic name from overrides
        self.category = category                            # semantic category derived from disk index
        self.parent   = parent                              # parent node (None = Root)
        self.children: list[VfsNode] = []                   # children node(s)

        self.offset = offset                                # Relative offset into parent
        self.size   = size                                  # Size of node in bytes (VirtualFile=disk[offset:offset+size])
        self.target: Tuple[int,...] | None = target         # Header HID for unpacking datacenter

        self.header    = header                             # raw header
        self.extension = extension                          # extension from override
        self.compressed_header: CompressorHandler.SlzHeader | None = None        # SLZ source header

        self._id_path: Tuple[int, ...] = hid                # hierarchical id (root, sub, subsub)

        self.status = NodeStatus.UNMODIFIED                 # node state
        self.pending_data: bytes | None = None              # cached data

        # Flags; Useful for rebuild and UI
        self.is_physical   = False                          # Has physical address
        self.is_compressed = False                          # SLZ
        self.is_banked     = False
        self.is_unpacked   = False                          # Static Kods
        self.is_hidden     = False                          # Hide node in UI (file system related or null nodes by default)

        self.expansion_pending: bool = False                 # Threading active bool
        self._expansion_event: threading.Event | None = None # Threading event for active thread
    
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
        
    def begin_expansion(self) -> threading.Event:
        '''Mark expansion in progress. Return wait event'''
        self._expansion_event = threading.Event()
        self.expansion_pending = True
        return self._expansion_event
    
    def finish_expansion(self) -> None:
        '''Signal expansion complete'''
        self.expansion_pending = False
        if self._expansion_event:
            self._expansion_event.set()
        
    def clear_pending(self):
        self.pending_data = None
        self.status = NodeStatus.UNMODIFIED

    def __repr__(self) -> str:
        return (f"<VfsNode '{self.name}' "
                f"id={self._id_path} "
                f"size={self.size}>")

###------------------------------------------------------- VFS Manager -----------------------------------------------------###

class HidSnapshot(NamedTuple):
    '''Result of a lock-protect VFS snapshot'''
    resolved: list[VfsNode]             # nodes already in the VFS
    unresolved: list[Tuple[int, ...]]   # HIDs whose parent need expansion

class VfsManager(QObject):
    '''Virtual File System Manager. Bridge between the dispatcher and node'''
    insert_start = pyqtSignal(VfsNode, int, int) # (parent, first_row, last_row)
    insert_finished = pyqtSignal()

    def __init__(self, root_node: VfsNode, node_enricher: Callable[[VfsNode], None] | None = None) -> None:
        super().__init__()
        self.root        = root_node
        self.enrich_node = node_enricher
        self._lock       = threading.RLock()
       
        self.nodes_by_id:      dict[Tuple[int, ...], VfsNode] = {}  # Flat path lookup map
        self.physical_offsets: dict[VfsNode, int] = {}              # Physical disk map
        # Initialize root with offset 0
        self._register_recursive(self.root) # Register physical nodes with VFS initilization
    
    ###--------------------- Registration ----------------------###

    def _register_recursive(self, node: VfsNode, is_physical: bool = False, disk_base: int = 0):
        '''Register node and all its children. Must be called inside _lock after initialization.'''
        self.nodes_by_id[node.hierarchical_id] = node
        if is_physical:
            self.physical_offsets[node] = disk_base + node.offset
        for child in node.children:
            self._register_recursive(child)

    def register_node(self, node: VfsNode):
        with self._lock:
            self._register_recursive(node)

    def insert_children(self, parent: VfsNode, new_children: list[VfsNode]) -> None:
        '''Update the VFS and signal to the tree model'''
        if not new_children:
            return
        
        with self._lock:
            base_idx = len(parent.children)
            self.insert_start.emit(parent, base_idx, base_idx + len(new_children) - 1)
            for i, child in enumerate(new_children): # Add the nodes to the file system / tree
                child.parent = parent
                child._id_path = parent._id_path + (base_idx + i,)
                child.is_hidden = True if parent.is_hidden or not child.size or child.offset == -1 else False
                if self.enrich_node:
                    self.enrich_node(child)
                parent.children.append(child)
                self._register_recursive(child)
            self.insert_finished.emit()

    def enrich_initial_tree(self) -> None:
        '''Walk the tree after initialization enriching nodes with metadata'''
        if not self.enrich_node:
            return
        for child in self.root.children:
            self.enrich_node(child)
        logger.debug('VfsManager.enrich_initial_tree: complete')

    ###--------------------- Lookup -----------------------###

    def get_offset(self, node: VfsNode) -> int:
        '''Get physical disk offsets'''
        return self.physical_offsets.get(node, 0)

    def get_node_by_id(self, hid: Tuple[int, ...]) -> VfsNode | None:
        '''Node lookup for known registered nodes.'''
        with self._lock:
            return self.nodes_by_id.get(hid)
    
    ###-------------------- Navigator API ------------------###

    def snapshot_hids(self, hids: list[Tuple[int,...]]) -> HidSnapshot:
        '''Lock-protected snapshot. Return nodes already in the VFS'''
        resolved:   list[VfsNode] =[]
        unresolved: list[Tuple[int,...]] = []

        with self._lock:
            for hid in hids:
                node = self.nodes_by_id.get(hid)
                if node:
                    resolved.append(node)
                else:
                    unresolved.append(hid)

        return HidSnapshot(resolved, unresolved)
    
    def find_nearest_ancestor(self, hid: Tuple[int,...]) -> VfsNode | None:
        '''Return the nearest ancestor for an HID, the node that needs expanding'''
        with self._lock:
            best: VfsNode | None = None
            for depth in range(1, len(hid)):
                ancestor = self.nodes_by_id.get(hid[:depth])
                if ancestor:
                    best = ancestor
            return best

    def _find_child_by_path(self, parent: VfsNode, target: Tuple[int,...]) -> VfsNode | None:
        '''Scan children for match. Must be called from _lock.'''
        for child in parent.children:
            if child.hierarchical_id == target:
                return child
        return None

###------------------------------------ Status Tracker ---------------------------------------###

class ModTracker(QObject):
    '''Modification state tracker'''
    node_modified = pyqtSignal(VfsNode)
    node_staged   = pyqtSignal(VfsNode)
    node_unstaged = pyqtSignal(VfsNode)
    node_reverted = pyqtSignal(VfsNode)

    state_changed     = pyqtSignal(int, int) # format: (unstaged_count, staged_count)
    rebuild_initiated = pyqtSignal(list)

    def __init__(self) -> None:
        super().__init__()
        self.modified_nodes: set[VfsNode] = set()
        self.rebuild_queue:  set[VfsNode] = set()
        self._originals:     dict[VfsNode, bytes] = {}

    def _emit_state(self):
        self.state_changed.emit(len(self.modified_nodes), len(self.rebuild_queue))

    def mark_modified(self, node: VfsNode, new_data: bytes, original_data: bytes) -> None:
        if node not in self._originals:
            self._originals[node] = original_data
        node.pending_data = new_data
        node.status       = NodeStatus.MODIFIED
        self.modified_nodes.add(node)
        self.node_modified.emit(node)
        self._emit_state()

    def get_original(self, node: VfsNode) -> bytes:
        return self._originals.get(node, b'')

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
        self._originals.pop(node, None)
        node.clear_pending()
        node.status = NodeStatus.UNMODIFIED
        logger.info(f'Reverted changes for node: {node.hierarchical_id_str}')
        self.node_reverted.emit(node)
        self._emit_state()

    def confirm_and_rebuild(self) -> None:
        '''Triggered by Confirm button in staging page'''
        if not self.rebuild_queue:
            return
        staged_nodes = list(self.rebuild_queue)
        self.rebuild_initiated.emit(staged_nodes)

    def clear(self) -> None:
        '''Clear state when closing an ISO'''
        self.modified_nodes.clear()
        self.rebuild_queue.clear()
        self._originals.clear()
        self._emit_state()
