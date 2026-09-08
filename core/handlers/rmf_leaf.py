'''RMF message data handler.
Early version that round-trips on text edits.

Opcode data structures are not the best.
RmfEditor should initialize with the raw table requests and pass the data to this handler.
Without the table datas, the handler will have to rely on defaults. This handler should not
receive the glyph data itself, that should go to a dedicated handler for glyph rendering.
There is a good chance that the text encoding round trip for special cases. I didn't test on
packets with multiple speakers.
'''
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Any

from core.contracts import LeafHandler
from core.node import VfsNode
from core.registry import Registry
from core.workers import ActionDef, ActionType

import logging
logger = logging.getLogger(f'radiata.{__name__}')

MAGIC = b'RMF1'
CMD_SUBCOMMAND_PROPER_NOUN = 0x06

###--------------------------------------------- Opcodes -----------------------------------------------###

PUTTEXT = {
    0x00: {
        'name': 'End',
        'payload_size': 0,
        'description': 'End the message sequence. Clear pointers and set render state 2.'
    },
    0x01: {
        'name': 'Space',
        'payload_size': 0,
        'description': 'English: Advance by (width * 12) >> 12\nJapanese: Advance by a cell (width * 32) >> 12'
    },
    0x02: {
        'name': 'Color',
        'payload_size': 16,
        'description': 'Word 1 colors the text, word 2 colors the drop shadow. R G B A little endian.'
    },
    0x03: {
        'name': 'Dimensions',
        'payload_size': 16,
        'description': 'Glyph cell width then height in U8.8 fixed point format. Word 2 multiplies into word 1.'
    },
    0x04: {
        'name': 'Position',
        'payload_size': 16,
        'description': 'Word 1 stores the anchor for the text (x, y) in 16bit signed int. Word 2 is relatve cursor position.'
    },
    0x05: {
        'name': 'Text Style',
        'payload_size': 0,
        'description': 'Stored subcommand % 3 into +0x0C. Every character code is stamped with the style in the high nibble.'
    },
    0x06: {
        'name': 'Signal',
        'payload_size': 8,
        'description': '_CbSignal fallthrough.'
    },
    0x07: {
        'name': 'Text Speed',
        'payload_size': 8,
        'description': 'Text speed in U8.8 fixed point format.'
    },
    0x09: {
        'name': 'Style Save/Load',
        'payload_size': 8,
        'description': 'The stored style. Used as a bitfield containing; text color, \
        shadow color, pen x, pen y, glyph width, glyph height, text style, and text speed.'
    },
    0x0A: {
        'name': 'Newline',
        'payload_size': 0,
        'description': 'Advance the pen y and reset the pen x to anchor.'
    }
}

CTALK = {
    0x00: {
        'name': '_End',
        'payload_size': 0,
        'description': 'End the message. Destruct the talk session.'
    },
    0x01: {
        'name': '_CbNop',
        'payload_size': 0,
        'description': 'Space NOP'
    },
    0x02: {
        'name': '_CbNop',
        'payload_size': 16,
        'description': 'Color'
    },
    0x03: {
        'name': '_CbNop',
        'payload_size': 16,
        'description': 'Dimensions'
    },
    0x04: {
        'name': '_CbNop',
        'payload_size': 16,
        'description': 'Position'
    },
    0x05: {
        'name': '_CbNop',
        'payload_size': 0,
        'description': 'TextStyle'
    },
    0x06: {
        'name': '_CbSignal',
        'payload_size': 8,
        'description': 'State polling. Used for syncronization.'
    },
    0x07: {
        'name': '_CbNop',
        'payload_size': 8,
        'description': 'TextSpeed'
    },
    0x08: {
        'name': '_CbNop',
        'payload_size': 0,
        'description': '---'
    },
    0x09: {
        'name': '_CbNop',
        'payload_size': 8,
        'description': 'StyleSave / StyleRestore'
    },
    0x0A: {
        'name': '_CbNop',
        'payload_size': 0,
        'description': 'Newline'
    },
    0x0B: {
        'name': '_CbNop',
        'payload_size': 0,
        'description': '---'
    },
    0x0C: {
        'name': '_CbNop',
        'payload_size': 0,
        'description': '---'
    },
    0x0D: {
        'name': '_CbNop',
        'payload_size': 0,
        'description': '---'
    },
    0x0E: {
        'name': '_CbNop',
        'payload_size': 0,
        'description': '---'
    },
    0x0F: {
        'name': '_CbNop',
        'payload_size': 0,
        'description': '---'
    },
    0x10: {
        'name': '_CbSpeaker',
        'payload_size': 16,
        'description': 'Word 1 stores the speaker table index. Word 2 stores the mode in a bit field.'
    },
    0x11: {
        'name': '_CbBupVisible',
        'payload_size': -1,
        'description': 'Variable payload size. Word 1 stores the speaker index. Then stores the visibility state.'
    },
    0x12: {
        'name': '_CbBupDirection',
        'payload_size': -1,
        'description': 'Variable payload size. Word 1 stores the speaker index. Resolves the bustup body.'
    },
    0x13: {
        'name': '_CbBupPosition',
        'payload_size': -1,
        'description': 'Variable payload size. Word 1 stores the speaker index. Byte-identical to _CbBupDirection.'
    },
    0x14: {
        'name': '_CbBFace',
        'payload_size': 16,
        'description': 'Word 1 stores the speaker index. Word 2 stores the face mode (expression).'
    },
    0x15: {
        'name': '_CbSpeak',
        'payload_size': 16,
        'description': 'Word 1 stores the speaker index. Word 2 stores the speak timing flags for independent speech.'
    },
    0x16: {
        'name': '_CbSpeakingCycle',
        'payload_size': 16,
        'description': 'Word 1 stores the speaker index. Word 2 stores the speaking cycle timings.'
    },
    0x17: {
        'name': '_CbEmotion',
        'payload_size': -1,
        'description': 'Variable payload size. Word 1 stores the speaker index. Next byte stores the emotion mode flags \
        Word 2 stores magnitude of the emotion.'
    },
    0x18: {
        'name': '_CbErr',
        'payload_size': 0,
        'description': 'Assertion trap 0x18'
    },
    0x19: {
        'name': '_CbErr',
        'payload_size': 0,
        'description': 'Assertion trap 0x19'
    },
    0x1A: {
        'name': '_CbEyeMove',
        'payload_size': -1,
        'description': 'Variable payload size. Half-Word@2 stores the speaker index. Byte@4 stores the loop target. \
        Byte@5 stored the eye move duration. Half-Word@6 stores the X/Y coordinates of the eye move target.'
    },
    0x1B: {
        'name': '_CbErr',
        'payload_size': 0,
        'description': 'Assertion trap 0x1B'
    },
    0x1C: {
        'name': '_CbEyeNumber',
        'payload_size': 16,
        'description': 'Half-Word@2 stores the speaker index. Byte@4 stores the eye variant.'
    },
    0x1D: {
        'name': '_CbBupReleaseCount',
        'payload_size': 16,
        'description': 'Half-Word@2 stores the speaker index. Half-Word@4 stores the bustup ref/release count.'
    },
    0x1E: {
        'name': '_CbNameWindow',
        'payload_size': 32,
        'description': 'Half-Word@2, speaker index. Byte@4, style bit mask. HW@6, signed X/Y anchor point. HW@8, override name id'
    },
    0x1F: {
        'name': '_CbErr',
        'payload_size': 0,
        'description': 'Assertion trap 0x1F'
    },
    0x20: {
        'name': '_CbWaitTime',
        'payload_size': 16,
        'description': 'HW@2, wait time in milliseconds (inf=0xFFFF). HW@4, waiting flags (blinking curosr, timed wait...)'
    },
    0x21: {
        'name': '_CbErr',
        'payload_size': 0,
        'description': 'Assertion trap 0x21'
    },
    0x22: {
        'name': '_CbWaitSelect',
        'payload_size': 16,
        'description': 'HW@2, time limit in milliseconds (0=inf). HW@4, option count low 7 bites, bit 0x80 cursor state flag.'
    },
    0x23: {
        'name': '_CbProhibitSkip',
        'payload_size': 8,
        'description': 'B@2, flag to set the prohibit skip flag: 0=can skip.'
    },
    0x24: {
        'name': '_CbWaitSelect2',
        'payload_size': 16,
        'description': 'Same as WaitSelect but with a default choice first.'
    },
    0x25: {
        'name': '_CbSentence',
        'payload_size': 16,
        'description': 'HW@2, flags. HW@4, glyph count.'
    },
    0x26: {
        'name': '_CbProper',
        'payload_size': 16,
        'description': 'Called with the first callback for a named entity to get and set a \
        proper name into a scratch buffer. To be utilized throughout the message sequence.'
    },
    0x27: {
        'name': '_CbJump',
        'payload_size': -1,
        'description': 'B@2 + upto 6 mode Words. Acts as a message sequence prologue. Executing \
        setup before the message packets. Jumping to the first standard packet (non-0F7*) opcode'
    },
    0x28: {
        'name': '_CbPlaySe',
        'payload_size': 16,
        'description': 'B@2, channel. HW@4, sound ID. Channel greater than 0x10 causes a signed read.'
    },
    0x29: {
        'name': '_CbErr',
        'payload_size': 0,
        'description': 'Assertion trap 0x29'
    },
}

###--------------------------------------------- Glyph decode/encode -----------------------------------------------###

# This should be replaced with the real glyph encoding tables
ENGLISH_GLYPH_ENCODE: dict[str, int] = {}
for _c in range(ord('A'), ord('Z') + 1):
    ENGLISH_GLYPH_ENCODE[chr(_c)] = 0x10 + (_c - ord('A') + 83)
for _c in range(ord('a'), ord('z') + 1):
    ENGLISH_GLYPH_ENCODE[chr(_c)] = 0x10 + (_c - ord('a') + 109)
ENGLISH_GLYPH_ENCODE['.'] = 0x10 + 3
ENGLISH_GLYPH_ENCODE[','] = 0x10 + 2
ENGLISH_GLYPH_ENCODE['?'] = 0x10 + 7
ENGLISH_GLYPH_ENCODE['!'] = 0x10 + 8
ENGLISH_GLYPH_ENCODE["'"] = 0x10 + 17
GLYPH_PLACEHOLDER_RE      = re.compile(r'\{G:([0-9A-Fa-f]{1,4})\}')

MAX_GLYPHS_BUDGET         = 256 # temp value test against real data


def decode_glyph(raw_word: int, is_english: bool = True) -> str:
    '''Decode english glyph to character'''
    b0 = raw_word & 0xFF
    b1 = (raw_word >> 8) & 0xFF

    glyph_index = (b0 - 0x10) + (b1 & 0x3F) * 240
    if b1 & 0x80:
        return f'[tile {glyph_index}]'
    if not is_english:
        return f'[{glyph_index}]'

    if 83 <= glyph_index <= 108:
        return chr(glyph_index - 18) # A - Z
    if 109 <= glyph_index <= 134:
        return chr(glyph_index - 12) # a - z
    if glyph_index == 3: return '.'
    if glyph_index == 2: return ','
    if glyph_index == 7: return '?'
    if glyph_index == 8: return '!'
    if glyph_index == 17: return "'"
    return f'[{glyph_index}]'


def describe_token(token: RmfToken) -> str:
    '''Human-readable name for a token from PUTTEXT/CTALK (metatdata)'''
    if token.is_glyph:
        return 'Glyph'
    b0 = token.raw_word & 0xFF
    b1 = (token.raw_word >> 8) & 0xFF
    subcmd = b1 & 0x0F
    if b0 <= 0x0A:
        entry = PUTTEXT.get(b0)
        return entry['name'] if entry else f'LayerA Unknown ({b0:02X})' # type: ignore name can only be str
    if b0 == 0x0E:
        entry = CTALK.get(0x10 + subcmd)
        return _friendly_ctalk_name(entry['name']) if entry else f'BupCtrl Unknown ({subcmd:02X})' # type: ignore name can only be str
    if b0 == 0x0F:
        entry = CTALK.get(0x20 + subcmd)
        return _friendly_ctalk_name(entry['name']) if entry else f'FlowCtrl Unknown ({subcmd:02X})' # type: ignore name can only be str
    return f'Unknown ({b0:02X})'


def _friendly_ctalk_name(name: str) -> str:
    if name.startswith('_Cb'):
        return name[3:]
    return name.lstrip('_')

###--------------------------------------------- Structs -----------------------------------------------###

@dataclass
class SpeakerEntry:
    '''Represents a speaker entry in the RMF file's speaker table'''
    character_id: int
    variant_id:   int

    @classmethod
    def from_bytes(cls, data: bytes) -> SpeakerEntry:
        char_id, var_id = struct.unpack('<HH', data)
        return cls(char_id, var_id)

    def to_bytes(self) -> bytes:
        '''Pack speaker entry into bytes'''
        return struct.pack('<HH', self.character_id, self.variant_id)


@dataclass
class RmfToken:
    '''Represents a token in the RMF file's token stream'''
    is_glyph: bool
    raw_word: int
    command_bytes: bytes | None = None

    def to_bytes(self) -> bytes:
        '''Pack token into bytes'''
        if self.is_glyph:
            return struct.pack('<H', self.raw_word)
        if self.command_bytes:
            return self.command_bytes
        return struct.pack('<H', self.raw_word)


@dataclass
class TextRun:
    '''A continuous run of text in the RMF file's token stream
    This captures space and newline commands + text glyphs'''
    start: int
    end:   int
    text:  str


@dataclass
class RmfPacket:
    glyph_budget: int
    speakers:     list[SpeakerEntry]
    tokens:       list[RmfToken]

    @classmethod
    def from_bytes(cls, data: bytes) -> RmfPacket:
        glyph_budget, speaker_count = struct.unpack('<II', data[:8])
        # Speaker entries
        speakers = []
        offset   = 8
        for _ in range(speaker_count):
            speakers.append(SpeakerEntry.from_bytes(data[offset:offset+4]))
            offset += 4
        # Token stream
        tokens = []
        while offset < len(data):
            word = struct.unpack('<H', data[offset:offset+2])[0]
            b0 = word & 0xFF
            b1 = (word >> 8) & 0xFF
            if b0 >= 0x10:
                tokens.append(RmfToken(is_glyph=True, raw_word=word))
                offset += 2
            else:
                if b0 == 0x0F and (b1 & 0x0F) == CMD_SUBCOMMAND_PROPER_NOUN:
                    payload_words = b1
                else:
                    payload_words = (b1 >> 4) & 0xF
                record_size = 2 + (payload_words * 2)
                cmd_data = data[offset : offset + record_size]
                tokens.append(RmfToken(is_glyph=False, raw_word=word, command_bytes=cmd_data))
                offset += record_size
        return cls(glyph_budget, speakers, tokens)

    def to_bytes(self) -> bytes:
        out = bytearray()
        out.extend(struct.pack('<II', self.glyph_budget, len(self.speakers)))
        for speaker in self.speakers:
            out.extend(speaker.to_bytes())
        for token in self.tokens:
            out.extend(token.to_bytes())
        ends_with_terminator = (
            bool(self.tokens)
            and not self.tokens[-1].is_glyph
            and self.tokens[-1].raw_word == 0x0000
        )
        if not ends_with_terminator:
            out.extend(struct.pack('<H', 0x0000))
        padding = (4 - (len(out) % 4)) % 4
        out.extend(b'\x00' * padding)
        return bytes(out)

    @staticmethod
    def _is_text_token(token: RmfToken) -> bool:
        return bool(token.is_glyph or (token.raw_word & 0xFF) in (0x01, 0x0A))

    def get_text_runs(self, is_english: bool) -> list[TextRun]:
        runs: list[TextRun] = []
        start: int | None = None
        for i, token in enumerate(self.tokens):
            if self._is_text_token(token):
                if start is None:
                    start = i
            else:
                if start is not None:
                    runs.append(TextRun(start, i, self._run_to_text(start, i, is_english)))
                    start = None
        if start is not None:
            runs.append(TextRun(start, len(self.tokens), self._run_to_text(start, len(self.tokens), is_english)))
        return runs

    def _run_to_text(self, start: int, end: int, is_english: bool) -> str:
        '''Collect a valid text character sequence'''
        chars = []
        for token in self.tokens[start:end]:
            if token.is_glyph:
                chars.append(self.glyph_to_mutable_text(token.raw_word, is_english))
            else:
                b0 = token.raw_word & 0xFF
                if b0 == 0x01:
                    chars.append(' ')
                elif b0 == 0x0A:
                    chars.append('\n')
        return ''.join(chars)

    def get_display_text(self, is_english: bool) -> str:
        runs = self.get_text_runs(is_english)
        if len(runs) <= 1:
            return runs[0].text if runs else ''
        return ''.join(run.text for run in runs)

    def glyph_to_mutable_text(self, raw_word: int, is_english: bool) -> str:
        '''One editable glyph cell text box'''
        decoded = decode_glyph(raw_word, is_english)
        if is_english and len(decoded) == 1:
            return decoded
        return f'{{G: {raw_word:04X}}}'

    def token_to_mutable_text(self, token: RmfToken, is_english: bool) -> str:
        if token.is_glyph:
            return self.glyph_to_mutable_text(token.raw_word, is_english)
        b0 = token.raw_word & 0xFF
        if b0 == 0x01:
            return ' '
        if b0 == 0x0A:
            return '\n'
        raw = token.command_bytes if token.command_bytes else struct.pack('<H', token.raw_word)
        return f'{{C: {raw.hex().upper()}}}'

    def get_full_msg(self, is_english: bool = True) -> str:
        return ''.join(self.token_to_mutable_text(t, is_english) for t in self.tokens)


    def mutable_text_to_tokens(self, text: str) -> list[RmfToken]:
        '''Inverse of _run_to_text. Raises on encode failure'''
        tokens: list[RmfToken] = []
        i = 0
        while i < len(text):
            m = GLYPH_PLACEHOLDER_RE.match(text, i)
            if m:
                tokens.append(RmfToken(is_glyph=True, raw_word=int(m.group(1), 16)))
                i = m.end()
                continue
            ch = text[i]
            if ch == '\n':
                tokens.append(RmfToken(is_glyph=False, raw_word=0x000A, command_bytes=bytes([0x0A, 0x00])))
            elif ch == ' ':
                tokens.append(RmfToken(is_glyph=False, raw_word=0x0001, command_bytes=bytes([0x01, 0x00])))
            elif ch == '\r':
                pass
            else:
                word = ENGLISH_GLYPH_ENCODE.get(ch)
                if word is None:
                    raise ValueError(f'no glyph mapped for: {ch!r}')
                tokens.append(RmfToken(is_glyph=True, raw_word=word))
            i += 1
        return tokens

    def find_unsupported_chars(self, text: str) -> list[str]:
        '''Characters in an edited run that _text_to_tokens would not encode.
        Checked before changes are applied to ensure success/failure state.'''
        unsupported: list[str] = []
        i = 0
        while i < len(text):
            m = GLYPH_PLACEHOLDER_RE.match(text, i)
            if m:
                i = m.end()
                continue
            ch = text[i]
            if ch not in ('\n', ' ', '\r') and ch not in ENGLISH_GLYPH_ENCODE:
                unsupported.append(ch)
            i += 1
        return unsupported

    def encode_text_mutation(
        self,
        text: str,
        glyph_budget: int = MAX_GLYPHS_BUDGET,
        is_english: bool = True,
    ) -> int:
        runs = self.get_text_runs(is_english)
        if len(runs) <= 1:
            pieces = [text]
        else:
            pieces = text.split('\n')
            if len(pieces) != len(runs):
                raise ValueError(
                    f'this message has {len(runs)} separately-positioned parts '
                    f'(dialogue lines or choice options); the edited text must keep '
                    f'exactly {len(runs)} lines (found {len(pieces)}) since the commands '
                    f'between parts aren\'t editable here yet'
                )
        unsupported: list[str] = []
        for piece in pieces:
            unsupported.extend(self.find_unsupported_chars(piece))
        if unsupported:
            shown = ', '.join(repr(c) for c in dict.fromkeys(unsupported))
            raise ValueError(f'No mappings for characters in message: {shown[:100]}')
        new_tokens: list[RmfToken] = []
        run_idx = 0
        i = 0
        while i < len(self.tokens):
            if run_idx < len(runs) and runs[run_idx].start == i:
                new_tokens.extend(self.mutable_text_to_tokens(pieces[run_idx]))
                i = runs[run_idx].end
                run_idx += 1
            else:
                new_tokens.append(self.tokens[i])
                i += 1
        total_glyphs = sum(1 for t in new_tokens if t.is_glyph)
        if total_glyphs > glyph_budget:  # Might be better to truncate, test UX
            raise ValueError(f'Too many glyphs: {total_glyphs} (max: {glyph_budget})')
        self.tokens = new_tokens
        for token in self.tokens:
            b0 = token.raw_word & 0xFF
            b1 = (token.raw_word >> 8) & 0xFF
            if b0 == 0x0F and (b1 & 0x0F) == 0x05 and token.command_bytes and len(token.command_bytes) >= 6:
                token.command_bytes = token.command_bytes[0:4] + struct.pack('<H', total_glyphs)
        self.glyph_budget = total_glyphs
        return len(self.tokens)


@dataclass
class GlyphDraw:
    '''
    One rendered glyph cell

    May need to be tweeked for texture/jpn glyph support.
    '''
    char:  str
    x:     float
    y:     float
    w:     float
    h:     float
    color: tuple[int, int, int, int]

@dataclass
class RmfFile:
    version:          int
    glyph_data_size:  int
    packet_data_size: int
    glyph_data_end:   int | None  # English only
    packets: list[RmfPacket | None] = field(default_factory=list) # None are sentinel entries
    tail_data:        bytes = b'' # Preserves tail font/glyph data
    is_english:       bool = True

    @classmethod
    def from_bytes(cls, data: bytes, is_english: bool = True) -> RmfFile:
        magic = data[0:4]
        if magic != MAGIC:
            raise ValueError(f'Invalid magic. Got {magic}, expected {MAGIC}')
        version, glyph_size, packet_data_size, packet_count = struct.unpack('<IIII', data[4:20])
        offset_table_start = 0x18 if is_english else 0x14
        glyph_data_end = None
        if is_english:
            glyph_data_end = struct.unpack('<I', data[20:24])[0]
        offsets = []
        table_offset = offset_table_start
        for _ in range(packet_count):
            packet_offset = struct.unpack('<I', data[table_offset:table_offset+4])[0]
            offsets.append(packet_offset)
            table_offset += 4
        packets: list[RmfPacket | None] = []
        for i, pkt_offset in enumerate(offsets):
            if pkt_offset == 0xFFFFFFFF:
                packets.append(None)
                continue
            next_offset = packet_data_size
            for next_pkt in offsets[i+1:]:
                if next_pkt != 0xFFFFFFFF:
                    next_offset = next_pkt
                    break
            packet_data = data[pkt_offset : next_offset]
            packets.append(RmfPacket.from_bytes(packet_data))
        appended_tail = data[packet_data_size:] if len(data) > packet_data_size else b''
        return cls(
            version,
            glyph_size,
            packet_data_size,
            glyph_data_end,
            packets,
            tail_data=appended_tail,
            is_english=is_english
        )

    def to_bytes(self, is_english: bool = True) -> bytes:
        '''Re-encode the RmfFile into a complete binary stream'''
        offset_table_start = 0x18 if is_english else 0x14
        header_and_table_size = offset_table_start + (len(self.packets) * 4)
        packet_bytes_list = []
        offsets = []
        current_offset = header_and_table_size
        for packet in self.packets:
            if packet is None:
                offsets.append(0xFFFFFFFF)
                continue
            packet_data = packet.to_bytes()
            packet_bytes_list.append(packet_data)
            offsets.append(current_offset)
            current_offset += len(packet_data)
        calculated_packet_size = current_offset
        header = bytearray()
        header.extend(MAGIC)
        header.extend(struct.pack('<IIII', self.version, self.glyph_data_size, calculated_packet_size, len(self.packets)))
        if is_english:
            glyph_end = calculated_packet_size + self.glyph_data_size
            header.extend(struct.pack('<I', glyph_end))
        for off in offsets:
            header.extend(struct.pack('<I', off))
        full_data = bytearray(header)
        for packet_data in packet_bytes_list:
            full_data.extend(packet_data)
        if self.tail_data is not None:
            full_data.extend(self.tail_data)
        return bytes(full_data)

    def get_embedded_glyph_widths(self) -> list[int]:
        '''Check for embedded glyph width data in the tail.'''
        if self.glyph_data_size <= 0 or not self.tail_data:
            return []
        tile_count = self.glyph_data_size // 512
        if tile_count <= 0:
            return []
        table_bytes = self.tail_data[self.glyph_data_size : self.glyph_data_size + tile_count]
        if len(table_bytes) < tile_count:
            return []
        return list(table_bytes)

    def build_render_timeline(
        self,
        default_frames_per_glyph: int = 3,       # Default engine typewrite speed
        max_select_frames:        int = 300,     # Maximum wait for select commands, artifical due to current non-interactability
        max_wait_frames:          int = 160,     # Maximum waittime, artifical due to current non-interactability
    ) -> tuple[list[list[GlyphDraw]], list[int]]:
        '''Build a frame-by-frame render timeline for the RMF file
        Return (timeline_states, packet_start_frames)
        Owns all command parsing, in the future will dispatch to separate command functions.
        '''
        timeline_states:     list[list[GlyphDraw]] = []
        packet_start_frames: list[int] = []

        # Default virtual canvas starting position (top-left or centered depending on engine defaults)
        default_start_x = 32.0
        default_start_y = 32.0
        pen_x = line_origin_x = default_start_x
        pen_y = line_origin_y = default_start_y
        current_color: tuple[int, int, int, int] = (220, 220, 220, 255)

        # Region specific cell size and advance values
        if self.is_english:
            width_word, height_word = 0x0B33, 0x1666
            cell_h = height_word / 256.0
            space_advance  = 8.0            # (width_word * 12) >> 12
            line_height    = 44.0           # (height_word * 32) >> 12
        else:
            width_word, height_word = 0x0E66, 0x1000
            cell_h = height_word / 256.0
            space_advance  = 28.0           # full cell
            line_height    = 32.0           # (height_word * 32) >> 12

        embedded_widths = self.get_embedded_glyph_widths()
        current_glyphs: list[GlyphDraw] = []
        frames_per_glyph = default_frames_per_glyph
        frames_spent_typing = 0
        for packet in self.packets:
            packet_start_frames.append(len(timeline_states))
            if packet is None:
                continue # doesn't contribute a frame, stays for alignment
            packet_text_pending_render = False
            for token in packet.tokens:
                b0 = token.raw_word & 0xFF
                b1 = (token.raw_word >> 8) & 0xFF
                payload = token.command_bytes[2:] if token.command_bytes else b''
                if token.is_glyph:
                    char = decode_glyph(token.raw_word, self.is_english)
                    is_embedded = bool(b1 & 0x80)
                    if is_embedded:
                        tile_index = b0 - 0x10
                        spacing = (
                            embedded_widths[tile_index]
                            if 0 <= tile_index < len(embedded_widths)
                            else 0x20
                        )
                    else:
                        spacing = 0x20
                    advance = (width_word * spacing) >> 12
                    current_glyphs.append(GlyphDraw(char, pen_x, pen_y, advance, cell_h, current_color))
                    pen_x += advance
                    for _ in range(frames_per_glyph):
                        timeline_states.append(list(current_glyphs))
                    frames_spent_typing += frames_per_glyph
                    packet_text_pending_render = True
                else:
                    subcmd = b1 & 0x0F
                    if b0 == 0x01: # Space
                        pen_x += space_advance
                        packet_text_pending_render = True
                    elif b0 == 0x0A: # Newline
                        pen_x = line_origin_x
                        pen_y += line_height
                        packet_text_pending_render = True
                    elif b0 == 0x02 and subcmd == 0: # Set color
                        if len(payload) >= 4:
                            r, g, b, a = payload[:4]
                            current_color = (r, g, b, min(255, a * 2))
                    elif b0 == 0x04: # Position
                        if len(payload) >= 4:
                            x = int.from_bytes(payload[0:2], 'little', signed=True)
                            y = int.from_bytes(payload[2:4], 'little', signed=True)
                            if subcmd == 0: # Absolute
                                pen_x = line_origin_x = float(x)
                                pen_y = line_origin_y = float(y)
                            elif subcmd == 1: # Relative
                                pen_x += x
                                pen_y += y
                                line_origin_x += x
                                line_origin_y += y
                    elif b0 == 0x03: # Dimensions
                        if len(payload) >= 4:
                            raw_w = int.from_bytes(payload[0:2], 'little')
                            raw_h = int.from_bytes(payload[2:4], 'little')
                            if raw_w > 0:
                                width_word = raw_w
                                narrow_advance = (raw_w * 12) >> 12    # *english* narrow advance is based on char size
                                full_advance = (raw_w * 32) >> 12
                                space_advance = float(narrow_advance if self.is_english else full_advance)
                            if raw_h > 0:
                                cell_h = raw_h / 256.0
                                height_word = raw_h
                                line_height = float((raw_h * 32) >> 12)
                    elif b0 == 0x07: # Text Speed
                        if len(payload) >= 2:
                            raw_speed = int.from_bytes(payload[0:2], 'little')
                            frames_per_glyph = max(1, round(raw_speed / 256.0))
                    elif b0 == 0x0F and subcmd == 0x05: # Sentence wrapper
                        if len(payload) >= 2:
                            flags = int.from_bytes(payload[0:2], 'little')
                            if flags & 0x03:
                                logger.warning(
                                    'Sentence flags & 3 set, render is using default. This doesn\'t match '
                                    'the engine which would recalculate the x position.'
                                )
                        current_glyphs = []
                        frames_spent_typing = 0
                        current_color = (220, 220, 220, 255)
                        pen_x = line_origin_x = default_start_x
                        pen_y = line_origin_y = default_start_y
                        packet_text_pending_render = True
                    elif b0 == 0x0F and subcmd == 0x00: # WaitTime
                        if len(payload) >= 2:
                            total_wait = int.from_bytes(payload[:2], 'little')
                            if total_wait != 0xFFFF:
                                capped_total = min(total_wait, max_wait_frames)
                                hold_frames = max(0, capped_total - frames_spent_typing)
                                for _ in range(hold_frames):
                                    timeline_states.append(list(current_glyphs))
                                frames_spent_typing = 0
                            packet_text_pending_render = False
                    elif b0 == 0x0F and subcmd in (0x02, 0x04): # WaitSelect/WaitSelect2
                        for _ in range(select_pause_frames):
                            timeline_states.append(list(current_glyphs))
                        packet_text_pending_render = False
            if packet_text_pending_render or len(timeline_states) == packet_start_frames[-1]:
                for _ in range(max_wait_frames):
                    timeline_states.append(list(current_glyphs))
        if not timeline_states:
            timeline_states = [[]]
        return timeline_states, packet_start_frames

###---------------------------------------------------- Handler -----------------------------------------------------###

@Registry.register(
    'RMF Message Data Handler',
    extensions=('.rmf',),
    supported_actions=(
        ActionDef('Properties', ActionType.DIALOG),
))
class RmfHandler(LeafHandler):
    '''Leaf handler for RMF message data.'''
    def __init__(self, source: bytes, parent: VfsNode | None = None, is_english: bool = True) -> None:
        super().__init__(source, parent)
        self._raw        = source
        self._is_english = is_english
        if source:
            self._rmf_file = RmfFile.from_bytes(source, is_english=is_english)

    def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> RmfFile:
        if raw_bytes:
            self._raw = raw_bytes
            self._rmf_file = RmfFile.from_bytes(raw_bytes, is_english=self._is_english)
        return self._rmf_file

    def decode_editor_data(self, node: VfsNode, payload: Any, **kwargs) -> bytes:
        if not isinstance(payload, RmfFile):
            raise TypeError(f'Expected RmfFile, got {type(payload)}')
        return payload.to_bytes(is_english=self._is_english)

    def execute_action(self, node: VfsNode, action_name: str, **kwargs):
        if action_name == 'Properties':
            return self.properties()
        return None

    def properties(self) -> str:
        if not self._rmf_file:
            return 'error: Invalid RMF file'
        return f'version: {self._rmf_file.version}\n' \
               f'glyph size: {self._rmf_file.glyph_data_size}\n' \
               f'packet data size: {self._rmf_file.packet_data_size}\n' \
               f'glyph data end: {self._rmf_file.glyph_data_end}'
