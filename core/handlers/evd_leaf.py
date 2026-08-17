'''Handler for EVD script files.'''
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from core.registry import Registry
from core.node import VfsNode
from core.contracts import LeafHandler
from core.workers import ActionDef, ActionType
from utilities import get_resource_path

import logging
logger = logging.getLogger(f'radiata.{__name__}')


COMMAND_REGION_OFFSET = 0x0C
EVD_KEEP_GOING_FLAG  = 0x80
EVD_OPCODE_EXCEPTION_THRESHOLD = 0xF0

END_SCRIPT_OPCODE         = 0x00
JUMP_OPCODE               = 0x02
MARKER_SEEK_OPCODE        = 0x0D
CALC_OPCODE               = 0x14
SCRIPT_START_STACK_OPCODE = 0x01
SCRIPT_START_OPCODE       = 0x04

CATEGORY_TERMINAL         = 'end'
CATEGORY_JUMP             = 'jump'
CATEGORY_SCRIPT_START     = 'script_start'
CATEGORY_MARKER_SEEK      = 'marker_seek'
CATEGORY_EXPRESSION       = 'expression'
CATEGORY_HIGH             = 'high'
CATEGORY_NORMAL           = 'normal'

class EvdError(RuntimeError):
    pass

###------------------------------------------------- Opcodes ---------------------------------------------------###

@dataclass(frozen=True)
class OpcodeInfo:
    '''Metadata for a single opcode.'''
    opcode:      int
    name:        str
    debug_addr:  str | None
    summary:     str
    category:    str = CATEGORY_NORMAL
    confirmed_argument_layout: bool = False

class OpcodeTable:
    '''
    Owns the opcode information for the EVD script.
    Includes opcode name/category lookup by opcode or name.
    Includes special cases for opcode handling.
    '''
    def __init__(self) -> None:
        self._by_opcode: dict[int, OpcodeInfo] = self._load_opcode_table()
        self._by_name: dict[str, int] = {info.name: opcode for opcode, info in self._by_opcode.items()}
        if not self._by_opcode:
            logger.warning('EVD opcode table is empty. name/categories will fall back to defaults.')

    @staticmethod
    def _load_opcode_table() -> dict[int, OpcodeInfo]:
        path = get_resource_path('ui/assets/evd_commands.json')
        try:
            with open(path, encoding='utf-8') as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f'Counld not load EVD opcode table from {path}: {e}')
            return {}

        table: dict[int, OpcodeInfo] = {}
        for entry in raw:
            try:
                opcode = int(entry['opcode'], 16)
            except (KeyError, ValueError, TypeError):
                logger.warning(f'Skipping malformed opcode table entry: {entry!r}')
                continue
            table[opcode] = OpcodeInfo(
                opcode=opcode,
                name=entry.get('name') or f'UNK_{opcode:02X}',
                debug_addr=entry.get('debug'),
                summary=entry.get('summaryHtml', ''),
                category=entry.get('category') or CATEGORY_NORMAL,
                confirmed_argument_layout=bool(entry.get('confirme_argument_layout', False)),
            )
        return table

    def __bool__(self) -> bool:
        return bool(self._by_opcode)

    def get(self, opcode: int) -> OpcodeInfo | None:
        return self._by_opcode.get(opcode)

    def name(self, opcode: int) -> str:
        '''Look up an opcode's name by numeric value.'''
        info = self.get(opcode)
        return info.name if info else f'Unknown {opcode:02X}'

    def by_name(self, name: str) -> int | None:
        '''Look up an opcode's numeric value by name.'''
        return self._by_name.get(name)

    def category(self, opcode: int) -> str:
        '''Look up an opcode's category by numeric value.'''
        if opcode >= EVD_OPCODE_EXCEPTION_THRESHOLD:
            return CATEGORY_HIGH
        info = self.get(opcode)
        return info.category if info else CATEGORY_NORMAL

    def confirmed_argument_layout(self, opcode: int) -> bool:
        '''Look up whether an opcode has a confirmed argument layout.'''
        info = self.get(opcode)
        return bool(info and info.confirmed_argument_layout)

    def all_opcodes(self) -> list[OpcodeInfo]:
        '''Return all opcodes, sorted by opcode value (sequential table order).'''
        return sorted(self._by_opcode.values(), key=lambda info: info.opcode)

OPCODES = OpcodeTable()  # module-level singleton

###------------------------------------------------- structs ---------------------------------------------------###

@dataclass(frozen=True)
class EvdInstruction:
    '''
    Struct representing a single EVD instruction.

    extra_words                      - holds the raw byte, even for instructions with no extra words
    length/effective_extra_words     - holds the extra words needed for parsing
    '''
    byte_offset: int
    opcode:      int
    extra_words: int
    arg:         int
    flags:       int
    payload:     bytes

    @property
    def word_offset(self) -> int:
        return self.byte_offset // 4

    @property
    def name(self) -> str:
        return OPCODES.name(self.opcode)

    @property
    def effective_extra_words(self) -> int:
        return 0 if self.opcode >= EVD_OPCODE_EXCEPTION_THRESHOLD else self.extra_words

    @property
    def length(self) -> int:
        return 4 + self.effective_extra_words * 4

    @property
    def keep_going(self) -> bool:
        '''
        bit 31 or bit 7 of flags. 1 = keep going, 0 = 1 frame yield
        Certain instructions force keep_going to be False (require immediate execution)
        '''
        return bool(self.flags & EVD_KEEP_GOING_FLAG)

    def __str__(self) -> str:
        kg_marker = '->' if self.keep_going else '||'
        payload_hex = self.payload.hex(' ').upper()
        return (
            f'[{self.word_offset:06X}] {kg_marker} '
            f'{self.name} (0x{self.opcode:02X}) | Arg: {self.arg:02X} | Payload: {payload_hex} | '
            f'Words: {self.extra_words:02X} | Flags: {self.flags:02X}'
        )

@dataclass(frozen=True)
class EvdInfo:
    '''Struct representing the parsed EVD script information.'''
    total_size:           int
    instruction_count:    int
    unreached_byte_count: int = 0

@dataclass(frozen=True)
class EvdMarkerTable:
    '''Struct representing a parsed EVD marker table.'''
    word_offset: int                # from the header
    byte_offset: int                # word_offset * 4
    entries:     tuple[int, ...]    # entries in sequential order added as their offset

@dataclass(frozen=True)
class ConditionInfo:
    condition_id:  int
    cond_base:     int
    invert:        bool
    unconditional: bool
    payload:       tuple[int, int] | None  # (word0, word1) of condition payload

@dataclass(frozen=True)
class ScriptStartFields:
    opcode:                 int
    raw_arg:                int
    mode:                   int | None
    has_explicit_character: bool | None
    script_id:              int
    condition:              ConditionInfo
    character_selector:     int | None

@dataclass(frozen=True)
class InstructionDescription:
    summary:     str
    details:     tuple[str, ...]
    jump_target: int | None
    category:    str

@dataclass(frozen=True)
class ReachabilityResult:
    instructions:          dict[int, EvdInstruction]
    unreached_ranges:      tuple[tuple[int, int], ...]
    desync_offsets:        tuple[int, ...]
    referenced_script_ids: frozenset[int]

class EvdEditorPayload(NamedTuple):
    '''Return payload for the EVD editor.'''
    info:                  EvdInfo
    instructions:          list[EvdInstruction]
    descriptions:          list[InstructionDescription]
    mutable:               list[MutableInstruction]
    header:                EvdHeader
    marker_table:          EvdMarkerTable | None
    unreached_ranges:      tuple[tuple[int, int], ...] = ()
    referenced_script_ids: frozenset[int] = frozenset()
    reached_offsets:       frozenset[int] = frozenset()

class EvdSavePayload(NamedTuple):
    '''What EvdEditor.current_data() will return and EvdHandler.decode_editor_data receives.'''
    header:       EvdHeader
    instructions: list[MutableInstruction]

@dataclass(slots=True, frozen=True)
class EvdHeader:
    '''Struct representing the parsed EVD header data.'''
    magic:                    bytes
    flags:                    int
    marker_table_word_offset: int  # 0 for no marker table
    raw_header_bytes:         bytes

    @property
    def marker_table_byte_offset(self) -> int | None:
        return self.marker_table_word_offset * 4 if self.marker_table_word_offset else None

@dataclass(frozen=True)
class SymbolicJump:
    '''
    A JUMP instruction's encoded form.
    Stores the target symbolically.
    Stores the condition bytes verbatim.
    '''
    target_id:         int | None
    raw_target_offset: int | None
    condition_byte:    int
    condition_payload: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.target_id is None and self.raw_target_offset is None:
            raise ValueError('SymbolicJump needs either target_id or raw_target_offset')
        if self.condition_byte != 0 and self.condition_payload is None:
            raise ValueError('conditional jump requires condition_payload')

@dataclass(frozen=True)
class MutableInstruction:
    id:            int
    opcode:        int
    arg:           int
    flags:         int
    extra_words:   int = 0
    payload_words: tuple[int, ...] = ()
    jump:          SymbolicJump | None = None

    def __post_init__(self) -> None:
        if self.opcode == JUMP_OPCODE and self.jump is None:
            raise ValueError('JUMP instruction requires a SymbolicJump')
        if self.opcode != JUMP_OPCODE and self.jump is not None:
            raise ValueError('only JUMP instruction may carry a SymbolicJump')
        if self.opcode < EVD_OPCODE_EXCEPTION_THRESHOLD and self.opcode != JUMP_OPCODE:
            if len(self.payload_words) != self.extra_words:
                raise ValueError(
                    f'opcode 0x{self.opcode:02X}: extra_words={self.extra_words} but '
                    f'{len(self.payload_words)} payload word(s) were given'
                )

    def length(self) -> int:
        if self.opcode == JUMP_OPCODE:
            return 16 if self.jump.condition_byte != 0 else 8 # type:ignore type is enforced post_init
        if self.opcode >= EVD_OPCODE_EXCEPTION_THRESHOLD:
            return 4
        return 4 + len(self.payload_words) * 4

###----------------------------------------------- Script -----------------------------------------------###

class EvdScript:
    _MARKER_SEEK_SELECTOR_LABELS = {2: 'from event value'}

    def __init__(self, data: bytes) -> None:
        self.raw = data
        self.header = self._parse_header(data)
        self.marker_table = self._parse_marker_table() if self.header else None
        self._reachability_cache: ReachabilityResult | None = None

    ###---------------------------------- Header / Marker Table ----------------------------------###

    def _parse_header(self, data: bytes) -> EvdHeader | None:
        '''Parses the EVD header from the given data.'''
        if len(data) < COMMAND_REGION_OFFSET:
            return None
        magic, flags, marker_table_word_offset = struct.unpack_from('<4sII', data, 0)
        if magic not in (b'EVD\x01', b'EVD\x00'):
            raise ValueError(f'Invalid EVD magic: {magic!r}')
        return EvdHeader(magic, flags, marker_table_word_offset, data[:COMMAND_REGION_OFFSET])

    def _parse_marker_table(self) -> EvdMarkerTable | None:
        '''Return None if the marker table is not present or doens't fit in the buffer.'''
        if not self.header or not self.header.marker_table_word_offset:
            return None
        byte_offset = self.header.marker_table_byte_offset
        if byte_offset is None or byte_offset + 4 > len(self.raw):
            logger.warning(f'marker table offset {byte_offset} is out of bounds.')
            return None

        count = int.from_bytes(self.raw[byte_offset:byte_offset + 2], 'little')
        entries: list[int] = []
        pos = byte_offset + 4
        for i in range(count):
            if pos + 4 > len(self.raw):
                logger.warning(f'marker table entry {i}/{count} out of bounds, truncating.')
                break
            entries.append(int.from_bytes(self.raw[pos:pos + 4], 'little') * 4)
            pos += 4
        return EvdMarkerTable(
            word_offset=self.header.marker_table_word_offset,
            byte_offset=byte_offset,
            entries=tuple(entries)
        )

    def properties(self) -> str:
        if not self.header:
            return 'error: Invalid EVD header'
        props =  f'header flags: {self.header.flags}\n'
        props += f'marker table word offset: {self.header.marker_table_word_offset}\n'
        if self.marker_table:
            props += f'marker table entries: {len(self.marker_table.entries)}'
        return props

    ###----------------------------------- Decoding -----------------------------------###

    def decode_condition_id(self, condition_id: int, payload_offset:int) -> ConditionInfo:
        cond_base = condition_id & 0x7F
        invert = bool(condition_id & 0x80)
        unconditional = condition_id == 0
        payload = None
        if not unconditional and payload_offset + 8 <= len(self.raw):
            w0 = int.from_bytes(self.raw[payload_offset : payload_offset + 4], 'little')
            w1 = int.from_bytes(self.raw[payload_offset + 4 : payload_offset + 8], 'little')
            payload = (w0, w1)
        return ConditionInfo(condition_id, cond_base, invert, unconditional, payload)

    def decode_script_start_operands(self, offset: int, opcode: int) -> ScriptStartFields | None:
        '''Decode operands for the SCRIPT_START(0x04) and SCRIPT_STACK_START(0x01) instructions'''
        if OPCODES.category(opcode) != CATEGORY_SCRIPT_START or offset + 8 > len(self.raw):
            return None

        header_val = int.from_bytes(self.raw[offset : offset + 4], 'little')
        arg = (header_val >> 16) & 0xFF
        operand0 = int.from_bytes(self.raw[offset + 4 : offset + 8], 'little')
        script_id = operand0 & 0xFFFF
        condition_id = (operand0 >> 24) & 0xFF
        condition = self.decode_condition_id(condition_id, offset + 8)
        cursor = offset + 8
        if not condition.unconditional:
            cursor += 8

        mode = None
        has_explicit_character = None
        character_selector = None
        if OPCODES.confirmed_argument_layout(opcode):
            mode = arg & 0x0F
            has_explicit_character = bool(arg & 0x10)
            if has_explicit_character and cursor + 4 <= len(self.raw):
                character_selector = int.from_bytes(self.raw[cursor : cursor + 4], 'little')
        return ScriptStartFields(
            opcode=opcode,
            raw_arg=arg,
            mode=mode,
            has_explicit_character=has_explicit_character,
            script_id=script_id,
            condition=condition,
            character_selector=character_selector,
        )

    def decode_linear(self, start: int = COMMAND_REGION_OFFSET, end: int | None = None) -> list[EvdInstruction]:
        '''Flat linear sequential walk of the command region.'''
        instructions: list[EvdInstruction] = []
        file_size = len(self.raw) if end is None else min(end, len(self.raw))
        offset = start
        while offset < file_size:
            if offset + 4 > file_size:
                logger.warning(f'Misaligned EVD file, truncating.')
                break
            header_val   = int.from_bytes(self.raw[offset : offset + 4], 'little')
            opcode       = header_val & 0xFF
            extra_words  = (header_val >> 8) & 0xFF
            args         = (header_val >> 16) & 0xFF
            flags        = (header_val >> 24) & 0xFF
            effective_extra_words = 0 if opcode >= EVD_OPCODE_EXCEPTION_THRESHOLD else extra_words
            payload_size = effective_extra_words * 4
            if offset + 4 + payload_size > file_size:
                logger.warning(f'Incomplete EVD instruction at {offset}, truncating.')
                break
            payload = self.raw[offset + 4 : offset + 4 + payload_size]
            instructions.append(EvdInstruction(offset, opcode, extra_words, args, flags, payload))
            offset += 4 + payload_size
        return instructions

    ###--------------------------------- Human-Readability ---------------------------------###

    @staticmethod
    def _format_condition(condition: ConditionInfo) -> str:
        if condition.unconditional:
            return 'unconditional'
        inv = ' (inverted)' if condition.invert else ''
        text = f'if cond 0x{condition.cond_base:02X}{inv}'
        if condition.payload:
            text += f'   [payload {condition.payload[0]:08X} {condition.payload[1]:08X}]'
        return text

    def describe(self, inst: EvdInstruction) -> InstructionDescription:
        opcode = inst.opcode
        offset = inst.byte_offset
        category = OPCODES.category(opcode)
        if category == CATEGORY_TERMINAL:
            return InstructionDescription(
                summary='End script',
                details=('releases the script slot',),
                jump_target=None,
                category=CATEGORY_TERMINAL,
            )
        if category == CATEGORY_JUMP and offset + 8 <= len(self.raw):
            branch_word = int.from_bytes(self.raw[offset + 4 : offset + 8], 'little')
            condition_id = (branch_word >> 24) & 0xFF
            relative_words = branch_word & 0xFFFFFF
            if relative_words & 0x800000:  # sign extended bit 23
                relative_words -= 1 << 24
            conditional = condition_id != 0
            length = 16 if conditional else 8
            base = offset + length
            target = base + relative_words * 4
            condition = self.decode_condition_id(condition_id, offset + 8)
            return InstructionDescription(
                summary=f'Jump to {target:06X}',
                details=(self._format_condition(condition),),
                jump_target=target,
                category=CATEGORY_JUMP,
            )

        if category == CATEGORY_SCRIPT_START:
            fields = self.decode_script_start_operands(offset, opcode)
            if fields is not None:
                details = [self._format_condition(fields.condition)]
                if fields.mode is not None:
                    details.append(f'mode {fields.mode}')
                if fields.has_explicit_character:
                    details.append(f'character selector {fields.character_selector:08X}')
                return InstructionDescription(
                    summary=f'Start script #{fields.script_id}',
                    details=tuple(details),
                    jump_target=None,
                    category=CATEGORY_SCRIPT_START,
                )

        if category == CATEGORY_MARKER_SEEK:
            selector = inst.arg & 0x07
            label = self._MARKER_SEEK_SELECTOR_LABELS.get(selector, f'selector {selector} (unconfirmed)')
            details = [label, 'target resolved at runtime']
            if self.marker_table:
                details.append(f'{len(self.marker_table.entries)} candidate marker(s)')
            return InstructionDescription(
                summary='Marker seek',
                details=tuple(details),
                jump_target=None,
                category=CATEGORY_MARKER_SEEK,
            )

        if category == CATEGORY_EXPRESSION:
            return InstructionDescription(
                summary='Expression',
                details=(f'payload: {inst.payload.hex(" ").upper()}' if inst.payload else 'no payload',),
                jump_target=None,
                category=CATEGORY_EXPRESSION,
            )

        if category == CATEGORY_HIGH:
            return InstructionDescription(
                summary=f'{inst.name} (system)',
                details=(f'arg {inst.arg:02X}',),
                jump_target=None,
                category=CATEGORY_HIGH,
            )

        details = (f'payload: {inst.payload.hex(" ").upper()}',) if inst.payload else ()
        return InstructionDescription(
            summary=f'{inst.name}',
            details=details,
            jump_target=None,
            category=CATEGORY_NORMAL,
        )

    ###--------------------------------- Reachability ---------------------------------###

    def _jump_successors(self, offset: int, length: int) -> list[int]:
        branch_word    = int.from_bytes(self.raw[offset + 4 : offset + 8], 'little')
        condition_id   = (branch_word >> 24) & 0xFF
        relative_words = branch_word & 0xFFFFFF
        if relative_words & 0x800000:  # sign extended bit 23
            relative_words -= 1 << 24
        conditional = condition_id != 0
        base        = offset + (16 if conditional else 8)
        target      = base + relative_words * 4
        successors  = [target]
        if conditional:
            successors.append(offset + length)
        return successors

    def _marker_seek_successors(self, offset: int, length: int) -> list[int]:
        successors = [offset + length]
        if self.marker_table:
            successors.extend(self.marker_table.entries)
        return successors

    @property
    def reachability(self) -> ReachabilityResult:
        if self._reachability_cache is None:
            self._reachability_cache = self.decode_reachable()
        return self._reachability_cache

    def decode_reachable(self) -> ReachabilityResult:
        '''Walks the command region starting from COMMAND_REGION_OFFSET,
        following only genuine accessible instructions.'''
        file_size:             int = len(self.raw)
        instructions:          dict[int, EvdInstruction] = {}
        worklist:              list[int] = [COMMAND_REGION_OFFSET]
        visited:               set[int]  = set()
        desync_offsets:        list[int] = []
        referenced_script_ids: set[int]  = set()

        while worklist:
            offset = worklist.pop()
            if offset in visited:
                continue
            if offset < 0 or offset + 4 > file_size:
                desync_offsets.append(offset)
                continue
            visited.add(offset)

            header_val   = int.from_bytes(self.raw[offset: offset + 4], 'little')
            opcode       = header_val & 0xFF
            extra_words  = (header_val >> 8) & 0xFF
            arg          = (header_val >> 16) & 0xFF
            flags        = (header_val >> 24) & 0xFF
            effective_extra_words = 0 if opcode >= EVD_OPCODE_EXCEPTION_THRESHOLD else extra_words
            category     = OPCODES.category(opcode)

            if category == CATEGORY_JUMP and offset + 8 <= file_size:
                condition_id = self.raw[offset + 7]
                length = 16 if condition_id != 0 else 8
            else:
                length = 4 + effective_extra_words * 4

            if offset + length > file_size:
                logger.warning(
                    f'Intruction at {offset} (opcode 0x{opcode:02X}) claims to be {length} bytes '
                    f'but only {file_size - offset} bytes remain, truncating remainder.'
                )
                length = file_size - offset
                if length < 4:
                    desync_offsets.append(offset)
                    continue

            payload = self.raw[offset + 4 : offset + length]
            instructions[offset] = EvdInstruction(
                byte_offset=offset,
                opcode=opcode,
                extra_words=extra_words,
                arg=arg,
                flags=flags,
                payload=payload,
            )
            if category == CATEGORY_SCRIPT_START:
                fields = self.decode_script_start_operands(offset, opcode)
                if fields is not None:
                    referenced_script_ids.add(fields.script_id)
            if category == CATEGORY_TERMINAL:
                continue
            elif category == CATEGORY_JUMP:
                worklist.extend(self._jump_successors(offset, length))
            elif category == CATEGORY_MARKER_SEEK:
                worklist.extend(self._marker_seek_successors(offset, length))
            else:
                worklist.append(offset + length)

        covered = sorted(instructions.items())
        unreached_ranges: list[tuple[int, int]] = []
        cursor = COMMAND_REGION_OFFSET
        for off, inst in covered:
            end = off + 4 + len(inst.payload)
            if off > cursor:
                unreached_ranges.append((cursor, off))
            cursor = max(cursor, end)
        if cursor < file_size:
            unreached_ranges.append((cursor, file_size))

        return ReachabilityResult(
            instructions=instructions,
            unreached_ranges=tuple(unreached_ranges),
            desync_offsets=tuple(desync_offsets),
            referenced_script_ids=frozenset(referenced_script_ids),
        )

    ###-------------------------------------- Editor Payload ------------------------------------###

    def editor_payload(self) -> EvdEditorPayload:
        '''
        Returns the parsed EVD script via editor payload.
        The payload returns BOTH instructions from a reachability walk and linear walk.
        Further reasearch will need to be done to determine the precise control flow.
        '''
        if not self.raw or not self.header:
            raise EvdError('EVD buffer is empty or missing valid header.')
        result = self.reachability
        if result.desync_offsets:
            logger.warning(
                f'{len(result.desync_offsets)} offsets pointed out of bounds: '
                f'{[hex(o) for o in result.desync_offsets[:10]]}'
                + (' ...' if len(result.desync_offsets) > 10 else '')
            )
        all_instructions: dict[int, EvdInstruction] = dict(result.instructions)
        for gap_start, gap_end in result.unreached_ranges:
            for inst in self.decode_linear(start=gap_start, end=gap_end):
                all_instructions.setdefault(inst.byte_offset, inst)
        instructions = [inst for _, inst in sorted(all_instructions.items())]
        descriptions = [self.describe(inst) for inst in instructions]
        mutable      = self.to_mutable(instructions)
        unreached_byte_count = sum(end - start for start, end in result.unreached_ranges)

        return EvdEditorPayload(
            info=EvdInfo(
                total_size=len(self.raw),
                instruction_count=len(instructions),
                unreached_byte_count=unreached_byte_count,
            ),
            instructions=instructions,
            descriptions=descriptions,
            mutable=mutable,
            header=self.header,
            marker_table=self.marker_table,
            unreached_ranges=result.unreached_ranges,
            referenced_script_ids=result.referenced_script_ids,
            reached_offsets=frozenset(result.instructions.keys()),
        )

    ###-------------------------------------- Encoding ------------------------------------###

    def to_mutable(self, instructions: list[EvdInstruction]) -> list[MutableInstruction]:
        '''Converts a decoded instruction list into a Mutable Symbolic form.'''
        known_offsets = {inst.byte_offset for inst in instructions}
        out: list[MutableInstruction] = []
        for inst in instructions:
            offset = inst.byte_offset
            if OPCODES.category(inst.opcode) == CATEGORY_JUMP and offset + 8 <= len(self.raw):
                branch_word = int.from_bytes(self.raw[offset + 4 : offset + 8], 'little')
                condition_byte = (branch_word >> 24) & 0xFF
                relative_words = branch_word & 0xFFFFFF
                if relative_words & 0x800000:
                    relative_words -= 1 << 24
                length = 16 if condition_byte != 0 else 8
                target_offset = (offset + length) + relative_words * 4
                condition_payload = None
                if condition_byte != 0 and offset + 16 <= len(self.raw):
                    condition_payload = (
                        int.from_bytes(self.raw[offset + 8 : offset + 12], 'little'),
                        int.from_bytes(self.raw[offset + 12 : offset + 16], 'little')
                    )
                target_id = target_offset if target_offset in known_offsets else None
                jump = SymbolicJump(
                    target_id=target_id,
                    raw_target_offset=None if target_id is not None else target_offset,
                    condition_byte=condition_byte,
                    condition_payload=condition_payload
                )
                out.append(MutableInstruction(
                    id=offset, opcode=inst.opcode, arg=inst.arg, flags=inst.flags, jump=jump
                ))
            else:
                words = tuple(
                    int.from_bytes(inst.payload[i : i + 4], 'little')
                    for i in range(0, len(inst.payload), 4)
                )
                out.append(MutableInstruction(
                    id=offset, opcode=inst.opcode, arg=inst.arg, flags=inst.flags,
                    extra_words=inst.extra_words, payload_words=words
                ))
        return out

    def encode(self, instructions: list[MutableInstruction]) -> bytes:
        '''Resolve the instuction offsets, then resolve the jump targets
        and encode the instructions.'''
        if not self.header:
            raise EvdError('Cannot encode without a valid header')
        # Pass 1: layout
        offsets: dict[int, int] = {}
        cursor = COMMAND_REGION_OFFSET
        for inst in instructions:
            offsets[inst.id] = cursor
            cursor += inst.length()
        total_size = cursor
        # Pass 2: encode
        out = bytearray(total_size)
        out[:COMMAND_REGION_OFFSET] = self.header.raw_header_bytes
        for inst in instructions:
            offset = offsets[inst.id]
            if inst.opcode == JUMP_OPCODE:
                jump = inst.jump
                if not jump:
                    raise EvdError(f'JUMP (id={inst.id}) has no target')
                length = inst.length()
                if jump.target_id is not None:
                    if jump.target_id not in offsets:
                        raise ValueError(f'JUMP (id={inst.id}) targets unknown instruction id {jump.target_id}')
                    target_offset = offsets[jump.target_id]
                else:
                    target_offset = jump.raw_target_offset if jump.raw_target_offset else 0
                base = offset + length
                delta = target_offset - base
                if delta % 4 != 0:
                    raise ValueError(f'JUMP (id={inst.id}) delta {delta} is not a multiple of 4')
                word_offset = delta // 4
                if not (-(1 << 23) <= word_offset < (1 << 23)):
                    raise ValueError(f'JUMP (id={inst.id}): target too far away to encode (word_offset={word_offset})')
                header_word = JUMP_OPCODE | (0x03 if jump.condition_byte else 0x01) << 8 | (inst.arg << 16) | (inst.flags << 24)
                payload_word = (word_offset & 0xFFFFFF) | (jump.condition_byte << 24)
                out[offset : offset + 4] = header_word.to_bytes(4, 'little')
                out[offset + 4 : offset + 8] = payload_word.to_bytes(4, 'little')
                if jump.condition_byte is not None and jump.condition_payload is not None:
                    out[offset + 8 : offset + 12] = jump.condition_payload[0].to_bytes(4, 'little')
                    out[offset + 12 : offset + 16] = jump.condition_payload[1].to_bytes(4, 'little')
            else:
                header_word = inst.opcode | (inst.extra_words << 8) | (inst.arg << 16) | (inst.flags << 24)
                out[offset : offset + 4] = header_word.to_bytes(4, 'little')
                cursor = offset + 4
                for word in inst.payload_words:
                    out[cursor : cursor + 4] = word.to_bytes(4, 'little')
                    cursor += 4
        return bytes(out)

    @classmethod
    def encode_with_header(cls, header: EvdHeader, instructions: list[MutableInstruction]) -> bytes:
        '''We keep the original header for the rebuild'''
        return cls(header.raw_header_bytes).encode(instructions)

###---------------------------------------------------- Handler -----------------------------------------------------###

@Registry.register(
    'EVD Script Handler',
    extensions=('.evd',),
    supported_actions=(
        ActionDef(name='Skip cutscenes', action_type=ActionType.PATCH),
        ActionDef('Properties', ActionType.DIALOG)
))
class EVDHandler(LeafHandler):
    '''Leaf handler for EVD script files.'''
    def __init__(self, source: bytes, parent: VfsNode | None = None) -> None:
        super().__init__(source)
        self._raw    = source
        self._script = EvdScript(source)

    def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> EvdEditorPayload:
        '''Return the editor payload of the parsed EVD file.'''
        return self._script.editor_payload()

    def decode_editor_data(self, node: VfsNode, payload: EvdSavePayload, **kwargs) -> bytes:
        if not isinstance(payload, EvdSavePayload):
            raise ValueError('Invalid payload: expected EvdSavePayload')
        return self._script.encode_with_header(payload.header, payload.instructions)

    def execute_action(self, node: VfsNode, action_name: str, **kwargs):
        if action_name == 'Skip cutscenes':
            return self.skip_cutscenes(node)
        elif action_name == 'Properties':
            return self._script.properties()
        return None

    def skip_cutscenes(self, node: VfsNode) -> None:
        raise NotImplementedError('Cutscene skipping is not yet implemented')
