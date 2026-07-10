# tests/test_rebuild_state.py
from core.node import VfsNode, ModTracker


def test_clear_resets_pending_on_tracked_nodes():
    t = ModTracker()
    n = VfsNode(name="leaf")
    t.mark_modified(n, new_data=b"new", original_data=b"old")
    assert n.pending_data == b"new"
    t.clear()
    assert n.pending_data is None
    assert not t.modified_nodes and not t._originals
