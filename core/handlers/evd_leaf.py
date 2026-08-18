'''
Handler for EVD script files.

Decoding and encoding are `core.evd.api`'s job, which is a thin layer over the
vendored `evd_tool`. That tool round-trips every EVD the game ships, so this
handler does no format work of its own: it decompiles to EVDCODE on the worker
thread, and compiles EVDCODE back to bytes on save. Anything the compiler
refuses is a rejected save, not a silently mangled script.
'''
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from core.registry import Registry
from core.node import VfsNode
from core.contracts import LeafHandler
from core.workers import ActionDef, ActionType
from core.evd import api
from core.evd.api import CodeLine, ScriptStats

import logging
logger = logging.getLogger(f'radiata.{__name__}')


class EvdError(RuntimeError):
    '''Raised when an EVD cannot be presented as a script at all.'''


@dataclass(frozen=True)
class EvdEditorPayload:
    '''What the worker thread hands the editor.

    `code` is the single source of truth: `lines` is a parse of it, and both
    views edit it. `source` is the flat EVDSRC the block form lowers to, kept
    only so the debug panel can show what the compiler actually sees.
    '''
    name:     str
    raw:      bytes
    code:     str
    lines:    tuple[CodeLine, ...]
    stats:    ScriptStats
    offsets:  dict[int, int]            # EVDCODE line -> byte offset in the assembled file
    source:   str = ''
    warning:  str = ''

    @property
    def line_count(self) -> int:
        return len(self.lines)


class EvdSavePayload(NamedTuple):
    '''What EvdEditor.current_data() returns and decode_editor_data receives.'''
    code: str


###---------------------------------------------------- Handler -----------------------------------------------------###

@Registry.register(
    'EVD Script Handler',
    extensions=('.evd',),
    supported_actions=(
        ActionDef(name='Skip cutscenes', action_type=ActionType.PATCH),
        ActionDef('Properties', ActionType.DIALOG),
    ))
class EVDHandler(LeafHandler):
    '''Leaf handler for EVD script files.'''

    def __init__(self, source: bytes, parent: VfsNode | None = None) -> None:
        super().__init__(source)
        self._raw = source

    ###------------------------------------- Editor pipeline -------------------------------------###

    def prepare_editor_data(self, node: VfsNode, raw_bytes: bytes) -> EvdEditorPayload:
        '''Decompile to EVDCODE. Runs on a worker thread; see documentation.md.'''
        data = raw_bytes or self._raw
        if not data.startswith(api.EVD_MAGIC):
            raise EvdError(f'{node.name} is not an EVD script (expected magic {api.EVD_MAGIC!r})')
        try:
            code = api.decompile_code(data, node.name)
        except Exception as e:
            raise EvdError(f'Could not decompile {node.name}: {e}') from e

        lines = api.parse_code(code)
        # Assembled here on the worker thread rather than on first edit: the
        # check that it round-trips and the byte offset of every line come out
        # of the same compile, and the editor needs the offsets to draw its
        # address column the moment the script opens.
        warning = ''
        built = api.assemble(code)
        if not built.ok:
            warning = f'Decompiled script does not compile: {built.error}'
            logger.error(f'{node.name}: {warning}')
        elif built.data != data:
            # The decompiler already proved this round-trips, so a failure here
            # is a real defect rather than an unsupported script.
            warning = 'Decompiled script does not reassemble to the original bytes; saving would change the file.'
            logger.error(f'{node.name}: {warning}')
        elif not built.offsets:
            logger.warning(f'{node.name}: line addresses unavailable; the address column will be blank')

        try:
            source = api.decompile_source(data)
        except Exception as e:
            source = f'; EVDSRC unavailable: {e}'

        return EvdEditorPayload(
            name=node.name,
            raw=data,
            code=code,
            lines=tuple(lines),
            stats=api.script_stats(data, lines),
            offsets=built.offsets,
            source=source,
            warning=warning,
        )

    def decode_editor_data(self, node: VfsNode, payload: EvdSavePayload, **kwargs) -> bytes:
        '''Compile the edited EVDCODE back to raw bytes.'''
        if not isinstance(payload, EvdSavePayload):
            raise ValueError('Invalid payload: expected EvdSavePayload')
        data = api.compile_code(payload.code)
        logger.info(f'{node.name}: compiled {len(payload.code.splitlines())} EVDCODE lines to {len(data)} bytes')
        return data

    ###---------------------------------------- Actions ------------------------------------------###

    def execute_action(self, node: VfsNode, action_name: str, **kwargs):
        if action_name == 'Skip cutscenes':
            return self.skip_cutscenes(node)
        if action_name == 'Properties':
            return self.properties()
        return None

    def properties(self) -> str:
        data = self._raw
        if not data.startswith(api.EVD_MAGIC):
            return 'error: not an EVD script'
        try:
            lines = api.parse_code(api.decompile_code(data))
        except Exception as e:
            return f'error: {e}'
        stats = api.script_stats(data, lines)
        return (
            f'header flags: {stats.header_flags}\n'
            f'marker table entries: {stats.marker_count}\n'
            f'commands: {stats.command_count}\n'
            f'blocks: {stats.block_count}\n'
            f'labels: {stats.label_count}\n'
            f'commands with no structured form: {stats.raw_commands}\n'
            f'size: {stats.byte_size} bytes'
        )

    def skip_cutscenes(self, node: VfsNode) -> None:
        raise NotImplementedError('Cutscene skipping is not yet implemented')
