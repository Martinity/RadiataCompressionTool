"""Shared test helpers for building real SLZ/SLE containers in memory."""
from core.handlers.compression_container import RadiCompressor

def build_slz_container(chunks, encrypted=False):
    """chunks: list of (payload_bytes, mode). Returns a multi-chunk SLZ/SLE blob
    with next_file_offset chained correctly across all but the last chunk."""
    blobs = []
    for i, (payload, mode) in enumerate(chunks):
        is_final = i == len(chunks) - 1
        comp = RadiCompressor(memoryview(payload), target_mode=mode,
                              is_final_payload=is_final).compress()
        blobs.append(bytearray(comp))
    # Patch next_file_offset = total chunk length for every non-final chunk.
    for i, blob in enumerate(blobs):
        nfo = 0 if i == len(blobs) - 1 else len(blob)
        blob[12:16] = nfo.to_bytes(4, "little")
    return b"".join(bytes(b) for b in blobs)
