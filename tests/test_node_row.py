"""Tests for the cached _row field on VfsNode."""
import pytest
from core.node import VfsNode, VfsManager


# ---------------------------------------------------------------------------
# append_child path
# ---------------------------------------------------------------------------

def test_root_row_is_zero():
    root = VfsNode(name='root')
    assert root.row() == 0


def test_single_child_row():
    root = VfsNode(name='root')
    child = VfsNode(name='child0')
    root.append_child(child)
    assert child.row() == 0


def test_multiple_children_rows():
    root = VfsNode(name='root')
    children = [VfsNode(name=f'child{i}') for i in range(5)]
    for c in children:
        root.append_child(c)
    for expected_idx, c in enumerate(children):
        assert c.row() == expected_idx, (
            f"{c.name}: expected row {expected_idx}, got {c.row()}"
        )


def test_row_cached_matches_list_index():
    """_row must equal parent.children.index(child) for all children."""
    root = VfsNode(name='root')
    n = 10
    for i in range(n):
        root.append_child(VfsNode(name=f'c{i}'))
    for child in root.children:
        assert child.row() == root.children.index(child)


def test_nested_children_rows():
    """Row caching works at arbitrary depth."""
    root = VfsNode(name='root')
    parent = VfsNode(name='parent')
    root.append_child(parent)

    for i in range(4):
        parent.append_child(VfsNode(name=f'leaf{i}'))

    assert parent.row() == 0
    for i, leaf in enumerate(parent.children):
        assert leaf.row() == i


# ---------------------------------------------------------------------------
# insert_children path (VfsManager)
# ---------------------------------------------------------------------------

class _FakeQObject:
    """Minimal stand-in so VfsManager doesn't need a QApplication in tests."""


def _make_manager(root: VfsNode) -> VfsManager:
    """Create a VfsManager; suppress PyQt6 signals for testing."""
    return VfsManager(root)


def test_insert_children_sets_row(qtbot):
    """VfsManager.insert_children must set _row correctly."""
    root = VfsNode(name='root')
    vfs_entry = VfsNode(name='vfs_entry')
    vfs_entry.is_boundary = True
    root.append_child(vfs_entry)
    mgr = VfsManager(root)

    new_nodes = [VfsNode(name=f'n{i}', size=1, offset=i) for i in range(6)]
    mgr.insert_children(root, new_nodes)

    for expected_idx, child in enumerate(root.children):
        assert child.row() == expected_idx, (
            f"{child.name}: expected row {expected_idx}, got {child.row()}"
        )


def test_insert_children_appended_after_existing(qtbot):
    """Row indices are offset correctly when children already exist."""
    root = VfsNode(name='root')
    vfs_entry = VfsNode(name='vfs_entry')
    vfs_entry.is_boundary = True
    root.append_child(vfs_entry)
    mgr = VfsManager(root)

    first_batch = [VfsNode(name=f'first{i}', size=1, offset=i) for i in range(3)]
    mgr.insert_children(root, first_batch)

    second_batch = [VfsNode(name=f'second{i}', size=1, offset=10 + i) for i in range(4)]
    mgr.insert_children(root, second_batch)

    for expected_idx, child in enumerate(root.children):
        assert child.row() == expected_idx
