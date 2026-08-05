'''Node metadata. Contains three supporting classes, VfsNode (File data), VfsManager (Relational data), ModTracker (Mutation tracking)'''
from __future__ import annotations

import threading
from enum import Enum, auto
from typing import NamedTuple, Callable, TYPE_CHECKING
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
        category:  tuple[str, ...] = ('Unknown',),
        offset:    int = 0,
        size:      int = 0,
        header:    bytes = b'',
        extension: str | None = None,
        parent:    VfsNode | None = None,
        hid:       tuple[int, ...] = (),
        target:    tuple[int, ...] | None = None,
    ):
        self.name     = name                                # semantic name from overrides
        self.category = category                            # semantic category derived from disk index
        self.parent   = parent                              # parent node (None = Root)
        self.children: list[VfsNode] = []                   # children node(s)

        self.offset = offset                                # Relative offset into parent
        self.size   = size                                  # Size of node in bytes (VirtualFile=disk[offset:offset+size])
        self.target: tuple[int,...] | None = target         # Header HID for unpacking datacenter

        self.extension = extension                          # extension from override saved in radi_metadata
        self.parent_header: CompressorHandler.SlzHeader | bytes | None = None    # Original header of the parent node used for rebuilding children
        self.logical_id: int | None = None                  # Logical ID from the TOC used for ISO rebuilding

        self._id_path: tuple[int, ...] = hid                # hierarchical id (root, sub, subsub)

        self.status = NodeStatus.UNMODIFIED                 # node state
        self.pending_data: bytes | None = None              # cached data

        self.is_physical   = False                          # Has physical address
        self.is_hidden     = False                          # Hide node in UI (file system related or null nodes by default)
        self.is_boundary   = False                          # The entrypoint node for the VFS (always the last node appended to root)

        self.expansion_pending: bool = False                 # Threading active bool
        self._expansion_event: threading.Event | None = None # Threading event for active thread
        self._expansion_task_active: bool = False            # True while a worker task is running (main-thread only)

        self._row: int = 0                                   # Cached row index within parent

    def append_child(self, child: VfsNode):
        '''
        Called to add a child node to this node.
        Keeps separate HID increments for ISO and VFS nodes, split by boundary flag.
        '''
        self.children.append(child)
        child.parent = self
        child._row = len(self.children) - 1

        if self.is_boundary:
            child._id_path = (child._row,)
        else:
            child._id_path = self._id_path + (child._row,)

    @property
    def hierarchical_id(self) -> tuple[int, ...]:
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
        return self._row

    def begin_expansion(self) -> threading.Event:
        '''Mark expansion in progress. Return wait event.
        If an expansion is already pending and an event exists, return the existing
        event so that a second waiter blocks on the same event the running task will set.'''
        if self.expansion_pending and self._expansion_event is not None:
            return self._expansion_event
        self._expansion_event = threading.Event()
        self.expansion_pending = True
        return self._expansion_event

    def finish_expansion(self) -> None:
        '''Signal expansion complete'''
        self.expansion_pending = False
        self._expansion_task_active = False
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
    unresolved: list[tuple[int, ...]]   # HIDs whose parent need expansion

class VfsManager(QObject):
    '''
    Holds O(N) node lookup tables for VFS and ISO nodes.

    Registration is valid for both VFS and ISO nodes during init.
    After which, the only way to register new ISO nodes is to add a child to root.
    '''
    insert_start      = pyqtSignal(VfsNode, int, int) # (parent, first_row, last_row)
    insert_finished   = pyqtSignal()

    request_extension = pyqtSignal(VfsNode)           # Request extension for a node that has '.bin' extension

    def __init__(
        self,
        root:          VfsNode,
        vfs_entry:     VfsNode,
        node_enricher: Callable[[VfsNode], None] | None = None,
    ) -> None:
        super().__init__()
        self.root        = root
        self.vfs_entry   = vfs_entry
        self.enrich_node = node_enricher
        self._lock       = threading.RLock()

        self.vfs_nodes_by_id:  dict[tuple[int, ...], VfsNode] = {}  # VFS-only hid lookup map
        self.iso_nodes_by_id:  dict[tuple[int, ...], VfsNode] = {}  # ISO-only hid lookup map, only mutated during init
        self.physical_offsets: dict[VfsNode, int] = {}              # Physical disk map
        self._register_recursive(self.root, self.iso_nodes_by_id)   # Register physical nodes with VFS initilization

    ###--------------------- Registration ----------------------###

    def _register_recursive(self, node: VfsNode, target_dict: dict[tuple[int, ...], VfsNode]) -> None:
        '''
        Register node and all its children.
        Node is registered in the target_dict (vfs or iso).

        After initialization, must be called inside _lock.
        '''
        target_dict[node.hierarchical_id] = node
        self.physical_offsets[node] = node.offset

        next_dict = self.vfs_nodes_by_id if node is self.vfs_entry else target_dict
        for child in node.children:
            self._register_recursive(child, next_dict)

    def insert_children(self, parent: VfsNode, new_children: list[VfsNode]) -> None:
        '''
        Update the VFS and signal to the tree model.

        Enforces all iso level nodes to be registered at initialization, not lazily via insert_children.
        Because of the registration enforcement all iso level nodes are depth 1 only.
        Not really a problem for anything unless the project expands to splitting the kernel image or main ELF.
        Could also be a problem if some kind of custom ISO level structure is modded in.
        '''
        if not new_children:
            return
        assert parent is not self.vfs_entry, (
            'vfs_entry\'s children must be populated at initialization, not lazily via insert_children'
        )
        with self._lock:
            base_idx = len(parent.children)
            self.insert_start.emit(parent, base_idx, base_idx + len(new_children) - 1)
            for i, child in enumerate(new_children): # Add the nodes to the file system / tree
                child.parent = parent
                child._id_path = parent._id_path + (base_idx + i,)
                child._row = base_idx + i
                child.is_hidden = bool(parent.is_hidden or not child.size or child.offset == -1)
                if self.enrich_node:
                    self.enrich_node(child)
                if not child.extension and not child.is_hidden:
                    self.request_extension.emit(child)
                parent.children.append(child)
                self._register_recursive(child, self.vfs_nodes_by_id)
            self.insert_finished.emit()

    def enrich_initial_tree(self) -> None:
        '''
        Walk the VFS after initialization enriching nodes with metadata.
        No need for extra metadata on ISO level nodes since the root
        directory + system.cnf supplies enough context.
        '''
        if not self.enrich_node:
            return
        for child in self.root.children[-1].children:
            self.enrich_node(child)
            if not child.extension and not child.is_hidden:
                self.request_extension.emit(child)
        logger.debug('VfsManager.enrich_initial_tree: complete')

    ###--------------------- Lookup -----------------------###

    def get_offset(self, node: VfsNode) -> int:
        '''Get physical disk offsets'''
        return self.physical_offsets.get(node, 0)

    def get_vfs_node_by_id(self, hid: tuple[int, ...]) -> VfsNode | None:
        '''Node lookup for known vfs registered nodes.'''
        with self._lock:
            return self.vfs_nodes_by_id.get(hid)

    def get_iso_node_by_id(self, hid: tuple[int, ...]) -> VfsNode | None:
        '''Node lookup for known iso registered nodes.'''
        with self._lock:
            return self.iso_nodes_by_id.get(hid)

    ###-------------------- Navigator API ------------------###

    def snapshot_hids(self, hids: list[tuple[int,...]]) -> HidSnapshot:
        '''Lock-protected snapshot. Return nodes already in the VFS'''
        resolved:   list[VfsNode] =[]
        unresolved: list[tuple[int,...]] = []

        with self._lock:
            for hid in hids:
                node = self.vfs_nodes_by_id.get(hid)
                if node:
                    resolved.append(node)
                else:
                    unresolved.append(hid)

        return HidSnapshot(resolved, unresolved)

    def find_nearest_ancestor(self, hid: tuple[int,...]) -> VfsNode | None:
        '''Return the nearest ancestor for an HID, the node that needs expanding'''
        with self._lock:
            best: VfsNode | None = None
            for depth in range(1, len(hid)):
                ancestor = self.vfs_nodes_by_id.get(hid[:depth])
                if ancestor:
                    best = ancestor
            return best

    def _find_child_by_path(self, parent: VfsNode, target: tuple[int,...]) -> VfsNode | None:
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

    state_changed     = pyqtSignal(int, int)   # (unstaged_count, staged_count)
    rebuild_initiated = pyqtSignal(list, bool) # (staged_nodes, slimmed)

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

    def confirm_and_rebuild(self, slimmed: bool = False) -> None:
        '''Triggered by Confirm button in staging page'''
        if not self.rebuild_queue:
            return
        staged_nodes = list(self.rebuild_queue)
        self.rebuild_initiated.emit(staged_nodes, slimmed)

    def clear(self) -> None:
        '''Clear state when closing an ISO'''
        for node in self.modified_nodes | self.rebuild_queue | set(self._originals):
            node.clear_pending()
        self.modified_nodes.clear()
        self.rebuild_queue.clear()
        self._originals.clear()
        self._emit_state()
