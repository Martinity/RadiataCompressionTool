# tests/test_compressor_rebuild.py — banked rebuild padding
from core.handlers.compression_container import CompressorHandler, RadiCompressor
from core.node import VfsNode

def _make_handler(container: bytes):
    h = CompressorHandler.__new__(CompressorHandler)   # bypass Qt __init__ wiring
    h.raw_source = memoryview(container)
    class _TH:                                          # minimal task_handle stub
        class _Sig:
            def emit(self, *a): pass
        log_message = _Sig()
    h.task_handle = _TH()
    return h

def test_banked_rebuild_is_sector_aligned():
    payload = b'1bcb' + b'\x00' * 2000                  # decompressed payload w/ banked magic
    container = RadiCompressor(memoryview(payload), target_mode=1, is_final_payload=True).compress()
    root = VfsNode(); child = VfsNode(name='0000', parent=root); root.children.append(child)
    child.parent_header = CompressorHandler.SlzHeader('SLZ', 1, len(container)-16, len(payload), 0)
    child.offset = 0
    child.pending_data = payload
    h = _make_handler(container)
    out = h.rebuild_node(root, [child])
    assert len(out) % 0x800 == 0                        # banked output is 0x800-aligned
