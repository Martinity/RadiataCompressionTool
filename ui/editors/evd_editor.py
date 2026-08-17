'''
EVD script editor

This is an early version with clear limitations. The current scope was to
get the overall script structure correctly mapped. This means that individual
instructions are not yet represented accurately.

Limitations:
- Mutations trigger a complete reencoding of the script sequence.
- Most instructions use default values for their fields.
- Instructions that support custom fields only accept fields on import.
- Qt data model for the MutableInstructions causes large render time freezes.
'''
from __future__ import annotations

import time
from PyQt6.QtCore import Qt, QMimeData, QPoint, pyqtSignal
from PyQt6.QtGui import QDrag, QShortcut, QKeySequence
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QHBoxLayout, QSizePolicy, QListWidgetItem,
    QStackedLayout, QListWidget, QScrollBar, QSplitter, QLineEdit, QInputDialog,
    QApplication, QMenu,  QPlainTextEdit, QPushButton
)
from core.contracts import BaseEditor
from core.node import VfsNode
from core.registry import Registry
from core.handlers.evd_leaf import (
    EvdEditorPayload, EVDHandler, EvdInfo, EvdInstruction, EvdMarkerTable,
    InstructionDescription, MutableInstruction, SymbolicJump, OpcodeInfo,
    OPCODES, JUMP_OPCODE, EVD_KEEP_GOING_FLAG, EvdError, EvdScript, EvdSavePayload
)

import logging
logger = logging.getLogger(f'radiata.{__name__}')

_CATEGORY_COLORS = {
    'jump':          '#B03030',
    'script_start':  '#2E7D32',
    'marker_seek':   '#6A1B9A',
    'calc':          '#1565C0',
    'high':          '#8B008B',
    'end':           '#B8860B',
    'normal':        '#BBBBBB',
}
_UNREACHED_COLOR = '#9E9E9E'

_MIME_MOVE_INSTRUCTION = 'application/x-evd-instruction-id'
_MIME_NEW_INSTRUCTION = 'application/x-evd-new-instruction'

###---------------------------------------------- Editor --------------------------------------------------###

@Registry.register_editor(
    name='EVD Script Editor',
    handler=EVDHandler,
    extensions=('.evd',),
    categories=(),
    is_fallback=False,
)
class EvdEditor(BaseEditor):
    '''
    EVD script editor with drag-and-drop reordering and instruction
    insertion.

    self._editable is the live, editable instruction sequence

    Every mutation re-encodes the entire EvdScript *yikes*. This is,
    hopefully, a temporary limitation due to a simple implementation for
    keeping the UI accurate to the symbolic jump target references.
    '''
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info   = None
        self._header = None
        self._instructions:     list[EvdInstruction] = []
        self._descriptions:     list[InstructionDescription] = []
        self._editable:         list[MutableInstruction] = []
        self._marker_table:     EvdMarkerTable | None = None
        self._unreached_ranges: tuple = ()
        self._reached_offsets:  frozenset[int] = frozenset()
        self._next_new_id = -1  # negative, so it can never collide with a real byte-offset-derived id
        self._generation  = 0
        self._undo_stack: list[list[MutableInstruction]] = []
        self._redo_stack: list[list[MutableInstruction]] = []
        self._MAX_UNDO = 50
        self._setup_ui()

    def _setup_ui(self) -> None:
        '''Setup the UI layout and widgets'''
        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self._status_label = QLabel()
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)

        # Main editor widget
        editor_widget = QWidget()
        editor_layout = QVBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        editor_layout.addWidget(self._build_toolbar())

        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        # New instruction panel
        self._instruction_list = SequenceListWidget()
        self._instruction_list.setObjectName('TextMono')
        self._instruction_list.setUniformItemSizes(True)
        self._instruction_list.setSpacing(0)
        self._instruction_list.instructionMoved.connect(self._on_instruction_moved)
        self._instruction_list.opcodeDropped.connect(self._on_opcode_dropped)
        self._instruction_list.deleteRequested.connect(self._on_delete_requested)

        self._scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self._instruction_list.setVerticalScrollBar(self._scrollbar)

        self._opcode_toolbar = OpcodeToolbar()

        main_splitter.addWidget(self._instruction_list)
        main_splitter.addWidget(self._opcode_toolbar)
        main_splitter.setStretchFactor(0, 4)
        main_splitter.setStretchFactor(1, 1)

        # Debug panel
        self._debug_panel = DebugPanel()

        vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        vertical_splitter.addWidget(main_splitter)
        vertical_splitter.addWidget(self._debug_panel)
        vertical_splitter.setStretchFactor(0, 3)
        vertical_splitter.setStretchFactor(1, 1)

        editor_layout.addWidget(vertical_splitter)
        editor_layout.addWidget(self._build_status_bar())

        self._stack.addWidget(self._status_label)
        self._stack.addWidget(editor_widget)

    def _build_toolbar(self) -> QWidget:
        '''The stop bar, overlapping with status bar. Will be changed.'''
        bar = QWidget()
        bar.setObjectName('SurfaceToolbar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 4, 6, 4)

        self._info_label = QLabel('Instructions: 0')
        lay.addWidget(self._info_label)

        lay.addStretch(1)
        lay.addWidget(_LegendWidget())
        return bar

    def _build_status_bar(self) -> QWidget:
        '''The bottom bar, overlapping with toolbar. Will be changed.'''
        bar = QWidget()
        bar.setObjectName('SurfaceStatusBar')
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 2, 6, 2)
        self._status_bar_label = QLabel('Ready')
        self._status_bar_label.setWordWrap(True)
        lay.addWidget(self._status_bar_label, stretch=1)
        return bar

    def _set_status(self, message: str, error: bool) -> None:
        color = '#B03030' if error else '#2E7D32'
        self._status_bar_label.setStyleSheet(f'color: {color};')
        self._status_bar_label.setText(message)

    def begin_loading(self, node: VfsNode) -> None:
        '''Loading placeholder.'''
        super().begin_loading(node)
        self._reset_state()
        self._status_label.setText(f'Loading {node.name}...')
        self._stack.setCurrentIndex(0)
        self._set_controls_enabled(False)

    def receive_data(self, result: EvdEditorPayload, data_resolver=None) -> None:
        '''Verify payload and send to populate_ui.'''
        self._data_resolver = data_resolver
        if not isinstance(result, EvdEditorPayload):
            self.show_load_error(
                f'Unexpected result type: {type(result).__name__}, expected EvdEditorPayload'
            )
            return
        self._original_payload = result
        self.set_dirty(False)
        self._populate_ui(result)
        if not self._info:
            raise EvdError('EvdEditorPayload failed to set info.')
        self._set_status(f'Loaded ({self._info.instruction_count} instructions)', error=False)
        self._stack.setCurrentIndex(1)
        self._set_controls_enabled(True)
        logger.info(f'[gen {self._generation}] loaded {self._info.instruction_count} instructions, '
                    f'{self._info.unreached_byte_count} unreached bytes')

    def show_load_error(self, message: str) -> None:
        self._status_label.setText(f'Load failed:\n{message}')
        self._stack.setCurrentIndex(0)
        self._set_controls_enabled(False)
        logger.error(message)

    def show_error(self, message: str) -> None:
        '''Writes error to log and status bar.'''
        logger.error(f'[gen {self._generation}] {self.__class__.__name__}: {message}')
        self._set_status(message, error=True)

    def _populate_ui(self, data: EvdEditorPayload) -> None:
        '''Populate the UI with the MutableInstruction sequence.
        Used for the initial load and after every successful edit.'''
        self._info             = data.info
        self._header           = data.header
        self._instructions     = data.instructions
        self._descriptions     = data.descriptions
        self._editable         = list(data.mutable)
        self._marker_table     = data.marker_table
        self._unreached_ranges = data.unreached_ranges
        self._reached_offsets  = data.reached_offsets

        selected_id = self._instruction_list.current_instruction_id()

        self._instruction_list.clear()
        for instr, desc in zip(self._instructions, self._descriptions):
            reached = instr.byte_offset in self._reached_offsets
            row = InstructionRow(instr, desc, reached)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, instr.byte_offset)
            item.setSizeHint(row.sizeHint())
            self._instruction_list.addItem(item)
            self._instruction_list.setItemWidget(item, row)

        if selected_id is not None:
            # Best-effort only: ids are derived from byte_offset (see
            # EvdScript.to_editable), and every edit re-encodes + re-decodes
            # the whole file, which reassigns fresh ids to everything whose
            # byte offset shifted -- in practice, anything after the edit
            # point. This only actually preserves selection when the edited
            # instruction is at or after the selected row. A real fix needs
            # identity decoupled from byte offset (e.g. a persistent counter
            # threaded through encode/decode instead of derived from
            # position) -- not attempted here; selection silently clearing
            # on an earlier edit is a known, visible limitation, confirmed
            # by direct test, not a hidden one.
            self._instruction_list.select_instruction_id(selected_id)

        marker_note = f', {len(self._marker_table.entries)} markers' if self._marker_table else ''
        unreached_note = f', {self._info.unreached_byte_count} unreached bytes' if self._info.unreached_byte_count else ''
        self._info_label.setText(f'Instructions: {self._info.instruction_count}{marker_note}{unreached_note}')

        self._debug_panel.update_state(self._editable, self._generation)

    def current_data(self) -> EvdSavePayload:
        '''Live editable state, flows to EVDHandler.decode_editor_data on save.'''
        if not self._header:
            raise EvdError('No header to start save.')
        return EvdSavePayload(self._header, self._editable)

    def discard_changes(self) -> None:
        if self.is_dirty() and self.current_node and self._original_payload is not None:
            self._pending_data = None
            self._populate_ui(self._original_payload)
            self.set_dirty(False)

    def _reset_state(self) -> None:
        self._info             = None
        self._header           = None
        self._instructions     = []
        self._descriptions     = []
        self._editable          = []
        self._marker_table     = None
        self._unreached_ranges = ()
        self._reached_offsets  = frozenset()
        self._next_new_id      = -1
        self._generation       = 0
        self._undo_stack       = []
        self._redo_stack       = []
        self._instruction_list.clear()
        self._info_label.setText('Instructions: 0')
        self._emit_undo_state()

    def _set_controls_enabled(self, enabled: bool) -> None:
        '''Toggle any custom buttons (export?)'''
        pass

    ### Edit application ------------------------------------------------------

    def _apply_edit(self, mutated_editable: list[MutableInstruction], action_desc: str) -> bool:
        '''
        Attempt to commit mutated_editable as the new state.
        Deleting an instruction that was some other JUMP's target leaves
        that JUMP unresolvable, which surfaces here as an encode-time ValueError.
        '''
        if self._header is None:
            self.show_error(f'{action_desc}: no header loaded')
            return False

        t0 = time.monotonic()
        try:
            encoded = EvdScript.encode_with_header(self._header, instructions=mutated_editable)
            fresh_payload = EvdScript(encoded).editor_payload()
        except (EvdError, ValueError) as e:
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.warning(f'[gen {self._generation}] {action_desc} REJECTED ({elapsed_ms:.1f}ms): {e}')
            self.show_error(f'{action_desc} failed: {e}')
            return False
        elapsed_ms = (time.monotonic() - t0) * 1000

        self._undo_stack.append(self._editable)
        if len(self._undo_stack) > self._MAX_UNDO:
            self._undo_stack.pop(0)
        self._redo_stack.clear()

        self._generation += 1
        logger.info(
            f'[gen {self._generation}] {action_desc} applied ({elapsed_ms:.1f}ms): '
            f'{len(mutated_editable)} instructions, {len(encoded)} bytes, '
            f'{fresh_payload.info.unreached_byte_count} unreached'
        )
        self._populate_ui(fresh_payload)
        self.set_dirty(True)
        self._set_status(f'{action_desc} applied ({len(mutated_editable)} instructions, {len(encoded)} bytes)', error=False)
        self._emit_undo_state()
        return True

    def _commit_state(self, editable_state: list[MutableInstruction], action_desc: str) -> None:
        '''Used to make edits from the History stacks.'''
        try:
            encoded = EvdScript.encode_with_header(self._header, editable_state)
            fresh_payload = EvdScript(encoded).editor_payload()
        except (EvdError, ValueError) as e:
            logger.error(
                f'[gen {self._generation}] {action_desc} produced an unencodable state -- '
                f'this should not happen, undo/redo history may be corrupt: {e}'
            )
            self.show_error(f'{action_desc} failed unexpectedly: {e}')
            return
        self._generation += 1
        logger.info(f'[gen {self._generation}] {action_desc}: {len(editable_state)} instructions, {len(encoded)} bytes')
        self._populate_ui(fresh_payload)
        self.set_dirty(bool(self._undo_stack))
        self._set_status(f'{action_desc} ({len(editable_state)} instructions)', error=False)

    def _emit_undo_state(self) -> None:
        self.undo_state_changed.emit(bool(self._undo_stack), bool(self._redo_stack))

    def undo(self) -> None:
        if not self._undo_stack:
            return
        self._redo_stack.append(self._editable)
        previous = self._undo_stack.pop()
        self._commit_state(previous, 'Undo')
        self._emit_undo_state()

    def redo(self) -> None:
        if not self._redo_stack:
            return
        self._undo_stack.append(self._editable)
        next_state = self._redo_stack.pop()
        self._commit_state(next_state, 'Redo')
        self._emit_undo_state()

    def _on_instruction_moved(self, moved_id: int, target_index: int) -> None:
        current = [e for e in self._editable if e.id != moved_id]
        try:
            moved = next(e for e in self._editable if e.id == moved_id)
        except StopIteration:
            logger.warning(f'moved instruction id {moved_id} not found in current state')
            return
        target_index = max(0, min(target_index, len(current)))
        current.insert(target_index, moved)
        self._apply_edit(current, 'Reorder')

    def _on_opcode_dropped(self, opcode: int, target_index: int) -> None:
        new_instr = self._build_default_instruction(opcode)
        if new_instr is None:
            return  # user cancelled a required follow-up (e.g. JUMP target picker)
        editable = self._editable[:]
        target_index = max(0, min(target_index, len(editable)))
        editable.insert(target_index, new_instr)
        self._apply_edit(editable, f'Insert {OPCODES.name(opcode)}')

    def _on_delete_requested(self, instr_id: int) -> None:
        editable = [e for e in self._editable if e.id != instr_id]
        if len(editable) == len(self._editable):
            return
        self._apply_edit(editable, 'Delete')

    def _build_default_instruction(self, opcode: int) -> MutableInstruction | None:
        '''The primary limitation for the editor: instruction payloads
        are almost always defaulted / zero payload'''
        new_id = self._next_new_id
        self._next_new_id -= 1
        default_flags = EVD_KEEP_GOING_FLAG  # continue immediately by default, not yield

        if opcode == JUMP_OPCODE:
            target_id = self._pick_jump_target()
            if target_id is None:
                return None
            jump = SymbolicJump(target_id=target_id, raw_target_offset=None, condition_byte=0)
            return MutableInstruction(id=new_id, opcode=opcode, arg=0, flags=default_flags, jump=jump)

        return MutableInstruction(id=new_id, opcode=opcode, arg=0, flags=default_flags, extra_words=0, payload_words=())

    def _pick_jump_target(self) -> int | None:
        if not self._instructions:
            self.show_error('Cannot insert a JUMP: no instructions to target')
            return None
        options = [f'{i.word_offset:06X}  {d.summary}' for i, d in zip(self._instructions, self._descriptions)]
        ids = [i.byte_offset for i in self._instructions]
        choice, ok = QInputDialog.getItem(self, 'Jump target', 'Jump to:', options, 0, False)
        if not ok:
            return None
        return ids[options.index(choice)]


class DebugPanel(QWidget):
    '''Shows exactly what encode() will see: ids, jump targets, and raw payload
    words for everything else.'''
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        header = QLabel('Debug: raw editable state (what encode() will see)')
        header.setStyleSheet('font-weight: bold; color: gray; font-size: 10px;')
        layout.addWidget(header)
        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setObjectName('TextMono')
        self._text.setStyleSheet('font-size: 10px;')
        layout.addWidget(self._text)

    def update_state(self, editable: list[MutableInstruction], generation: int) -> None:
        lines = [f'-- generation {generation}, {len(editable)} instructions --']
        for e in editable:
            if e.opcode == JUMP_OPCODE:
                j = e.jump
                if not j:
                    raise ValueError('Jump instruction has no target.')
                if j.target_id is not None:
                    target = f'target_id={j.target_id}'
                else:
                    target = f'raw_target_offset={j.raw_target_offset:#x} (unresolved -- not reorder-safe)'
                cond = f'cond=0x{j.condition_byte:02X}' if j.condition_byte else 'unconditional'
                lines.append(f'  id={e.id:>8}  JUMP   {target}  {cond}')
            else:
                words = ' '.join(f'{w:08X}' for w in e.payload_words)
                lines.append(
                    f'  id={e.id:>8}  op=0x{e.opcode:02X} {OPCODES.name(e.opcode):<16} '
                    f'arg=0x{e.arg:02X} flags=0x{e.flags:02X} extra_words={e.extra_words}  words=[{words}]'
                )
        self._text.setPlainText('\n'.join(lines))


class SequenceListWidget(QListWidget):
    '''
    The main instruction sequence view. Accepts two kinds of drop:
    reordering an existing row (drag originates from an InstructionRow in
    this same list) and inserting a new instruction (drag originates from
    OpcodeToolbar). Both are reported as signals with a target index
    rather than mutating anything itself.
    '''
    instructionMoved = pyqtSignal(int, int)  # moved_id, target_index
    opcodeDropped = pyqtSignal(int, int)     # opcode, target_index
    deleteRequested = pyqtSignal(int)        # instr_id

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(False)  # drag is initiated manually by InstructionRow, not the view itself
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)

    def current_instruction_id(self) -> int | None:
        item = self.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def select_instruction_id(self, instr_id: int) -> None:
        for i in range(self.count()):
            item = self.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == instr_id:
                self.setCurrentItem(item)
                return

    def dragEnterEvent(self, e) -> None:
        md = e.mimeData()
        if md.hasFormat(_MIME_MOVE_INSTRUCTION) or md.hasFormat(_MIME_NEW_INSTRUCTION):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e) -> None:
        e.acceptProposedAction()

    def dropEvent(self, event) -> None:
        pos = event.position().toPoint() if hasattr(event, 'position') else event.pos()
        target_index = self._drop_target_index(pos)
        md = event.mimeData()
        if md.hasFormat(_MIME_MOVE_INSTRUCTION):
            moved_id = int(bytes(md.data(_MIME_MOVE_INSTRUCTION)).decode())
            self.instructionMoved.emit(moved_id, target_index)
            event.acceptProposedAction()
        elif md.hasFormat(_MIME_NEW_INSTRUCTION):
            opcode = int(bytes(md.data(_MIME_NEW_INSTRUCTION)).decode())
            self.opcodeDropped.emit(opcode, target_index)
            event.acceptProposedAction()
        else:
            event.ignore()

    def _drop_target_index(self, pos: QPoint) -> int:
        item = self.itemAt(pos)
        if item is None:
            return self.count()
        row = self.row(item)
        rect = self.visualItemRect(item)
        if pos.y() > rect.center().y():
            row += 1
        return row

    def keyPressEvent(self, e) -> None:
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            instr_id = self.current_instruction_id()
            if instr_id is not None:
                self.deleteRequested.emit(instr_id)
                return
        super().keyPressEvent(e)

    def contextMenuEvent(self, a0) -> None:
        '''Shows a context menu for the selected instruction.
        In the future this should be the entrypoint for editing instructions.'''
        item = self.itemAt(a0.pos())
        if item is None:
            return
        instr_id = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        delete_action = menu.addAction('Delete instruction')
        chosen = menu.exec(a0.globalPos())
        if chosen is delete_action:
            self.deleteRequested.emit(instr_id)


class OpcodeToolbar(QWidget):
    '''List of OpcodeListWidget items drag-and-drop-able and filterable.'''
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._filter_box = QLineEdit()
        self._filter_box.setPlaceholderText('Filter opcodes...')
        self._filter_box.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter_box)

        self._list = OpcodeListWidget()
        self._all_items: list[tuple[QListWidgetItem, str]] = []  # (item, searchable text)
        for info in OPCODES.all_opcodes():
            label = f'0x{info.opcode:02X}  {info.name}'
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, info.opcode)
            item.setToolTip(info.summary or info.name)
            self._list.addItem(item)
            self._all_items.append((item, label.lower()))
        layout.addWidget(self._list)

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        for item, searchable in self._all_items:
            item.setHidden(bool(text) and text not in searchable)


class OpcodeListWidget(QListWidget):
    '''Plain QListWidget using Qt's own drag initiation.
    Mime data is overridden to emit our custom _MIME_NEW_INSTRUCTION type.'''
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragOnly)

    def mimeData(self, items: list[QListWidgetItem]) -> QMimeData:
        mime = QMimeData()
        if items:
            opcode = items[0].data(Qt.ItemDataRole.UserRole)
            mime.setData(_MIME_NEW_INSTRUCTION, str(opcode).encode())
        return mime


class _LegendWidget(QWidget):
    '''Color legend displayed top right (toolbar)'''
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        for category, label in (
            ('jump', 'Jump'), ('script_start', 'Script start'),
            ('marker_seek', 'Marker seek'), ('calc', 'Expr'),
            ('high', 'System'),
        ):
            dot = QLabel('\u25CF')
            dot.setStyleSheet(f'color: {_CATEGORY_COLORS[category]};')
            text = QLabel(label)
            text.setStyleSheet('color: gray; font-size: 10px;')
            lay.addWidget(dot)
            lay.addWidget(text)


class InstructionRow(QWidget):
    '''
    Early version of what will eventually be the instruction card UI elements.
    One instruction's display row: offset, keep-going marker, a decoded
    one-line summary (colored by category), and dimmed detail text.
    Unreached instructions are shown grayed out and italic rather than
    hidden.

    Draggable: press-and-drag starts a QDrag carrying this instruction's
    id (_MIME_MOVE_INSTRUCTION), picked up by SequenceListWidget.dropEvent.
    '''
    def __init__(self, instr: EvdInstruction, desc: InstructionDescription, reached: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._instr_id = instr.byte_offset
        self._drag_start_pos: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(10)

        offset_label = QLabel(f'{instr.word_offset:06X}')
        offset_label.setFixedWidth(56)
        offset_label.setStyleSheet('color: #808080;')
        layout.addWidget(offset_label)

        kg_label = QLabel('\u2192' if instr.keep_going else '\u2551')  # -> continues immediately, || yields
        kg_label.setFixedWidth(14)
        kg_label.setToolTip('Keep-going: runs the next command immediately' if instr.keep_going
                             else 'Yields: resumes on the next game update')
        layout.addWidget(kg_label)

        summary_label = QLabel(desc.summary)
        summary_label.setMinimumWidth(160)
        color = _UNREACHED_COLOR if not reached else _CATEGORY_COLORS.get(desc.category, _CATEGORY_COLORS['normal'])
        weight = 'normal' if not reached else 'bold'
        style_extra = 'font-style: italic;' if not reached else ''
        summary_label.setStyleSheet(f'color: {color}; font-weight: {weight}; {style_extra}')
        layout.addWidget(summary_label)

        if desc.details:
            detail_label = QLabel('  \u2022  '.join(desc.details))
            detail_label.setStyleSheet(f'color: {"#B0B0B0" if not reached else "#707070"}; font-style: italic;')
            detail_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            layout.addWidget(detail_label, stretch=1)
        else:
            layout.addStretch(1)

        if not reached:
            unreached_tag = QLabel('unreached')
            unreached_tag.setStyleSheet(f'color: {_UNREACHED_COLOR}; font-size: 9px;')
            unreached_tag.setToolTip(
                'Not proven reachable from the command region entry point (0x0C). '
                'Could be another numbered sub-script, or genuinely dead data -- shown, not hidden.'
            )
            layout.addWidget(unreached_tag)

        raw_hex = instr.payload.hex(' ').upper()
        self.setToolTip(f'opcode 0x{instr.opcode:02X}  arg 0x{instr.arg:02X}  flags 0x{instr.flags:02X}\npayload: {raw_hex}')

    def mousePressEvent(self, a0) -> None:
        if a0.button() == Qt.MouseButton.LeftButton:
            self._drag_start_pos = a0.position().toPoint() if hasattr(a0, 'position') else a0.pos()
        super().mousePressEvent(a0)

    def mouseMoveEvent(self, a0) -> None:
        if self._drag_start_pos is None or not (a0.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(a0)
            return
        pos = a0.position().toPoint() if hasattr(a0, 'position') else a0.pos()
        if (pos - self._drag_start_pos).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(a0)
            return

        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(_MIME_MOVE_INSTRUCTION, str(self._instr_id).encode())
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)
        self._drag_start_pos = None
