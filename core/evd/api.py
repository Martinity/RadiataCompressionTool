'''
Project-facing API over the vendored `evd_tool`.

`evd_tool` is a 22k-line CLI script kept byte-identical to its upstream copy in
`rs_elf/tools`. Everything the handler and the editor need goes through here so
that re-syncing upstream never touches app code:

    text forms      decompile_code / compile_code / compile_code_to_source
    diagnostics     EvdCompileError, carrying the author-facing line number
    metadata        COMMANDS (per-command parameters and their roles), SYMBOLS
    line model      parse_code / CodeLine, a 1:1 view of the EVDCODE text

The line model is deliberately line-oriented rather than a syntax tree. One
EVDCODE line is one command, so a list of lines is both what the code view
edits and what the structure view renders, and neither can drift from the other.
'''
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Iterator

from core.evd import evd_tool
from utilities import get_resource_path

import logging
logger = logging.getLogger(f'radiata.{__name__}')

COMMAND_REGION_OFFSET = 0x0C
EVD_MAGIC = evd_tool.EVD_MAGIC

###------------------------------------------- Categories -------------------------------------------###

CATEGORY_TERMINAL     = 'end'
CATEGORY_JUMP         = 'jump'
CATEGORY_SCRIPT_START = 'script_start'
CATEGORY_MARKER_SEEK  = 'marker_seek'
CATEGORY_EXPRESSION   = 'calc'
CATEGORY_HIGH         = 'high'
CATEGORY_NORMAL       = 'normal'
CATEGORY_STRUCTURE    = 'structure'  # event/label/entry/header/braces: emits no command
CATEGORY_MACRO        = 'macro'      # authoring shorthand that lowers to several commands

# The opcodes that do something other than act on the scene, and so are worth
# telling apart by colour. Everything else is CATEGORY_NORMAL. These come from
# the format reference, sections 4 and 8; there are only six because control
# flow in this format is only six opcodes wide.
_OPCODE_CATEGORIES: dict[int, str] = {
    0x00: CATEGORY_TERMINAL,      # end script
    0x01: CATEGORY_SCRIPT_START,  # stacked start
    0x02: CATEGORY_JUMP,          # every branch and jump
    0x04: CATEGORY_SCRIPT_START,  # start script
    0x0D: CATEGORY_MARKER_SEEK,   # marker seek
    0x14: CATEGORY_EXPRESSION,    # read-modify-write across mutable state
}

# Opcode families from the format reference, section 9. Only used for grouping
# in the palette and for a second colour axis in the code view; the categories
# above stay the authority for anything that changes behaviour.
_OPCODE_FAMILIES: tuple[tuple[int, int, str], ...] = (
    (0x00, 0x1F, 'Script control, flags, values, windows'),
    (0x20, 0x3F, 'Characters'),
    (0x40, 0x5F, 'Background, map, camera'),
    (0x60, 0x7F, 'Primitives, textures, sound, movies'),
    (0x80, 0x8F, 'Dialogue, text, windows'),
    (0x90, 0xEF, 'Person, schedule, battle, effects'),
    (0xF0, 0xFF, 'Markers'),
)


def opcode_family(opcode: int | None) -> str:
    if opcode is None:
        return 'Structure'
    for low, high, name in _OPCODE_FAMILIES:
        if low <= opcode <= high:
            return name
    return 'Other'

###------------------------------------------- Diagnostics -------------------------------------------###

_LINE_PREFIX = re.compile(r'^line (\d+): (.*)$', re.S)
# "character_sub_anim character_number does not match character" -- the compiler
# names the derived field first and the input it disagrees with last.
_MISMATCH = re.compile(r'\b(\w+) does not match (\w+)\b')


class EvdCompileError(ValueError):
    '''A rejected EVDCODE source, with the author's line number when there is one.'''

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(f'line {line}: {message}' if line else message)
        self.message = message
        self.line = line

    @classmethod
    def from_exception(cls, exc: Exception) -> 'EvdCompileError':
        if isinstance(exc, cls):
            return exc
        match = _LINE_PREFIX.match(str(exc))
        if match:
            return cls(match.group(2), int(match.group(1)))
        return cls(str(exc))

    @property
    def conflicting_field(self) -> str | None:
        '''The derived parameter this error blames, when it blames one.

        A derived parameter is cross-checked against the inputs it comes from,
        so editing an input alone is rejected rather than ignored. The name here
        is what the parameter editor drops before retrying.
        '''
        match = _MISMATCH.search(self.message)
        return match.group(1) if match else None

###--------------------------------------------- Symbols ---------------------------------------------###

class SymbolTables:
    '''Id-to-name tables for characters, items, locations, BGM, skills, events and flags.

    Names are advisory: the decompiler appends them as `// field=name` comments
    and the compiler ignores them, so a missing table costs readability only.
    '''
    DOMAINS = ('character', 'item', 'location', 'bgm', 'skill', 'event', 'flag')

    def __init__(self) -> None:
        self._tables: dict[str, dict[int, str]] = {}
        path = get_resource_path('ui/assets/evd_symbols.json')
        try:
            self._tables = evd_tool.load_symbol_tables(path)
        except (OSError, ValueError) as e:
            logger.error(f'Could not load EVD symbol tables from {path}: {e}')

    def __bool__(self) -> bool:
        return bool(self._tables)

    @property
    def tables(self) -> dict[str, dict[int, str]] | None:
        '''The mapping `evd_tool` wants, or None when nothing loaded.'''
        return self._tables or None

    def lookup(self, domain: str, value: int) -> str | None:
        if not self._tables:
            return None
        return evd_tool.symbol_for(domain, value, self._tables)

    def search(self, domain: str, text: str) -> list[tuple[int, str]]:
        '''Ids in `domain` whose name contains `text`, case-insensitively.'''
        needle = text.strip().lower()
        if not needle:
            return []
        return sorted(
            (value, name)
            for value, name in self._tables.get(domain, {}).items()
            if needle in name.lower()
        )


SYMBOLS = SymbolTables()

###------------------------------------------ Command index ------------------------------------------###

@dataclass(frozen=True)
class ParamInfo:
    '''One named parameter of one command.

    `role` is the whole reason this metadata exists. An `input` is yours to set.
    A `derived` value is recomputed from the inputs it comes from and is
    cross-checked on compile, so changing one on its own is an error rather than
    a silent no-op -- the editor greys it and drops it when its input moves.
    '''
    name:    str
    role:    str  # 'input' | 'derived'
    meaning: str
    # How much the meaning is worth: 'traced' from this form's disassembly,
    # 'glossary' from the shared vocabulary, 'template' generated from the
    # parameter's own name, 'untraced' explicitly unknown, 'none' undescribed.
    # A structurally authoritative field is not necessarily an understood one.
    evidence: str = 'none'

    @property
    def is_input(self) -> bool:
        return self.role == 'input'

    @property
    def is_traced(self) -> bool:
        return self.evidence in ('traced', 'glossary')


@dataclass(frozen=True)
class CommandInfo:
    '''Everything needed to complete, document and validate one EVDCODE command.'''
    name:       str
    engine:     str
    summary:    str
    example:    str
    opcode:     int | None
    raw:        bool
    evidence:   str
    parameters: tuple[ParamInfo, ...]
    shorthand:  tuple[str, ...]              # every parameter the shorthand accepts
    positional: tuple[str, ...] = ()         # the ones it takes in order, without names

    @cached_property
    def by_name(self) -> dict[str, ParamInfo]:
        return {p.name: p for p in self.parameters}

    @property
    def inputs(self) -> tuple[ParamInfo, ...]:
        return tuple(p for p in self.parameters if p.is_input)

    @property
    def family(self) -> str:
        return opcode_family(self.opcode)

    def role_of(self, param: str) -> str | None:
        info = self.by_name.get(param)
        return info.role if info else None

    def evidence_of(self, param: str) -> str:
        info = self.by_name.get(param)
        return info.evidence if info else 'none'

    def meaning_of(self, param: str) -> str:
        '''What a parameter means, falling back to the shared vocabulary.

        A command's own list only holds the parameters the corpus classifier saw
        on it, so a legal-but-unused spelling (`not=` on `if_value`, where every
        shipped script writes `is=`) is missing from it while still being
        documented format-wide.
        '''
        info = self.by_name.get(param)
        if info is not None and info.meaning:
            return info.meaning
        shared = evd_tool.parameter_description(self.name, param)
        return shared or 'Not in the command index; passed through to the compiler as written.'


@dataclass(frozen=True)
class StructureInfo:
    '''A structural construct or authoring macro: `event`, `label`, `if_value`, `spawn_char`...'''
    name:      str
    summary:   str
    signature: str
    snippet:   str


class CommandIndex:
    '''The command index generated by `evd_tool extension-data`.

    One file feeds this: `evd_command_index.json`, carrying every command with
    its parameters and their input/derived roles, built from the same corpus
    classification the format reference is written from. Because it is
    generated, re-running that command is the whole update story.
    '''

    def __init__(self) -> None:
        index = self._load_json('ui/assets/evd_command_index.json', {})
        self._commands: dict[str, CommandInfo] = {}
        for name, entry in (index.get('commands') or {}).items():
            self._commands[name] = CommandInfo(
                name=name,
                engine=entry.get('engine', name),
                summary=entry.get('summary', ''),
                example=entry.get('example', ''),
                opcode=entry.get('opcode'),
                raw=bool(entry.get('raw')),
                evidence=entry.get('evidence', ''),
                parameters=tuple(
                    ParamInfo(p.get('name', ''), p.get('role', 'input'), p.get('meaning', ''),
                              p.get('evidence', 'none'))
                    for p in entry.get('parameters', ())
                ),
                shorthand=tuple((entry.get('shorthand') or {}).get('params', ())),
                positional=tuple((entry.get('shorthand') or {}).get('positional', ())),
            )

        self._aliases: dict[str, str] = dict(index.get('aliases') or {})
        self._structure: dict[str, StructureInfo] = {
            name: StructureInfo(
                name=name,
                summary=entry.get('summary', ''),
                signature=entry.get('signature', name),
                snippet=entry.get('snippet', name),
            )
            for name, entry in (index.get('structure') or {}).items()
        }
        self._directives: dict[str, str] = dict(index.get('directives') or {})

        if not self._commands:
            logger.warning('EVD command index is empty; parameter editing falls back to free text.')

    @staticmethod
    def _load_json(relative: str, fallback):
        path = get_resource_path(relative)
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f'Could not load {path}: {e}')
            return fallback

    def __bool__(self) -> bool:
        return bool(self._commands)

    def __contains__(self, name: str) -> bool:
        return self.canonical(name) in self._commands

    def canonical(self, name: str) -> str:
        '''Resolve an accepted spelling to the one the decompiler prints.'''
        return self._aliases.get(name, name)

    def get(self, name: str) -> CommandInfo | None:
        return self._commands.get(self.canonical(name))

    def structure(self, name: str) -> StructureInfo | None:
        return self._structure.get(name)

    @property
    def command_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._commands))

    @property
    def structure_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._structure))

    @property
    def directives(self) -> dict[str, str]:
        return dict(self._directives)

    def category(self, name: str) -> str:
        '''Control-flow category of a command name, for colouring.'''
        if name in _STRUCTURE_KEYWORDS:
            return CATEGORY_STRUCTURE
        info = self.get(name)
        if info is not None and info.opcode is not None:
            return self.category_of_opcode(info.opcode)
        if info is not None or name in self._structure:
            return CATEGORY_MACRO  # lowers to several commands, so it has no single opcode
        return CATEGORY_NORMAL

    @staticmethod
    def category_of_opcode(opcode: int) -> str:
        if opcode >= 0xF0:
            return CATEGORY_HIGH
        return _OPCODE_CATEGORIES.get(opcode, CATEGORY_NORMAL)

    def palette_entries(self) -> list[tuple[str, str, str]]:
        '''(name, family, summary) for everything droppable into a script.

        Structural constructs and macros come first under their own family so
        the common authoring moves are not buried in 135 alphabetical commands.
        '''
        entries: list[tuple[str, str, str]] = [
            (info.name, 'Structure', info.summary)
            for info in sorted(self._structure.values(), key=lambda i: i.name)
        ]
        entries.extend(
            (info.name, info.family, info.summary)
            for info in sorted(self._commands.values(), key=lambda i: (i.opcode if i.opcode is not None else 0x100, i.name))
            if info.name not in self._structure
        )
        return entries


_STRUCTURE_KEYWORDS = frozenset({
    'event', 'label', 'entry', 'header', 'headerExtra', 'header_extra',
    'markerTable', 'marker_table', 'option', 'bytes', 'cmd',
})

COMMANDS = CommandIndex()

###-------------------------------------------- Text forms -------------------------------------------###

_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def event_name_for(name: str) -> str:
    '''An EVDCODE event name has to be an identifier; EVD files are called `516_01`.'''
    cleaned = re.sub(r'[^A-Za-z0-9_]', '_', name.strip())
    if not cleaned:
        return 'Main'
    return cleaned if _IDENTIFIER.match(cleaned) else f'Event_{cleaned}'


def decompile_code(data: bytes, event_name: str = 'Main', annotate: bool = True) -> str:
    '''Raw EVD bytes to block-structured EVDCODE.

    `evd_tool` verifies its own output here: it recompiles the sugared form and
    falls back to the unsugared one when the bytes differ, so what comes back
    always reassembles to `data`.
    '''
    return evd_tool.decompile_evd_code(
        data,
        COMMAND_REGION_OFFSET,
        event_name_for(event_name),
        SYMBOLS.tables if annotate else None,
    )


def decompile_source(data: bytes, annotate: bool = True) -> str:
    '''Raw EVD bytes to flat EVDSRC, the intermediate EVDCODE lowers to.'''
    return evd_tool.decompile_evd_source(
        data, COMMAND_REGION_OFFSET, False, SYMBOLS.tables if annotate else None
    )


def compile_code(text: str) -> bytes:
    '''EVDCODE to raw EVD bytes. Raises EvdCompileError with the author's line.'''
    try:
        return evd_tool.compile_evd_code(text)
    except Exception as e:
        raise EvdCompileError.from_exception(e) from e


def compile_code_to_source(text: str) -> str:
    '''The EVDSRC an EVDCODE file lowers to, for inspection.'''
    try:
        return evd_tool.compile_evd_code_to_source(text)
    except Exception as e:
        raise EvdCompileError.from_exception(e) from e


def validate_code(text: str) -> EvdCompileError | None:
    '''None when `text` assembles, otherwise the first error.'''
    try:
        compile_code(text)
    except EvdCompileError as e:
        return e
    return None


@dataclass(frozen=True)
class Assembly:
    '''One compile of an EVDCODE file, plus where each line landed.'''
    data:    bytes | None = None
    offsets: dict[int, int] = field(default_factory=dict)  # EVDCODE line -> byte offset
    error:   EvdCompileError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def assemble(text: str) -> Assembly:
    '''Compile `text` and report the byte offset every line landed at.

    The offsets matter because labels in this format *are* offsets: the
    decompiler names a jump target `loc_02BC` after the byte it sits at. Once
    lines move, the names stay and the addresses shift, so the only way to find
    where `loc_02BC` actually is now is to assemble and look.

    They are recovered by walking the assembled command region and lining it up
    with the lowered EVDSRC, whose statements are emitted in that same order.
    If the two do not line up -- a `.bytes` region assembles to something the
    command walk cannot read -- no offsets are reported rather than wrong ones.
    '''
    try:
        parser = evd_tool.EVDCodeParser(text)
        lowered = parser.parse()
        data = evd_tool.compile_evd_source(lowered)
    except Exception:
        # Report it through compile_code, which maps the failure back onto the
        # line the author wrote instead of a line of the lowered intermediate.
        return Assembly(error=validate_code(text) or EvdCompileError('script did not assemble'))

    statements = [
        item for item in evd_tool.parse_source_items(lowered)
        if item['kind'] == 'cmd' and item['head'] not in _NON_EMITTING_DIRECTIVES
    ]
    walked = _walk_statement_offsets(data, statements)
    if walked is None:
        return Assembly(data=data)

    by_line: dict[int, int] = dict(_header_line_offsets(text, data))
    for item, offset in zip(statements, walked):
        author_line = parser.source_line_of.get(item['line_no'])
        if author_line is not None:
            by_line.setdefault(author_line, offset)

    # A label, a brace and a directive emit nothing, so they take the address of
    # the next thing that does -- which for a label is exactly its value.
    offsets: dict[int, int] = {}
    next_offset = len(data)
    for number in range(len(text.splitlines()), 0, -1):
        if number in by_line:
            next_offset = by_line[number]
        offsets[number] = next_offset
    return Assembly(data=data, offsets=offsets)


_NON_EMITTING_DIRECTIVES = frozenset({
    '.header', '.header_extra', '.entry', '.marker_table', '.org', '.align',
})

_RE_HEAD = re.compile(r'^\s*(?:event\s+\S+\s*\{|([A-Za-z_][A-Za-z0-9_]*)\s*\()')


def _header_line_offsets(text: str, data: bytes) -> dict[int, int]:
    '''Addresses for the lines that are file structure rather than commands.

    These are not commands, but they are bytes, and they are the first bytes:
    the magic at +0x00 and the two header words at +0x04 and +0x08. Without
    them the address column would begin partway down the file at the first
    command, when the file itself begins at 0000.
    '''
    fixed = {'header': 0x04, 'headerExtra': 0x08, 'header_extra': 0x08}
    table = evd_tool.u32(data, 0x08) * 4 if len(data) >= 12 else 0
    offsets: dict[int, int] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        match = _RE_HEAD.match(raw)
        if not match:
            continue
        head = match.group(1)
        if head is None:                       # `event Name {` -- the magic word
            offsets.setdefault(number, 0x00)
        elif head in fixed:
            offsets[number] = fixed[head]
        elif head in ('markerTable', 'marker_table') and table:
            offsets[number] = table
    return offsets


_CURSOR_DIRECTIVES = frozenset({'.org', '.align'})


def _walk_statement_offsets(data: bytes, statements: list[dict]) -> list[int] | None:
    '''Where each lowered statement landed, or None if the walk lost its place.

    The statements are in emission order, so walking them against the assembled
    bytes gives each one its address. It has to be a joint walk rather than a
    plain command walk: a `.bytes` region is raw data that decodes as garbage
    commands, and the marker table sits inside the command region without being
    one. Anything unexpected returns None -- no addresses beats wrong ones.
    '''
    header_extra = evd_tool.u32(data, 0x08) if len(data) >= 12 else 0
    marker_table = evd_tool.decode_marker_table_source(data, header_extra)
    offsets: list[int] = []
    cursor = COMMAND_REGION_OFFSET
    for item in statements:
        if marker_table and cursor == marker_table['offset']:
            cursor += int(marker_table['size'])
        if cursor >= len(data):
            return None
        head = item['head']
        if head in _CURSOR_DIRECTIVES:
            return None  # moves the cursor by an amount only the compiler knows
        offsets.append(cursor)
        if head == '.bytes':
            cursor += len(item['parts'])
            continue
        if head == '.word':
            cursor += 4 * len(item['parts'])
            continue
        if cursor % 4 or cursor + 4 > len(data):
            return None
        try:
            command = evd_tool.decode_command_at(data, cursor)
        except ValueError:
            return None
        end = int(command['end_offset'])
        if command.get('truncated') or end <= cursor:
            return None
        cursor = end
    return offsets


def label_for_offset(offset: int) -> str:
    '''The name the decompiler would give a label at `offset`.'''
    return evd_tool.label_name(offset)

###--------------------------------------------- Line model ------------------------------------------###

KIND_BLANK     = 'blank'
KIND_COMMENT   = 'comment'
KIND_EVENT     = 'event'      # `event Main {`
KIND_OPTION    = 'option'     # `option {` inside a choose
KIND_CLOSE     = 'close'      # `}`
KIND_ELSE      = 'else'       # `} else {`
KIND_LABEL     = 'label'      # `label(loc_000C)`
KIND_DIRECTIVE = 'directive'  # `header(...)`, `entry(...)`, `headerExtra(...)`
KIND_COMMAND   = 'command'    # any call that emits a command
KIND_UNKNOWN   = 'unknown'

# `markerTable` is the EVDCODE spelling the decompiler prints for the
# `.marker_table` directive; both are accepted on input.
_DIRECTIVE_HEADS = frozenset({'header', 'headerExtra', 'header_extra', 'entry',
                              'markerTable', 'marker_table'})

_RE_ELSE   = re.compile(r'^\}\s*else\s*\{$')
_RE_CLOSE  = re.compile(r'^\}$')
_RE_EVENT  = re.compile(r'^event\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{$')
_RE_OPTION = re.compile(r'^option\s*\{$')
_RE_CALL   = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*(\{)?$', re.S)


@dataclass(frozen=True)
class Arg:
    '''One argument of a call. `key` is empty for a positional argument.'''
    key:   str
    value: str

    def __str__(self) -> str:
        return f'{self.key}={self.value}' if self.key else self.value


@dataclass
class CodeLine:
    '''One line of EVDCODE, and everything the views need to render or edit it.

    `text` is the line exactly as it appears. Lines the user has not touched are
    written back verbatim, so decompiler output survives an unrelated edit
    untouched rather than being re-rendered through this model.
    '''
    number:  int                       # 1-based, matches compiler diagnostics
    text:    str
    indent:  int
    kind:    str
    head:    str = ''
    args:    tuple[Arg, ...] = ()
    comment: str = ''                  # including the leading '//'
    opens:   bool = False
    closes:  bool = False
    depth:   int = 0                   # nesting depth, from the brace walk
    block_end: int | None = None       # line number of the matching '}', for openers

    @property
    def is_call(self) -> bool:
        return self.kind in (KIND_COMMAND, KIND_LABEL, KIND_DIRECTIVE)

    @property
    def info(self) -> CommandInfo | None:
        return COMMANDS.get(self.head) if self.head else None

    @property
    def opcode(self) -> int | None:
        info = self.info
        return info.opcode if info else None

    @property
    def category(self) -> str:
        if self.kind in (KIND_EVENT, KIND_OPTION, KIND_CLOSE, KIND_ELSE, KIND_LABEL, KIND_DIRECTIVE):
            return CATEGORY_STRUCTURE
        if self.kind != KIND_COMMAND:
            return CATEGORY_NORMAL
        return COMMANDS.category(self.head)

    @property
    def target_label(self) -> str | None:
        '''Where this line jumps, when it jumps somewhere.'''
        for arg in self.args:
            if arg.key in ('goto', 'target'):
                return arg.value
        return None

    def arg(self, key: str) -> str | None:
        for a in self.args:
            if a.key == key:
                return a.value
        return None

    def with_args(self, args: tuple[Arg, ...], comment: str | None = None) -> 'CodeLine':
        '''A copy re-rendered from `args`; only for lines the user actually edited.'''
        rendered = render_call(
            self.indent, self.head, args,
            self.comment if comment is None else comment,
            self.opens,
        )
        return parse_line(rendered, self.number)


def example_to_call(example: str, indent: int = 0) -> str:
    '''Turn a documented EVDSRC example (`head a=1 b=2`) into an EVDCODE call.

    Every command in the index carries an example lifted from a shipped script,
    which is what makes a freshly inserted command assemble instead of arriving
    as an empty payload the author has to reverse engineer. The conversion is
    the decompiler's own, because it is not a join: EVDCODE separates arguments
    with commas, so any value that contains one (`xy=-320,-240`) has to be
    quoted on the way across.
    '''
    parts = evd_tool.split_source_tokens(example.strip())
    if not parts:
        return f'{" " * indent}{example.strip()}'
    return ' ' * indent + evd_tool.source_call_to_code(parts[0], parts[1:]).strip()


# Structural signatures are written to be read, not compiled: bodies are `...`
# and operands are the names of what goes there. Substituting concrete values
# turns them into something that assembles the moment it is inserted.
_TEMPLATE_BODY = 'nop()'
_TEMPLATE_OPERANDS = {
    'character': '1', 'item': '1', 'x': '0', 'y': '0', 'z': '0',
    # A head angle is pitch and yaw only; a posture is a full rotation. Their
    # masks are checked against the axes present, so the two cannot share one.
    'angle': '"x:0,y:0"', 'posture': '"x:0,y:0,z:0"', 'vector': '"0,0,0"',
    'speed': '1', 'sound': '0', 'trigger_type': '0', 'value': '0', 'stand': '0',
    'pan': '0', 'volume': '0',
}
_TEMPLATE_OPERAND_RE = re.compile(
    r'(?<=[(,\s])(' + '|'.join(_TEMPLATE_OPERANDS) + r')(?=[,)\s])'
)


def command_template(name: str, indent: int = 4, label: str = 'loc_new') -> str:
    '''Insertable starting text for `name`, or '' when there is nothing to insert.

    A documented command becomes its own example, a real line lifted from a
    shipped script, so it assembles as inserted. A structural construct becomes
    its signature with the placeholders filled in. A few (`option`, `raw`) only
    mean something once the author supplies the missing part and are returned as
    stubs that do not yet compile.
    '''
    pad = ' ' * indent
    structure = COMMANDS.structure(name)
    if structure is not None:
        # The block form wins over the command example where a name has both.
        # `if_value`'s example is the jump spelling, and its `goto=loc_090C`
        # names a label from the script it was lifted from -- which in another
        # script silently compiles to the raw offset 0x090C instead.
        text = structure.signature.replace('...', _TEMPLATE_BODY).replace('loc_name', label)
        text = _TEMPLATE_OPERAND_RE.sub(lambda m: _TEMPLATE_OPERANDS[m.group(1)], text)
        return '\n'.join(pad + _reindent(part) for part in text.splitlines())

    info = COMMANDS.get(name)
    if info is None:
        return ''
    if info.example:
        return example_to_call(info.example, indent)
    if info.positional:
        # A shorthand spelling that no shipped script uses has no example to
        # borrow, but its signature says what it takes and in what order.
        return f'{pad}{name}({", ".join(_TEMPLATE_OPERANDS.get(p, "0") for p in info.positional)})'
    return f'{pad}{name}()'


def _reindent(part: str) -> str:
    '''Signatures indent nested lines by two spaces; EVDCODE files use four.'''
    stripped = part.lstrip(' ')
    return ' ' * ((len(part) - len(stripped)) * 2) + stripped


def unique_label(lines: list[CodeLine], stem: str = 'loc_new') -> str:
    '''A label name not already defined in `lines`.'''
    taken = {name for name, _ in iter_labels(lines)}
    if stem not in taken:
        return stem
    index = 1
    while f'{stem}{index}' in taken:
        index += 1
    return f'{stem}{index}'


def render_call(indent: int, head: str, args: tuple[Arg, ...] | list[Arg], comment: str, opens: bool) -> str:
    body = ', '.join(str(a) for a in args)
    line = f'{" " * indent}{head}({body})'
    if opens:
        line += ' {'
    if comment:
        line += f'  {comment}'
    return line


def comment_start(line: str) -> int:
    '''Index of the trailing `//`, or -1. A `//` inside a quoted string is text.'''
    in_string = False
    escaped = False
    for i, ch in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == '/' and line[i + 1:i + 2] == '/':
            return i
    return -1


def split_comment(line: str) -> tuple[str, str]:
    '''Split off a trailing `// ...`, ignoring `//` inside a quoted string.'''
    index = comment_start(line)
    if index < 0:
        return line.rstrip(), ''
    return line[:index].rstrip(), line[index:].rstrip()


def split_args(text: str) -> tuple[Arg, ...]:
    '''Split a call's argument list on top-level commas, respecting quotes.'''
    args: list[Arg] = []
    depth = 0
    in_string = False
    escaped = False
    current = ''
    for ch in text:
        if in_string:
            current += ch
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            current += ch
            continue
        if ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        if ch == ',' and depth <= 0:
            if current.strip():
                args.append(_make_arg(current))
            current = ''
            continue
        current += ch
    if current.strip():
        args.append(_make_arg(current))
    return tuple(args)


def _make_arg(raw: str) -> Arg:
    token = raw.strip()
    key, sep, value = token.partition('=')
    if not sep:
        return Arg('', token)
    return Arg(key.strip(), value.strip())


def parse_line(text: str, number: int) -> CodeLine:
    '''Parse one EVDCODE line. Never raises: anything unrecognised stays KIND_UNKNOWN.'''
    indent = len(text) - len(text.lstrip(' '))
    code, comment = split_comment(text)
    stripped = code.strip()

    if not stripped:
        kind = KIND_COMMENT if comment else KIND_BLANK
        return CodeLine(number, text.rstrip(), indent, kind, comment=comment)

    if _RE_ELSE.match(stripped):
        return CodeLine(number, text.rstrip(), indent, KIND_ELSE, head='else',
                        comment=comment, opens=True, closes=True)
    if _RE_CLOSE.match(stripped):
        return CodeLine(number, text.rstrip(), indent, KIND_CLOSE, head='}',
                        comment=comment, closes=True)

    match = _RE_EVENT.match(stripped)
    if match:
        return CodeLine(number, text.rstrip(), indent, KIND_EVENT, head='event',
                        args=(Arg('', match.group(1)),), comment=comment, opens=True)
    if _RE_OPTION.match(stripped):
        return CodeLine(number, text.rstrip(), indent, KIND_OPTION, head='option',
                        comment=comment, opens=True)

    match = _RE_CALL.match(stripped)
    if match:
        head, body, brace = match.group(1), match.group(2), match.group(3)
        if head == 'label':
            kind = KIND_LABEL
        elif head in _DIRECTIVE_HEADS:
            kind = KIND_DIRECTIVE
        else:
            kind = KIND_COMMAND
        return CodeLine(number, text.rstrip(), indent, kind, head=head,
                        args=split_args(body), comment=comment, opens=bool(brace))

    return CodeLine(number, text.rstrip(), indent, KIND_UNKNOWN, comment=comment)


def parse_code(text: str) -> list[CodeLine]:
    '''Parse a whole EVDCODE file into one CodeLine per text line.

    Also resolves nesting: every line carries its depth, and every opener the
    line number of its matching `}`, which is what lets the structure view move
    or delete a block as a unit instead of stranding its body.
    '''
    lines = [parse_line(raw, number) for number, raw in enumerate(text.splitlines(), start=1)]
    open_stack: list[CodeLine] = []
    depth = 0
    for line in lines:
        if line.kind == KIND_ELSE:
            # `} else {` closes the if body and opens the else body at the same
            # depth, so it neither indents nor pairs as a new opener.
            if open_stack:
                depth = max(0, depth - 1)
            line.depth = depth
            depth += 1
            continue
        if line.closes:
            depth = max(0, depth - 1)
            line.depth = depth
            if open_stack:
                open_stack.pop().block_end = line.number
            continue
        line.depth = depth
        if line.opens:
            open_stack.append(line)
            depth += 1
    for orphan in open_stack:
        orphan.block_end = None
    return lines


def block_range(lines: list[CodeLine], number: int) -> tuple[int, int]:
    '''The 1-based inclusive line span a line owns: itself, or itself plus its block.'''
    index = number - 1
    if not (0 <= index < len(lines)):
        return number, number
    line = lines[index]
    if line.opens and line.block_end:
        return line.number, line.block_end
    return line.number, line.number


def render_code(lines: list[CodeLine]) -> str:
    return '\n'.join(line.text for line in lines) + '\n'


def foldable(lines: list[CodeLine]) -> dict[int, int]:
    '''Opener line -> its closing line, for every block that can be collapsed.

    A block is worth folding only if it has something inside it; `event Main {`
    counts, so the whole script can be collapsed to one line.
    '''
    return {line.number: line.block_end for line in lines
            if line.opens and line.block_end and line.block_end > line.number + 1}


def hidden_lines(lines: list[CodeLine], folded: set[int]) -> set[int]:
    '''Line numbers inside a collapsed block, including nested ones.

    A fold inside a fold contributes nothing extra -- its lines are already
    hidden by the outer one -- so unfolding the outer block restores whatever
    fold state the inner blocks were left in.
    '''
    spans = foldable(lines)
    hidden: set[int] = set()
    for opener in folded:
        end = spans.get(opener)
        if end:
            hidden.update(range(opener + 1, end + 1))
    return hidden


def fold_summary(lines: list[CodeLine], opener: int) -> str:
    '''What a collapsed block shows in place of its body: ` ... }`.'''
    end = foldable(lines).get(opener)
    if not end:
        return ''
    return f' ... }}   ({end - opener - 1} lines)'


def iter_labels(lines: list[CodeLine]) -> Iterator[tuple[str, int]]:
    '''(name, line number) for every `label(...)` in the file.'''
    for line in lines:
        if line.kind == KIND_LABEL and line.args:
            yield line.args[0].value, line.number


_LOOKS_LIKE_LABEL = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


BRANCH_OPCODE = 0x02
_LABEL_DIRECTIVES = frozenset({'entry', 'markerTable', 'marker_table'})


def iter_label_targets(lines: list[CodeLine]) -> Iterator[tuple[CodeLine, str]]:
    '''(line, label name) for everything that names a jump target.

    `goto=` is always a label, but `target=` is overloaded: on a branch it is the
    destination, while on `set_sound_listener` it is an enum naming what to
    follow. Only a branch's `target=` counts.
    '''
    for line in lines:
        if line.kind == KIND_DIRECTIVE and line.head in _LABEL_DIRECTIVES:
            for arg in line.args:
                yield line, arg.value
            continue
        branch = line.opcode == BRANCH_OPCODE
        for arg in line.args:
            if arg.key == 'goto' or (branch and arg.key == 'target'):
                yield line, arg.value


def undefined_label_problems(lines: list[CodeLine]) -> list[EvdCompileError]:
    '''Jump targets that name a label the script does not define.

    The compiler does not catch these. `loc_0910` parses as the raw byte offset
    0x0910 when no such label exists, so a branch left pointing at a label that
    was deleted -- or renamed, or never existed in this script -- assembles
    quietly into a jump to whatever now sits at that offset. Moving and deleting
    lines is most of what this editor does, which is exactly what strands them.
    '''
    defined = {name for name, _ in iter_labels(lines)}
    problems: list[EvdCompileError] = []
    for line, name in iter_label_targets(lines):
        if name in defined or not _LOOKS_LIKE_LABEL.match(name):
            continue  # a plain number is a deliberate raw offset, not a mistake
        try:
            offset = evd_tool.parse_hex_int(name)
        except ValueError:
            problems.append(EvdCompileError(
                f'{line.head} targets {name}, which is not a label in this script', line.number))
            continue
        problems.append(EvdCompileError(
            f'{line.head} targets {name}, which is not a label in this script; '
            f'it will assemble as the raw byte offset 0x{offset:04X}', line.number))
    return problems


def label_references(lines: list[CodeLine]) -> dict[str, int]:
    '''How many lines jump to each label, so unreferenced ones can be flagged.'''
    counts: dict[str, int] = {}
    for line in lines:
        for arg in line.args:
            if arg.key in ('goto', 'target'):
                counts[arg.value] = counts.get(arg.value, 0) + 1
            elif line.kind == KIND_DIRECTIVE and line.head in _LABEL_DIRECTIVES:
                counts[arg.value] = counts.get(arg.value, 0) + 1
    return counts

###-------------------------------------------- Line editing -----------------------------------------###

###------------------------------------------ Packed operands ----------------------------------------###

# A character selector packs an id and a variant into one word. `evd_tool`'s own
# spec tuples are reused so the layouts here cannot drift from the ones the
# compiler enforces.
_PACKED_SPECS: dict[str, tuple[tuple[str, int, int], ...]] = {
    'parent': evd_tool.PARENT_WORD_SPECS,
}


def packed_spec(info: 'CommandInfo | None', key: str) -> tuple[tuple[str, int, int], ...] | None:
    '''How `key` splits into named parts, or None when it is a plain value.

    `character` splits two ways depending on the command: byte 2 is a variant on
    most, and a type selector on the handful that read it as one. The command's
    own parameter list says which.
    '''
    if key == 'character':
        if info is not None and 'character_type' in info.by_name:
            return evd_tool.CHARACTER_TYPE_SPECS
        return evd_tool.CHARACTER_VARIANT_SPECS
    return _PACKED_SPECS.get(key)


def split_packed(word: int, specs: tuple[tuple[str, int, int], ...]) -> dict[str, int]:
    return {name: (word >> shift) & mask for name, shift, mask in specs}


def compose_packed(word: int, values: dict[str, int],
                   specs: tuple[tuple[str, int, int], ...]) -> int:
    '''`word` with the named parts in `values` written into their fields.

    Only the fields named are touched, so bits the specs do not cover -- the top
    byte of a character selector, which no handler has been shown to read --
    survive rather than being zeroed by an edit that never mentioned them.
    '''
    for name, shift, mask in specs:
        if name in values:
            word = (word & ~((mask << shift) & 0xFFFFFFFF)) | ((values[name] & mask) << shift)
    return word & 0xFFFFFFFF


###------------------------------------------ Named id operands --------------------------------------###

# Domains with good enough coverage to offer as a list. Flags are deliberately
# out: names exist for 175 of 8,191, so a list would hide far more than it shows.
PICKABLE_DOMAINS = ('character', 'item', 'bgm', 'location')

# A packed word is not an id, so it never gets a list even though the decompiler
# annotates it -- its id half is a separate field and that is what gets picked.
_NOT_PICKABLE = frozenset({'character'})


def symbol_domain(info: 'CommandInfo | None', key: str) -> str | None:
    '''Which id table `key` draws from, or None if it is a plain number.

    The mapping is `evd_tool`'s own, the same one that decides which fields get
    a `// name` comment on decompile, so a field offers a list exactly when the
    decompiler would have named it.
    '''
    if key in _NOT_PICKABLE:
        return None
    engine = evd_tool.resolve_form_name(info.name) if info is not None else ''
    engine_key = evd_tool.resolve_parameter_name(engine, key) if engine else key
    overrides = evd_tool.FORM_SYMBOL_DOMAINS.get(engine, {})
    domain = (overrides.get(key) or overrides.get(engine_key)
              or evd_tool.PARAMETER_SYMBOL_DOMAINS.get(key)
              or evd_tool.PARAMETER_SYMBOL_DOMAINS.get(engine_key))
    return domain if domain in PICKABLE_DOMAINS else None


def is_writable_head(name: str) -> bool:
    """Whether the compiler accepts `name` as a command head.

    The index can publish a command under the dispatch table's handler name,
    which is not always a spelling an author can write -- `nop_ff` is the
    handler for 0xFF, `return_zero` is the command. Offering one the compiler
    rejects is worse than the coverage gap it fills.
    """
    return evd_tool.source_head_is_compilable(name)


def domain_choices(domain: str) -> list[tuple[int, str]]:
    '''Every (id, name) in `domain`, lowest id first.

    Character includes the abstraction codes -- "current character" and the
    party slots -- which are not real ids but are the most common operands in
    the format, and are what an author reaches for first.
    '''
    entries = dict(SYMBOLS._tables.get(domain, {}))
    if domain == 'character':
        entries.update(evd_tool.CHARACTER_ABSTRACTION_CODES)
    return sorted(entries.items())


###------------------------------------------ Value occurrences --------------------------------------###

_TOKEN = re.compile(r'0x[0-9A-Fa-f]+|-?\d+(?:\.\d+)?|"(?:[^"\\]|\\.)*"|[A-Za-z_][A-Za-z0-9_]*')


def value_key(text: str) -> str | None:
    '''A comparable identity for a value, or None if it is not one.

    Numbers compare numerically, so `1000` and `0x3E8` are the same value --
    which matters here because the decompiler prints flags in decimal and
    masks in hex, and an author tracing an event value through a script should
    not have to notice which spelling a line happened to use.
    '''
    token = text.strip()
    if not token:
        return None
    number = parse_number(token)
    if number is not None:
        return f'#{number}'
    if token.startswith('"'):
        return f'={token}'
    return None


def token_at(text: str, column: int) -> tuple[str, int, int] | None:
    '''The token containing `column`, as (text, start, end).'''
    for match in _TOKEN.finditer(text):
        if match.start() <= column <= match.end():
            return match.group(), match.start(), match.end()
    return None


def value_occurrences(line: CodeLine, key: str) -> list[tuple[int, int]]:
    '''Spans in `line.text` whose value matches `key`.

    Only argument values count. A parameter *name* that happens to read as a
    number is not a value, and neither is a head or a label, so tracing `1000`
    never lights up something that merely contains it.
    '''
    spans: list[tuple[int, int]] = []
    if not line.is_call:
        return spans
    open_paren = line.text.find('(')
    if open_paren < 0:
        return spans
    limit = comment_start(line.text)
    if limit < 0:
        limit = len(line.text)
    for match in _TOKEN.finditer(line.text, open_paren, limit):
        before = line.text[:match.start()].rstrip()
        if before.endswith('='):                       # a value
            pass
        elif before.endswith(('(', ',')):              # a positional value
            pass
        else:
            continue
        if line.text[match.end():match.end() + 1] == '=':
            continue                                   # actually a parameter name
        if value_key(match.group()) == key:
            spans.append((match.start(), match.end()))
    return spans


def parse_number(text: str) -> int | None:
    '''Parse a field value the way the compiler does, or None if it is not a number.'''
    try:
        return evd_tool.parse_hex_int(text.strip())
    except (ValueError, AttributeError):
        return None


_MAX_DERIVED_RETRIES = 8
_ANNOTATION_ENTRY = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$')


def prune_annotation(comment: str, changed_keys: set[str]) -> str:
    '''Drop the decompiler's `// field=Name` notes for fields that just changed.

    The names are looked up from the ids, so leaving them after an edit would
    label the new id with the old name. Anything that is not a generated
    annotation -- a comment the user wrote -- is left exactly as it is.
    '''
    if not comment.startswith('//') or not changed_keys:
        return comment
    entries = [part.strip() for part in comment[2:].split(',')]
    parsed = [_ANNOTATION_ENTRY.match(entry) for entry in entries]
    if not all(parsed):
        return comment
    kept = [entry for entry, match in zip(entries, parsed) if match.group(1) not in changed_keys]  # type: ignore[union-attr]
    return f'// {", ".join(kept)}' if kept else ''


@dataclass
class EditResult:
    '''Outcome of applying a parameter edit to one line.'''
    text:    str = ''
    dropped: tuple[str, ...] = ()       # derived fields the compiler made us recompute
    error:   EvdCompileError | None = None
    changed_line: str = ''
    line:    int = 0                    # the line the edit targeted
    offsets: dict[int, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def error_text(self) -> str:
        '''The error, pointing at the edited line when the compiler did not.

        Some rejections come out of a bare conversion rather than a checked
        field, so they carry no line of their own. Naming the line that was
        being edited is still true and is the only place the user can look.
        '''
        if self.error is None:
            return ''
        if self.error.line or not self.line:
            return str(self.error)
        return f'line {self.line}: {self.error.message}'


def apply_line_edit(lines: list[CodeLine], number: int, args: tuple[Arg, ...],
                    comment: str | None = None) -> EditResult:
    '''Replace line `number`'s arguments and re-validate the whole script.

    Editing an input leaves the parameters derived from it stale, and the
    compiler rejects that rather than ignoring it. Rather than guessing a
    dependency graph, this drops exactly the field each rejection names and
    retries: the compiler is the only thing that actually knows.
    '''
    index = number - 1
    if not (0 <= index < len(lines)):
        return EditResult(error=EvdCompileError(f'no line {number} to edit'), line=number)

    original = lines[index]
    candidate = list(args)
    dropped: list[str] = []

    explicit_comment = comment
    before = {a.key: a.value for a in original.args}
    changed = {a.key for a in args if a.key and before.get(a.key) != a.value}
    changed |= {key for key in before if key and key not in {a.key for a in args}}

    for _ in range(_MAX_DERIVED_RETRIES):
        note = (explicit_comment if explicit_comment is not None
                else prune_annotation(original.comment, changed | set(dropped)))
        edited = original.with_args(tuple(candidate), note)
        text = render_code(lines[:index] + [edited] + lines[index + 1:])
        # assemble rather than validate: the offsets come out of the same
        # compile the check needs, so the addresses stay live for free.
        result = assemble(text)
        error = result.error
        if error is None:
            return EditResult(text=text, dropped=tuple(dropped), changed_line=edited.text,
                              line=number, offsets=result.offsets)
        stale = error.conflicting_field
        blames_elsewhere = error.line is not None and error.line != number
        if stale is None or blames_elsewhere or not any(a.key == stale for a in candidate):
            return EditResult(error=error, line=number)
        candidate = [a for a in candidate if a.key != stale]
        dropped.append(stale)

    return EditResult(line=number, error=EvdCompileError(
        'could not reconcile the derived parameters on this line', number
    ))


def apply_text_edit(lines: list[CodeLine], number: int, text_line: str) -> EditResult:
    '''Replace one line with raw text and re-validate.'''
    index = number - 1
    if not (0 <= index < len(lines)):
        return EditResult(error=EvdCompileError(f'no line {number} to edit'), line=number)
    replacement = parse_line(text_line, number)
    text = render_code(lines[:index] + [replacement] + lines[index + 1:])
    result = assemble(text)
    return EditResult(text='' if result.error else text, error=result.error,
                      changed_line=replacement.text, line=number, offsets=result.offsets)

###--------------------------------------------- Statistics ------------------------------------------###

@dataclass(frozen=True)
class ScriptStats:
    '''Headline numbers for the toolbar.'''
    byte_size:     int
    line_count:    int
    command_count: int
    label_count:   int
    block_count:   int
    header_flags:  int
    marker_count:  int
    raw_commands:  int = 0    # commands with no structured form, carrying raw words

    def summary(self) -> str:
        parts = [
            f'{self.command_count} commands',
            f'{self.label_count} labels',
            f'{self.byte_size} bytes',
        ]
        if self.block_count:
            parts.insert(1, f'{self.block_count} blocks')
        if self.marker_count:
            parts.append(f'{self.marker_count} markers')
        if self.raw_commands:
            parts.append(f'{self.raw_commands} raw')
        return ', '.join(parts)


def script_stats(data: bytes, lines: list[CodeLine]) -> ScriptStats:
    header_flags = evd_tool.u32(data, 0x04) if len(data) >= 8 else 0
    header_extra = evd_tool.u32(data, 0x08) if len(data) >= 12 else 0
    marker_table = evd_tool.decode_marker_table_source(data, header_extra) if header_extra else None
    return ScriptStats(
        byte_size=len(data),
        line_count=len(lines),
        command_count=sum(1 for line in lines if line.kind == KIND_COMMAND),
        label_count=sum(1 for line in lines if line.kind == KIND_LABEL),
        block_count=sum(1 for line in lines if line.opens and line.kind == KIND_COMMAND),
        header_flags=header_flags,
        marker_count=len(marker_table['targets']) if marker_table else 0,
        raw_commands=sum(
            1 for line in lines
            if line.kind == KIND_COMMAND and (line.info.raw if line.info else False)
        ),
    )
