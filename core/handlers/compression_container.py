'''ContainerHandler Custom Compressor for tri-ace ps2 era games.'''
from dataclasses import dataclass
from core.registry import Registry
from core.contracts import ContainerHandler
from core.extension_overrides import generate_ext_overrides
from core.node import VfsNode
from core.workers import ActionDef, ActionType
from typing import Optional, Any, Callable

import logging
logger = logging.getLogger(f'radiata.{__name__}')

###------------------------------------ Wrapper -------------------------------------------###

@Registry.register(
    name='Tri-Ace Ps2 Compression Handler',
    extensions=('.slz', '.sle'),
    supported_actions=(
        ActionDef('Decompress', ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG)
    ))
class CompressorHandler(ContainerHandler):
    '''Wrapper for Compressor class'''

    @dataclass(slots=True)
    class SlzHeader:
        magic: str
        mode: int
        compressed_size: int
        decompressed_size: int
        next_file_offset: int

    def __init__(self, source: bytes, parent: VfsNode):
        super().__init__(source)
        self.handler_parent = parent
        self.raw_source = memoryview(self.handle.read())
    
    def get_file_tree(self) -> VfsNode:
        '''Return a node for the compressed file'''
        root = VfsNode() # dummy nodes

        extension_dict: dict[bytes, str] = generate_ext_overrides()
        current_offset = 0
        chunk_idx = 0

        while current_offset < len(self.raw_source):
            header_view = self.raw_source[current_offset: current_offset + 16]
            if len(header_view) < 16 or header_view[:3] not in [b'SLZ', b'SLE']:
                break
            
            header = bytes(header_view)
            header_obj = self.SlzHeader(
                magic='SLZ' if header[:3] == b'SLZ' else 'SLE',
                mode=header[3],
                compressed_size=int.from_bytes(header[4:8], 'little'),
                decompressed_size=int.from_bytes(header[8:12], 'little'),
                next_file_offset=int.from_bytes(header[12:16], 'little')
            )

            chunk_view = self.raw_source[current_offset : current_offset + header_obj.compressed_size + 16]
            compressor = RadiCompressor(chunk_view) # slice out header bytes for node
            inline_header = compressor.decompress(get_header=True)
            ext: str = next((match for sig, match in extension_dict.items() if inline_header.startswith(sig)), '.bin')

            node = VfsNode(
                name=f'{chunk_idx:04d}',
                offset=current_offset,
                size=header_obj.decompressed_size,
                header=inline_header,
                extension=ext,
                parent=root,
            )
            node.compressed_header = header_obj

            if inline_header[0:4] == b'1bcb': # bcb size + sector aligned to find next bcb
                sector_size = 0x800
                bcb_size = header_obj.compressed_size + len(header)
                aligned_size = (bcb_size + sector_size - 1) & ~(sector_size - 1)
                current_offset += aligned_size
                node.is_banked = True
            else:
                current_offset += header_obj.next_file_offset if header_obj.next_file_offset > 0 else (header_obj.compressed_size + 16)
            
            root.append_child(node)
            chunk_idx += 1
        return root

    def get_raw_node(self, node: VfsNode) -> bytes:
        '''Return a specific raw node'''
        if not node.compressed_header:
            logger.warning(f'No compressed header found for {node.name}')
            return b''
        
        compressed_size = node.compressed_header.compressed_size
        compressed_view = self.raw_source[node.offset : node.offset + compressed_size + 16]

        compressor = RadiCompressor(compressed_view)
        return compressor.decompress()

    def rebuild_node(self, root: VfsNode, staged_nodes: list[VfsNode], log_callback: Callable) -> bytes:
        '''Compress the bytes back to SLZ/SLE'''
        new_compressed_file = b''
        for i, child in enumerate(root.children):
            is_final_payload = i == len(root.children) - 1
            if child in staged_nodes and child.compressed_header: # Modified child
                if not child.pending_data:
                    continue
                raw_bytes = child.pending_data
                target_mode = child.compressed_header.mode
                is_encrypted = child.compressed_header.magic == 'SLE'

                if raw_bytes[2:6] == b'1bcb':
                    is_final_payload = True # mark (is_final_payload = true) so (next_file_offset = 0). There may be more payloads

                compressor = RadiCompressor(memoryview(raw_bytes), target_mode=target_mode, target_is_encrypted=is_encrypted, is_final_payload=is_final_payload)
                compressed_output = compressor.compress()

                padding_size = (-len(compressed_output)) & (0x800 - 1) if raw_bytes[2:6] == b'1bcb' else 0 # bcb sector alignment
                new_compressed_file += compressed_output + (b'\00' * padding_size)

            else: # Unmodified child
                compressed_size = child.compressed_header.compressed_size
                next_file_offset = child.compressed_header.decompressed_size
                chunk_size = next_file_offset if next_file_offset > 0 else (compressed_size + 16)
                original_chunk = bytearray(self.raw_source[child.offset:child.offset + chunk_size])
                if is_final_payload and next_file_offset != 0:
                    original_chunk[12:16] = (0).to_bytes(4, 'little')
                new_compressed_file += original_chunk

        log_callback(f'Rebuilt SLZ container. Original size:{len(self.raw_source)} New size:{len(new_compressed_file)}')
        return new_compressed_file

    def get_properties(self, node: VfsNode) -> str:
        if not node.children:
            node = self.get_file_tree()
        lines = ["Compressed File Properties:"]
        for child in node.children:
            if not child.compressed_header:
                continue
            c = child.compressed_header
            next_file_str = (
                None
                if not c.next_file_offset
                else str(c.next_file_offset)
            )
            ratio = (
                c.compressed_size / c.decompressed_size
                if c.decompressed_size > 0
                else 0
            )
            lines.extend([
                "",
                f"Mode: {c.mode}",
                f"Compressed Size: {c.compressed_size}",
                f"Decompressed Size: {c.decompressed_size}",
                f"Compression ratio: {(ratio * 100):.02f}%",
            ])
            if next_file_str:
                lines.append(f"Offset to next chained file: {next_file_str}")
        return "\n".join(lines)

    def execute_action(self, node: VfsNode, action_name: str, progress_callback, log_callback, **kwargs) -> Optional[Any]:
        if action_name == 'Decompress':
            if log_callback:
                log_callback(f'Decompressed {node.name}...')
            return self.get_file_tree()
        elif action_name == 'Properties':
            return self.get_properties(node)
        return None
    
    def get_buffer_data(self, node: VfsNode) -> memoryview:
        '''Specialized extraction for composite buffer nodes.'''
        target = node
        if not hasattr(target, 'compressed_header'):
            if node.parent and hasattr(node.parent, 'compressed_header'):
                target = node.parent
            else:
                logger.error(f'Cannot extract buffer: No compression metadata {node.name}')
                return memoryview(b'')
            
        continuous_buffer = bytearray()
        current_offset = target.offset

        while True:
            header_view = self.raw_source[current_offset : current_offset + 16]
            if len(header_view) < 16 or header_view[:3] not in [b'SLZ', b'SLE']:
                break

            header = bytes(header_view)
            compressed_size = int.from_bytes(target.compressed_header[4:8], 'little')
            next_file = int.from_bytes(header[12:16], 'little')

            chunk_view = self.raw_source[current_offset : current_offset + compressed_size + 16]
            compressor = RadiCompressor(chunk_view)
            continuous_buffer.extend(compressor.decompress())

            if next_file == 0:
                break

            current_offset += next_file

        return memoryview(continuous_buffer)
    
###------------------------------------ Compressor ------------------------------------------###
        
class RadiCompressor():
    '''Compressor class for all compression related processing.'''

    ###-------------------------- Parameters -----------------------###

    @dataclass(slots=True, frozen=True)
    class CompressorMode:
        '''Compressor parameters'''
        name: str
        mode: int
        window_size: int = 0
        literal_size: int = 1
        flag_bits: int = 8
        length_base: int = 0
        min_match: int = 0
        max_match: int = 0
        rle_enabled: bool = False
        word_aligned: bool = False
        # RLE specific
        rle_threshold: int = 0
        rle_short_min: int = 0
        rle_short_max: int = 0
        rle_long_min: int = 0
        rle_long_max: int = 0
        
    MODES: dict[int, CompressorMode] = {
        0: CompressorMode(name='STORE', mode = 0),

        1: CompressorMode(
            name='LZSS', mode=1, window_size=4096, length_base=3,
            min_match=3, max_match=18
        ),
        
        2: CompressorMode(
            name="LZSS+RLE", mode=2, window_size=4096, length_base=3,
            min_match=3, max_match=17, rle_enabled=True,
            rle_threshold=0xF0, rle_short_min=4, rle_short_max=18,
            rle_long_min=19, rle_long_max=274            
        ),

        3: CompressorMode(
            name="LZSS16", mode=3, window_size=8192, literal_size=2,
            flag_bits=16, length_base=2, min_match=4, max_match=34,
            word_aligned=True            
        )
    }

    SCRAMBLE_KEY = [0x66, 0x66, 0x54, 0x42, 0xB3, 0x79, 0xF0, 0xC7, 
                    0xE7, 0xD5, 0x1E, 0x4B, 0x7B, 0xA4, 0x1C, 0x7D]

    def __init__(self, data: memoryview, target_mode: int = 3, target_is_encrypted: bool = False, is_final_payload: bool = True):
        '''
        Initializes the compressor/decompressor.
        If an SLZ/SLE header is detected, it ignores target_mode and sets up for decompression.
        Otherwise, it sets up for compression using the target_mode.
        '''
        self.data = data
        self.hash_bits: int = 15
        self.hash_size: int = 1 << self.hash_bits
        self.is_encrypted = target_is_encrypted
        self.is_final_payload = is_final_payload

        # Auto-detection.
        # TODO in this section is for rebuilding. 
        # Flags may need implementation depending on rebuild strategy
        if self.data[:3] == b'SLZ' or self.data[:3] == b'SLE': # Decompress
            self.mode = self.MODES.get(self.data[3], self.MODES[0])
            self.is_compressed = True
            self.is_chained: bool = False #TODO
            self.is_packed: bool = False #TODO
        else: # Compress
            self.mode = self.MODES.get(target_mode, self.MODES[1])
            self.is_compressed = False
            self.is_chained: bool = False #TODO
            self.is_banked: bool = False #TODO
            self.is_packed: bool = False #TODO
            self.is_encrypted: bool = False #TODO

    ###------------------------- Compress ---------------------------###

    def _flush_flags(self, compressed: bytearray, flag_bits: int, token_buffer: bytearray) -> None:
        '''Write flags in LE'''
        compressed.append(flag_bits & 0xFF)
        if self.mode.name == 'LZSS16':
            compressed.append((flag_bits >> 8) &0xFF)
        compressed.extend(token_buffer)

    def _encode_match(self, offset: int, length: int) -> bytes:
        '''Write the offset, length for matches'''
        if self.mode.name == 'LZSS16':
            length //= 2
            offset //= 2

        length_code = length - self.mode.length_base
        offset_high = (offset >> 8) & 0x0F
        offset_low = offset & 0xFF
        byte1 = offset_low
        byte2 = (length_code << 4) | offset_high
        return bytes([byte1, byte2])

    def _encode_header(self, compressed_payload_length: int, uncompressed_length: int, next_file_offset: int) -> bytes:
        '''Write the header for the compressed output'''
        header = bytearray(16)
        header[:3] = b'SLZ'
        header[3] = (self.mode.mode & 0xFF)
        header[4:8] = compressed_payload_length.to_bytes(4, 'little')
        header[8:12] = (uncompressed_length.to_bytes(4, 'little'))
        header[12:16] = (next_file_offset.to_bytes(4, 'little'))
        return bytes(header)

    def _hash3(self, pos) -> int:
        '''get hash for byte'''
        return ((self.data[pos] << 10) ^ (self.data[pos+1] << 5) ^ self.data[pos+2]) & (self.hash_size - 1)
    
    def _find_best_match(self, pos, n, head, prev) -> tuple[int, int]:
        '''Search the hash chain for best match'''
        max_length = min(self.mode.max_match, n - pos)
        if max_length < self.mode.min_match:
            return 0, 0
        word_aligned = self.mode.word_aligned
        step = 2 if word_aligned else 1
        max_offset = self.mode.window_size - step
        h = self._hash3(pos)
        candidate = head[h]
        best_length = 0
        best_offset = 0
        chain_limit = 64
        first_byte = self.data[pos]
        while candidate >= 0 and chain_limit > 0:
            offset = pos - candidate
            if offset > max_offset:
                break
            if offset < step:
                candidate = prev[candidate % self.mode.window_size]
                chain_limit -= 1
                continue
            if word_aligned and offset % 2 != 0:
                candidate = prev[candidate % self.mode.window_size]
                chain_limit -= 1
                continue
            if self.data[candidate] == first_byte:
                ml = 0
                while ml < max_length and self.data[candidate + ml] == self.data[pos + ml]:
                    ml += 1
                if word_aligned:
                    ml -= ml % 2
                if ml > best_length:
                    best_length = ml
                    best_offset = offset
                    if best_length == max_length:
                        break
            candidate = prev[candidate % self.mode.window_size]
            chain_limit -= 1
        return best_length, best_offset

    def _update_hash(self, pos, n, head, prev) -> None:
        '''Maintain the linked hashes'''
        if pos + 2 < n:
            h = self._hash3(pos)
            prev[pos % self.mode.window_size] = head[h]
            head[h] = pos

    def compress(self) -> bytes:
        '''Pack data into compressed file'''
        if self.mode.name == 'STORE':
            next_file_offset = len(self.data) + 16 if not self.is_final_payload else 0
            header = self._encode_header(len(self.data), len(self.data), next_file_offset)
            return header + self.data
            
        compressed = bytearray()
        i = 0
        original_size = len(self.data)
        n = original_size
        token_buffer = bytearray()
        flag_bits = 0
        flag_count = 0

        if self.mode.word_aligned and n % 2 != 0:
            self.data = memoryview(bytes(self.data) + b'\x00')
            n += 1

        head = [-1] * self.hash_size
        prev = [-1] * self.mode.window_size

        while i < n:
            # deal with flags
            if flag_count == self.mode.flag_bits:
                self._flush_flags(compressed, flag_bits, token_buffer)
                flag_bits = 0
                flag_count = 0
                token_buffer.clear()

            RLE_triggered = False

            if self.mode.rle_enabled:
                run_length = 1
                max_rle_check = min(n, i + self.mode.rle_long_max)
                for j in range(i + 1, max_rle_check):
                    if self.data[j] == self.data[i]:
                        run_length += 1
                    else:
                        break

                if run_length >= self.mode.rle_short_min:
                    RLE_triggered = True
                    flag_bits |= (0 << flag_count)
                    flag_count += 1

                    fill =  self.data[i]

                    if run_length <= self.mode.rle_short_max: # RLE short
                        token_buffer.extend([fill, 0xF0 | (run_length - self.mode.length_base)])
                    else: # RLE long
                        token_buffer.extend([(run_length - self.mode.rle_long_min), 0xF0, fill])
                    for k in range(run_length):
                        self._update_hash(i + k, n, head, prev)
                    i += run_length

            if not RLE_triggered : # LZSS
                best_length, best_offset = self._find_best_match(i, n, head, prev)

                if best_length >= self.mode.min_match:  # LZSS match
                    flag_bits |= (0 << flag_count)
                    flag_count += 1
                    token_buffer.extend(self._encode_match(best_offset, best_length))
                    for k in range(best_length):
                        self._update_hash(i + k, n, head, prev)
                    i += best_length

                else: # Literal
                    flag_bits |= (1 << flag_count)
                    flag_count += 1
                    token_buffer.extend((self.data[i : i + self.mode.literal_size]))
                    self._update_hash(i, n, head, prev)
                    i += self.mode.literal_size
        # Flush remaining literals
        if flag_count > 0:
            self._flush_flags(compressed, flag_bits, token_buffer)

        # Emit end-of-stream sentinel (offset==0 back-reference).
        sentinel_flag = bytearray([0x00])
        if self.mode.name == 'LZSS16':
            sentinel_flag.append(0x00)  # 16-bit flag word
        sentinel_flag.extend(b'\x00\x00')  # offset=0, length=0 back-reference
        compressed.extend(sentinel_flag)

        next_file_offset = len(compressed) + 16 if not self.is_final_payload else 0
        header = self._encode_header(len(compressed), original_size, next_file_offset)
        
        if self.is_encrypted: # Convert SLEs back to SLE
            compressed = self._scramble_slz_payload(compressed)
            header = header[:2] + b'E' + header[3:]

        return bytes(header + compressed)

    def _scramble_slz_payload(self, data: bytes) -> bytes:
        '''Scramble the compressed'''
        KEY = self.SCRAMBLE_KEY
        scrambled = bytearray()
        mod_value = 0x03

        for i, byte in enumerate(data):
            key_value = KEY[i%16]
            scrambled_byte = (byte ^ key_value) & 0xFF
            modified_byte = (scrambled_byte + mod_value) & 0xFF
            scrambled.append(modified_byte)
            mod_value = (mod_value + 0x03) & 0xFF
        
        return bytes(scrambled)

    ###------------------------- Decompress --------------------------###

    def decompress(self, get_header: bool = False) -> bytes:
        '''Unpack compressed data get_header for limited metadata decompression'''
        if self.data[:3] == b'SLE': # Decryption
            self.data = self._unscramble_slz_payload()

        if self.mode.name == 'STORE': # STORE mode
            return bytes(self.data[16:])
        
        compressed_size = int.from_bytes(self.data[4:8], 'little')
        expected_size = int.from_bytes(self.data[8:12], 'little')
        pos = 16
        decompressed = bytearray()
        
        flags = 0
        bits_remaining = 0
        def get_next_flag_bit() -> int:
            nonlocal pos, flags, bits_remaining
            if bits_remaining == 0: # fill flag LZSS8
                if pos >= len(self.data):
                    logger.warning("Failed to terminate before EOF. Bailing out")
                    return 0
                low = self.data[pos]
                pos += 1
                if self.mode.name == 'LZSS16': # fill flag LZSS16
                    if pos >= len(self.data):
                        logger.warning("Failed to terminate before EOF. Bailing out")
                        return 0
                    high = self.data[pos]
                    pos += 1
                    flags = (high << 8) | low
                else:
                    flags = low
                bits_remaining = self.mode.flag_bits
            # consume flags
            bit = flags & 1
            flags >>= 1
            bits_remaining -= 1
            return bit

        max_size = compressed_size + 16
        if get_header: # Metadata toggle gate
            max_size = 64
        current_flags = []
        # loop over compressed payload and stop when hit expected size (else 0s from flag will pad EOF)
        while pos < max_size:        
            is_literal = (get_next_flag_bit() == 1)
            current_flags.append(1 if is_literal else 0)
            if len(current_flags) == self.mode.flag_bits:
                current_flags = []

            if is_literal: # literal
                decompressed.extend(self.data[pos:pos + self.mode.literal_size])
                pos += self.mode.literal_size

            else: # reference
                if pos + 2 > len(self.data):
                    logger.warning("Failed to terminate before EOF. Bailing out")
                    return bytes(decompressed)
                byte1 = self.data[pos]
                byte2 = self.data[pos+1]
                pos += 2
                if self.mode.rle_enabled and byte2 >= self.mode.rle_threshold: # RLE triggered
                    if byte2 > self.mode.rle_threshold: # short RLE
                        length = (byte2 & 0xF) + self.mode.length_base
                        fill = byte1
                    else: # long RLE
                        length = byte1 + self.mode.rle_long_min
                        if pos >= len(self.data):
                            raise EOFError("Ran out of self.data for long RLE fill")
                        fill = self.data[pos] # fill is byte 3
                        pos += 1
                    decompressed.extend(bytes([fill]) * length) 
                else: # LZSS triggered
                    length_code = (byte2 >> 4) & 0x0F
                    offset_low = byte1
                    offset_high = byte2 & 0x0F
                    offset = ((offset_high << 8) | offset_low )
                    length = length_code + self.mode.length_base
                    if self.mode.name == 'LZSS16': # LZSS16
                        offset *= 2
                        length = length * 2
                    if offset == 0:
                        break  # offset==0 is end-of-stream sentinel
                    else:
                        target = len(decompressed) - offset
                        for k in range(length):
                            if target + k < 0:
                                decompressed.append(0x00)  # ring buffer zero-init
                            else:
                                decompressed.append(decompressed[target + k])
            if len(decompressed) >= expected_size: # flush extra flags
                decompressed = decompressed[:expected_size]
                break

        decompressed = decompressed[:expected_size]
        if len(decompressed) != expected_size and not get_header:
            logger.warning(f"Size mismatch! Header uncompressed={hex(expected_size)}, "
                f"produced={hex(len(decompressed))}")
        return bytes(decompressed)

    def _unscramble_slz_payload(self) -> bytes:
        '''Decrypt compressed payload.'''
        KEY = self.SCRAMBLE_KEY
        comp_size = int.from_bytes(self.data[4:8], 'little')
        payload = self.data[16:]
        unscrambled = bytearray()
        mod_value = 0x03

        for i in range(min(comp_size, len(payload))):
            key_value = KEY[i % 16]
            modified_byte = (payload[i] - mod_value) & 0xFF
            unscrambled_byte = modified_byte ^ key_value
            unscrambled.append(unscrambled_byte)
            mod_value = (mod_value + 0x03) & 0xFF

        updated = bytes(self.data[:2]) + b'Z' + bytes(self.data[3:16]) + bytes(unscrambled)
        return updated