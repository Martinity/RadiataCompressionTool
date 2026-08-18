"""EVD script handling: the EVDCODE line model, parameter edits, and the handler.

Every fixture is built by compiling EVDCODE in memory rather than reading a game
file, so the suite is self-contained and still exercises the real assembler.
"""
import re

import pytest

from core.evd import api
from core.evd.api import Arg
from core.handlers.evd_leaf import EVDHandler, EvdSavePayload, EvdError
from core.node import VfsNode

SCRIPT = """event Main {
    header(0x00000003)
    headerExtra(0x00000000)
    entry(loc_000C)
    label(loc_000C)
    set_flag(flag=0)
    play_character_sub_animation(handler_mode=0, action=virtual_anim_sub_control, character=9900, character_number=9900, character_type=0x00)
    if_value(event_value=1009, not=0) {
        set_value(event_value=1009, value=0)
    }
    clear_flag(flag=1)
    end_script(yield=1)
}
"""


@pytest.fixture(scope="module")
def script_bytes():
    return api.compile_code(SCRIPT)


@pytest.fixture(scope="module")
def code(script_bytes):
    return api.decompile_code(script_bytes, "Main")


@pytest.fixture
def lines(code):
    return api.parse_code(code)


def line_with_head(lines, head):
    return next(line for line in lines if line.head == head)


###--------------------------------------------- Line model ---------------------------------------------###

def test_parse_assigns_one_line_per_text_line(code, lines):
    assert len(lines) == len(code.splitlines())
    assert [line.number for line in lines] == list(range(1, len(lines) + 1))


def test_kinds_and_depths(lines):
    kinds = {line.number: line.kind for line in lines}
    assert kinds[1] == api.KIND_EVENT
    assert kinds[2] == api.KIND_DIRECTIVE
    assert kinds[5] == api.KIND_LABEL
    assert kinds[6] == api.KIND_COMMAND
    assert lines[0].depth == 0
    assert lines[5].depth == 1
    body = line_with_head(lines, "set_value")
    assert body.depth == 2


def test_block_opener_knows_its_closing_brace(lines):
    block = line_with_head(lines, "if_value")
    assert block.opens
    assert block.block_end is not None
    assert lines[block.block_end - 1].kind == api.KIND_CLOSE
    assert api.block_range(lines, block.number) == (block.number, block.block_end)


def test_plain_line_owns_only_itself(lines):
    plain = line_with_head(lines, "set_flag")
    assert api.block_range(lines, plain.number) == (plain.number, plain.number)


def test_args_are_split_and_keyed(lines):
    line = line_with_head(lines, "if_value")
    assert line.arg("event_value") == "1009"
    assert line.arg("not") == "0"
    assert line.arg("missing") is None


def test_category_follows_the_opcode(lines):
    assert line_with_head(lines, "if_value").category == api.CATEGORY_JUMP
    assert line_with_head(lines, "set_value").category == api.CATEGORY_EXPRESSION
    assert line_with_head(lines, "label").category == api.CATEGORY_STRUCTURE


def test_symbol_annotation_is_a_comment_not_an_argument(lines):
    line = line_with_head(lines, "set_flag")
    assert line.comment.startswith("//")
    assert [a.key for a in line.args] == ["flag"]


def test_double_slash_inside_a_string_is_not_a_comment():
    line = api.parse_line('    print_text(text="a//b")  // real note', 1)
    assert line.arg("text") == '"a//b"'
    assert line.comment == "// real note"


def test_render_round_trips_unmodified_lines(code, lines):
    assert api.render_code(lines) == code


def test_labels_and_references(lines):
    assert dict(api.iter_labels(lines)) == {"loc_000C": 5}
    assert api.label_references(lines).get("loc_000C") == 1  # the entry directive


###------------------------------------------- Compile errors -------------------------------------------###

def test_validate_accepts_the_fixture(code):
    assert api.validate_code(code) is None


def test_validate_reports_the_authors_line(code):
    broken = code.replace("if_value(", "if_valve(")
    error = api.validate_code(broken)
    assert error is not None
    assert error.line == 8


def test_an_unlocated_rejection_is_still_pinned_to_the_edited_line(lines):
    """Some rejections come out of a bare conversion and carry no line. The edit
    still knows which line it touched, and that is what the editor shows."""
    line = line_with_head(lines, "set_flag")
    result = api.apply_line_edit(lines, line.number, (Arg("flag", "banana"),))
    assert not result.ok
    assert result.error is not None and result.error.line is None
    assert result.error_text.startswith(f"line {line.number}: ")


def test_compile_error_names_the_conflicting_derived_field():
    error = api.EvdCompileError("character_sub_anim character_number does not match character", 7)
    assert error.conflicting_field == "character_number"
    assert api.EvdCompileError("something else entirely").conflicting_field is None


###------------------------------------------ Parameter edits -------------------------------------------###

def test_editing_an_input_rewrites_the_line(lines):
    line = line_with_head(lines, "clear_flag")
    result = api.apply_line_edit(lines, line.number, (Arg("flag", "42"),))
    assert result.ok
    assert result.changed_line.strip() == "clear_flag(flag=42)"
    assert result.dropped == ()


def test_editing_an_input_drops_the_derived_field_it_invalidates(lines):
    """`character_number` is derived from `character`; leaving it stale is an
    error rather than a silent no-op, so the edit has to drop it."""
    line = line_with_head(lines, "control_character_sub_animation")
    edited = tuple(Arg("character", "1") if a.key == "character" else a for a in line.args)
    result = api.apply_line_edit(lines, line.number, edited)
    assert result.ok
    assert result.dropped == ("character_number",)
    assert "character=1" in result.changed_line
    assert "character_number=" not in result.changed_line
    assert api.validate_code(result.text) is None


def test_a_rejected_edit_returns_the_error_and_no_text(lines):
    line = line_with_head(lines, "set_flag")
    result = api.apply_line_edit(lines, line.number, (Arg("flag", "banana"),))
    assert not result.ok
    assert result.text == ""
    assert result.error is not None


def test_editing_out_of_range_is_rejected(lines):
    assert not api.apply_line_edit(lines, 9999, ()).ok


def test_stale_name_annotations_are_dropped_on_edit(lines):
    """The comment names the id; keeping it after the id changes would label the
    new value with the old name."""
    line = line_with_head(lines, "set_flag")
    assert "Movement Lock" in line.comment
    result = api.apply_line_edit(lines, line.number, (Arg("flag", "4400"),))
    assert result.ok
    assert "Movement Lock" not in result.changed_line


def test_hand_written_comments_survive_an_edit():
    kept = api.prune_annotation("// my own note", {"flag"})
    assert kept == "// my own note"
    pruned = api.prune_annotation("// flag=Movement Lock, item=Sword", {"flag"})
    assert pruned == "// item=Sword"
    assert api.prune_annotation("// flag=Movement Lock", {"flag"}) == ""


###-------------------------------------------- Command index -------------------------------------------###

def test_index_loaded():
    assert api.COMMANDS
    assert "set_flag" in api.COMMANDS


def test_parameter_roles_are_available():
    info = api.COMMANDS.get("control_character_sub_animation")
    assert info is not None
    assert info.role_of("character") == "input"
    assert info.role_of("character_number") == "derived"
    assert all(p.meaning for p in info.parameters)


def test_aliases_resolve_to_the_printed_spelling():
    assert api.COMMANDS.canonical("change_time") == "set_game_clock"
    assert api.COMMANDS.get("change_time") is api.COMMANDS.get("set_game_clock")


def test_every_documented_example_compiles_as_an_evdcode_call():
    """The palette inserts a command's example, so an example that does not
    assemble would insert a line the editor immediately rejects."""
    failures = []
    for name in api.COMMANDS.command_names:
        info = api.COMMANDS.get(name)
        if info is None or not info.example:
            continue
        call = api.example_to_call(info.example, indent=4)
        text = f"event Main {{\n    header(0x00000003)\n    entry(loc)\n    label(loc)\n{call}\n    end_script(yield=1)\n}}\n"
        if api.validate_code(text) is not None:
            failures.append(name)
    assert not failures, f"examples that do not assemble: {failures}"


###------------------------------------------- Vendored module ------------------------------------------###

def test_the_vendored_tool_carries_no_cli_or_container_code():
    """The app is handed raw EVD payloads and drives everything through `api`,
    so the CLI, the SLZ container walk and the analysis commands are stripped."""
    from core.evd import evd_tool
    for name in ("main", "build_parser", "SLZ_MAGIC", "container_summary",
                 "analyze_command_handlers", "cmd_decompile_code"):
        assert not hasattr(evd_tool, name), f"{name} should have been stripped"


def test_the_vendored_tool_keeps_the_tables_its_output_depends_on():
    """The strip is a reachability walk, and the tables that name forms and
    parameters are filled in by statements that bind nothing -- `TABLE.update()`
    and `TABLE[key][key] = ...`. Dropping those leaves a module that assembles
    identical bytes and prints different names, so they are pinned here."""
    from core.evd import evd_tool
    assert evd_tool.PARAMETER_ALIASES["trigger"]["character_word"] == "character"
    assert evd_tool.PARAMETER_ALIASES["window_message"]["message_id"] == "window"
    assert evd_tool.FORM_NAME_ALIASES.get("change_time") == "set_radiata_time"
    assert len(evd_tool.FORM_NAME_ALIASES) > 100


###------------------------------------------ configure_battle ------------------------------------------###

BATTLE = ("event Main {\n"
          "    header(0x00000003)\n"
          "    entry(loc_000C)\n"
          "    label(loc_000C)\n"
          "    configure_battle(battle_map=366, battle_bgm=28, battle_script=1, "
          "battle_event_file=500, battle_event_script=3)\n"
          "    end_script(yield=1)\n}\n")


def test_the_battle_event_pair_names_a_file_and_a_script():
    """Traced from `CRadiScript::Command_16`: the word's low half goes to
    app+0x84C, which the battle path passes to `CRadiScript::LoadScriptFile`,
    and its high half (masked 0x7FFF) to app+0x84E, which is handed to
    `CRadiScript::SetScriptNumber` as the script selector. Together they name
    one script the way the files do -- file 500, script 3, i.e. 500_03."""
    info = api.COMMANDS.get("configure_battle")
    assert info is not None
    assert info.role_of("battle_event_file") == "input"
    assert info.role_of("battle_event_script") == "input"
    assert "LoadScriptFile" in info.meaning_of("battle_event_file")
    assert "SetScriptNumber" in info.meaning_of("battle_event_script")
    assert api.validate_code(BATTLE) is None


def test_the_pair_packs_into_one_word_high_half_first():
    data = api.compile_code(BATTLE)
    code = api.decompile_code(data, "Main")
    line = next(l for l in api.parse_code(code) if l.head == "configure_battle")
    assert api.parse_number(line.arg("battle_event_file")) == 500
    assert api.parse_number(line.arg("battle_event_script")) == 3


def test_the_superseded_spellings_still_compile():
    """`suppress_map_init` and `unused_84e` were the names before the pair was
    traced. Scripts written with them have to keep assembling identically."""
    old = BATTLE.replace("battle_event_file", "suppress_map_init") \
                .replace("battle_event_script", "unused_84e")
    assert api.compile_code(old) == api.compile_code(BATTLE)


def test_the_file_id_reads_as_an_event_name():
    """It is an event file id, so it resolves through the event symbol table
    the same way `battle_script` does."""
    assert api.evd_tool.PARAMETER_SYMBOL_DOMAINS.get("battle_event_file") == "event"


def test_the_2824_globals_stay_unnamed():
    """Checked alongside the pair: a scan of every module finds exactly one
    access to each of 0x3B2824 and 0x3B2826 -- this handler's own store, and no
    read anywhere. They keep their `unused_` names."""
    info = api.COMMANDS.get("configure_battle")
    assert info is not None
    assert "never read" in info.meaning_of("unused_2824")
    assert "never read" in info.meaning_of("unused_2826")


###--------------------------------- Audit item 2: every installed handler ------------------------------###

def test_the_index_publishes_every_installed_opcode():
    """The dispatch table installs 138 handlers. One the corpus never exercised
    is still one a script may legally use; omitting it from the index reads as
    "no such command" rather than "no structured form yet"."""
    from core.evd import evd_tool
    published = {info.opcode for info in
                 (api.COMMANDS.get(n) for n in api.COMMANDS.command_names)
                 if info is not None and info.opcode is not None}
    assert set(evd_tool.OPCODE_NOTES) - published == set()


def test_every_published_command_has_a_readable_summary():
    """Opcode coverage without a summary is a row that reads as undocumented.
    The handler evidence is register-speak, so it does not count as one."""
    blank = [n for n in api.COMMANDS.command_names
             if not (api.COMMANDS.get(n).summary or "").strip()]
    assert not blank, f"commands with no summary: {blank}"
    raw_trace = [n for n in api.COMMANDS.command_names
                 if re.match(r"^Command_[0-9a-f]{2}\b", api.COMMANDS.get(n).summary or "")]
    assert not raw_trace, f"summaries that are the raw trace: {raw_trace}"


def test_every_published_parameter_has_a_description():
    """The editor greys a parameter whose meaning is only a name template. A
    parameter with no meaning at all shows that warning against nothing."""
    blank = [f"{name}.{p.name}" for name in api.COMMANDS.command_names
             for p in api.COMMANDS.get(name).parameters if not (p.meaning or "").strip()]
    assert not blank, f"parameters shipped with no description: {blank}"


def test_raw_examples_are_short_enough_to_read():
    """A raw escape's corpus specimen is kilobytes of payload words. The palette
    inserts the example, so an untrimmed one pastes a wall of hex."""
    oversized = [name for name in api.COMMANDS.command_names
                 if api.COMMANDS.get(name).raw and len(api.COMMANDS.get(name).example) > 500]
    assert not oversized, f"raw examples that were not trimmed: {oversized}"


def test_every_published_head_is_one_the_compiler_accepts():
    """A fallback entry named after the dispatch-table handler can advertise a
    command that does not compile -- `nop_ff` is the handler for 0xFF, but
    `return_zero` is what an author writes."""
    unwritable = [n for n in api.COMMANDS.command_names
                  if not api.is_writable_head(n)]
    assert unwritable == [], f"published but not writable: {unwritable}"


def test_handlers_without_a_form_still_carry_their_evidence():
    """They have no structured form, so what is known about them is the raw
    escape plus the handler note. Both have to survive into the index."""
    from core.evd import evd_tool
    formless = [api.COMMANDS.get(n) for n in api.COMMANDS.command_names]
    raw_with_evidence = [i for i in formless if i and i.raw and i.evidence]
    assert len(raw_with_evidence) > 20
    for info in raw_with_evidence[:5]:
        assert info.opcode is None or info.opcode in evd_tool.OPCODE_NOTES


###------------------------------- Audit item 3: parameter evidence levels ------------------------------###

def test_every_parameter_declares_where_its_meaning_came_from():
    levels = {"traced", "glossary", "template", "untraced", "none"}
    seen = set()
    for name in api.COMMANDS.command_names:
        info = api.COMMANDS.get(name)
        for param in info.parameters:
            assert param.evidence in levels, (name, param.name, param.evidence)
            seen.add(param.evidence)
    assert {"traced", "glossary", "template"} <= seen


def test_a_traced_meaning_is_told_apart_from_a_generated_one():
    """The audit's core complaint: a sentence generated from a parameter's name
    was being read as an established semantic. Now it says which it is."""
    vibration = api.COMMANDS.get("play_vibration")
    assert vibration.evidence_of("motor_flag") == "traced"
    assert not [p for p in vibration.parameters
                if p.name == "motor_flag" and not p.is_traced]
    # something only a name template describes
    generated = [(n, p.name) for n in api.COMMANDS.command_names
                 for p in api.COMMANDS.get(n).parameters if p.evidence == "template"]
    assert generated, "the template-worded parameters should still be identifiable"


###----------------------------- Audit item 4: multi-operation form names -------------------------------###

def test_multi_operation_handlers_use_neutral_names():
    """A handler that hides, creates and erases should not be called `show_`.
    The narrow verb stays valid input; it just is not the published name."""
    for old, new in (
        ("show_portrait", "control_portrait"),
        ("set_character_render", "control_character_render"),
        ("play_character_sub_animation", "control_character_sub_animation"),
        ("animate_camera_color", "control_camera_animation"),
        ("select_camera", "control_camera_select"),
        ("play_special_effect", "control_special_effect"),
    ):
        assert new in api.COMMANDS, new
        assert api.COMMANDS.canonical(old) == new, f"{old} should resolve to {new}"
        assert api.COMMANDS.get(old) is api.COMMANDS.get(new)


def test_the_neutral_summaries_say_what_else_the_handler_does():
    portrait = api.COMMANDS.get("control_portrait")
    assert "erases" in portrait.summary
    camera = api.COMMANDS.get("control_camera_animation")
    assert "fog" in camera.summary


def test_the_narrow_verbs_still_compile():
    head = "event Main {\n    header(0x00000003)\n    entry(loc_000C)\n    label(loc_000C)\n"
    tail = "    end_script(yield=1)\n}\n"
    old = "show_portrait(character=default, portrait_slot=0, has_erase_duration=0, portrait_variant=0, action=0)"
    new = old.replace("show_portrait", "control_portrait")
    assert api.compile_code(f"{head}    {old}\n{tail}") == api.compile_code(f"{head}    {new}\n{tail}")


###------------------------------------ Audited parameter corrections -----------------------------------###
# From the 2026-08-17 command audit, each re-confirmed against the debug build
# before it was applied. Pinned because the meanings live in a table that is
# merged into after a dict literal -- the first attempt at these was silently
# lost to a duplicate key, and nothing failed.

def test_show_portrait_names_the_action_it_performs():
    """`(arg >> 6)` is what `BustupDisp` branches on: 0 hide, 1 create, 2 erase.
    It was called `upper_arg`, as if uninterpreted."""
    info = api.COMMANDS.get("control_portrait")
    assert info is not None
    assert info.role_of("action") == "input"
    assert info.role_of("upper_arg") is None, "the old name should be gone from the index"
    assert "BustupDisp" in info.meaning_of("action")


def test_show_portrait_slot_and_variant_are_distinguished():
    """`(arg >> 1) & 1` indexes the pointer array at controller+0x3C+slot*4;
    `(arg >> 4) & 3` is a presentation variant. Both were `flag1`/`mode`."""
    info = api.COMMANDS.get("control_portrait")
    assert "0x3C" in info.meaning_of("portrait_slot")
    assert info.role_of("portrait_variant") == "input"


def test_show_portrait_float_is_the_erase_duration():
    """Consumed only by action 2, as EraseStart(2.0, value), defaulting to 5.0."""
    info = api.COMMANDS.get("control_portrait")
    meaning = info.meaning_of("erase_duration")
    assert "EraseStart" in meaning and "5.0" in meaning


def test_play_vibration_middle_byte_is_one_bit_not_a_pattern():
    """`CVibPlayer::PlayVibration` masks it with `& 1`; there is no pattern
    table, and seven of the eight bits are discarded."""
    info = api.COMMANDS.get("play_vibration")
    assert info.role_of("motor_flag") == "input"
    assert info.role_of("pattern") is None
    assert "& 1" in info.meaning_of("motor_flag")


def test_set_game_clock_day_is_one_based_with_two_sentinels():
    """The handler passes `value - 1`; 0 skips the call and 0xFFFF derives it."""
    info = api.COMMANDS.get("set_game_clock")
    meaning = info.meaning_of("day_1based")
    assert "value - 1" in meaning
    assert "0xFFFF" in meaning and "0 makes no day call" in meaning


def test_packed_time_is_a_tuple_not_a_duration():
    """Five components of 6/6/6/5/9 bits, all-ones meaning leave unchanged."""
    info = api.COMMANDS.get("set_time_schedule")
    meaning = info.meaning_of("packed_time")
    assert "not a duration" in meaning
    assert "6, 6, 6, 5" in meaning
    assert "0x1FF" in info.meaning_of("part4")
    assert "range check" in info.meaning_of("range_check")


def test_duration_help_does_not_point_at_a_field_that_is_absent():
    """The shared glossary line tells the author to edit `duration_word`. On
    these forms `duration` is the input itself and no `duration_word` exists."""
    for name in ("play_vibration", "fade_screen", "move_camera", "set_camera_transform"):
        info = api.COMMANDS.get(name)
        assert info is not None, name
        published = {p.name for p in info.parameters}
        if "duration" in published and "duration_word" not in published:
            assert "duration_word" not in info.meaning_of("duration"), name


def test_the_audited_renames_still_accept_the_old_spellings():
    head = ("event Main {\n    header(0x00000003)\n    entry(loc_000C)\n    label(loc_000C)\n")
    tail = "    end_script(yield=1)\n}\n"
    for old, new in (
        ("show_portrait(character=default, flag1=0, stream_float=0, mode=0, upper_arg=0)",
         "show_portrait(character=default, portrait_slot=0, has_erase_duration=0, "
         "portrait_variant=0, action=0)"),
        ("play_vibration(strength=0xC8, pattern=0x00, duration=0x000A)",
         "play_vibration(strength=0xC8, motor_flag=0x00, duration=0x000A)"),
        ("set_game_clock(minute=0, hour=12, day=0)",
         "set_game_clock(minute=0, hour=12, day_1based=0)"),
    ):
        assert api.compile_code(f"{head}    {old}\n{tail}") == \
               api.compile_code(f"{head}    {new}\n{tail}"), old


def test_the_symbol_typo_is_corrected():
    """`Ridley vists Jack` -- in three entries, not the two the audit lists."""
    events = api.SYMBOLS._tables["event"]
    assert not [v for v in events.values() if "vists" in v]
    assert sum(1 for v in events.values() if "Ridley visits Jack" in v) == 3


###-------------------------------------------- Line addresses ------------------------------------------###

def test_assemble_reports_an_offset_for_every_line(script_bytes, code, lines):
    built = api.assemble(code)
    assert built.ok and built.data == script_bytes
    assert set(built.offsets) == {line.number for line in lines}


def test_the_listing_starts_at_zero_and_ends_at_the_file_size(script_bytes, code, lines):
    """The header is bytes too: the magic at +0x00 and the two header words at
    +0x04 and +0x08. Without them the column would begin at the first command,
    partway into a file that starts at 0000."""
    offsets = api.assemble(code).offsets
    by_head = {line.head: offsets[line.number] for line in lines if line.head}
    assert by_head["event"] == 0x00
    assert by_head["header"] == 0x04
    assert by_head["headerExtra"] == 0x08
    assert offsets[lines[-1].number] == len(script_bytes)


def test_offsets_are_ready_before_any_edit(script_bytes):
    """The address column is drawn from the payload, so it has to be filled in
    on load rather than on the first edit."""
    handler = EVDHandler(script_bytes)
    payload = handler.prepare_editor_data(VfsNode(name="test.evd"), script_bytes)
    assert payload.offsets
    assert set(payload.offsets) == {line.number for line in payload.lines}


def test_marker_table_is_a_directive_at_the_address_the_header_points_to():
    """The decompiler prints the `.marker_table` directive as `markerTable`, and
    the table it emits lives inside the command region at header_extra * 4."""
    data = api.compile_code(
        "event Main {\n"
        "    header(0x00000003)\n"
        "    entry(start)\n"
        "    label(start)\n"
        "    set_flag(flag=0)\n"
        "    end_script(yield=1)\n"
        "    markerTable(start)\n"
        "}\n"
    )
    code = api.decompile_code(data, "Main")
    lines = api.parse_code(code)
    table = next(line for line in lines if line.head in ("markerTable", "marker_table"))
    assert table.kind == api.KIND_DIRECTIVE
    assert api.assemble(code).offsets[table.number] == api.evd_tool.u32(data, 0x08) * 4


def test_every_label_sits_at_the_offset_its_name_says(code, lines):
    """A label is named after the byte it sits at, so the decompiler's own names
    are the ground truth for the offsets computed here."""
    offsets = api.assemble(code).offsets
    labels = [line for line in lines if line.kind == api.KIND_LABEL]
    assert labels
    for line in labels:
        assert api.label_for_offset(offsets[line.number]) == line.args[0].value


def test_commands_advance_by_their_own_size(code, lines):
    offsets = api.assemble(code).offsets
    commands = [line for line in lines if line.kind == api.KIND_COMMAND]
    for earlier, later in zip(commands, commands[1:]):
        step = offsets[later.number] - offsets[earlier.number]
        assert step >= 4 and step % 4 == 0


def test_a_label_takes_the_address_of_what_follows_it(code, lines):
    offsets = api.assemble(code).offsets
    label = next(line for line in lines if line.kind == api.KIND_LABEL)
    following = next(line for line in lines
                     if line.number > label.number and line.kind == api.KIND_COMMAND)
    assert offsets[label.number] == offsets[following.number]


def test_addresses_move_when_the_script_does():
    """The point of the column: inserting a command shifts everything after it,
    the label names stay as they were, and only the address still says where
    the target actually is."""
    # Round-tripped first, so the labels carry the names the decompiler gives
    # them -- which is what makes a name comparable to an address at all.
    original = api.decompile_code(api.compile_code(
        "event Main {\n"
        "    header(0x00000003)\n"
        "    entry(start)\n"
        "    label(start)\n"
        "    set_flag(flag=0)\n"
        "    jump(goto=tail)\n"
        "    label(tail)\n"
        "    end_script(yield=1)\n"
        "}\n"
    ), "Main")
    target = [line for line in api.parse_code(original) if line.kind == api.KIND_LABEL][-1]
    name = target.args[0].value
    before = api.assemble(original).offsets[target.number]
    assert api.label_for_offset(before) == name, "the name matches the address before the edit"

    inserted = original.replace("    set_flag(flag=0)", "    set_flag(flag=0)\n    clear_flag(flag=1)", 1)
    moved = next(line for line in api.parse_code(inserted)
                 if line.kind == api.KIND_LABEL and line.args[0].value == name)
    after = api.assemble(inserted).offsets[moved.number]
    assert after > before, "an inserted command must move what follows it"
    assert api.label_for_offset(after) != name, "the name is stale, the address is live"


def test_assemble_reports_errors_without_offsets():
    built = api.assemble("event Main {\n    not_a_command()\n}\n")
    assert not built.ok
    assert built.data is None and built.offsets == {}


###----------------------------------------- Stranded jump targets -------------------------------------###

def test_a_clean_script_reports_no_stranded_targets(lines):
    assert api.undefined_label_problems(lines) == []


def test_deleting_a_label_strands_the_branch_that_used_it(code):
    """The compiler accepts this: `loc_0114` with no such label parses as the raw
    byte offset 0x0114, so the branch silently lands somewhere else. Moving and
    deleting lines is most of what the editor does, so it has to be caught here."""
    branching = code.replace(
        "    if_value(event_value=1009, not=0) {\n        set_value(event_value=1009, value=0)\n    }\n",
        "    if_value(event_value=1009, not=0, goto=loc_0114)\n",
    )
    assert branching != code
    assert api.validate_code(branching) is None, "the compiler accepts it, which is the point"
    problems = api.undefined_label_problems(api.parse_code(branching))
    assert len(problems) == 1
    assert "loc_0114" in problems[0].message
    assert "0x0114" in problems[0].message, "say what it will actually assemble to"


def test_target_is_only_a_label_on_a_branch():
    """`set_sound_listener(target=camera)` names an enum, not a jump target."""
    lines = api.parse_code(
        'event Main {\n    set_sound_listener(target=camera, mode=0)\n}\n'
    )
    assert api.undefined_label_problems(lines) == []


def test_a_numeric_target_is_a_deliberate_raw_offset():
    lines = api.parse_code('event Main {\n    jump(goto=0x0010)\n}\n')
    assert api.undefined_label_problems(lines) == []


###----------------------------------------- Value occurrences ------------------------------------------###

def test_numbers_compare_numerically_however_they_are_written():
    """The decompiler prints flags in decimal and masks in hex. Tracing a value
    should not depend on which spelling a line happened to get."""
    assert api.value_key("1009") == api.value_key("0x3F1")
    assert api.value_key("1009") != api.value_key("1010")
    assert api.value_key('"text"') == '="text"'
    assert api.value_key("event_value") is None, "a bare name is not a value"
    assert api.value_key("") is None


def test_occurrences_are_values_not_names(lines):
    key = api.value_key("1009")
    line = api.parse_line("    set_value(event_value=1009, value=1009)  // note 1009", 1)
    spans = api.value_occurrences(line, key)
    assert [line.text[a:b] for a, b in spans] == ["1009", "1009"]
    # the trailing comment is not code, so its 1009 is not an occurrence
    assert all(b <= line.text.index("//") for _a, b in spans)


def test_a_label_that_reads_like_a_value_is_not_one():
    line = api.parse_line("    label(loc_1009)", 1)
    assert api.value_occurrences(line, api.value_key("1009")) == []


def test_a_parameter_name_is_never_an_occurrence():
    """`flag=1` has a value 1; `1=flag` would be nonsense, but a name that
    parses as a number must not light up as one."""
    line = api.parse_line("    set_flags(first_flag=4740, flag_count=2)", 1)
    spans = api.value_occurrences(line, api.value_key("2"))
    assert [line.text[a:b] for a, b in spans] == ["2"]


def test_occurrences_span_the_whole_script(code, lines):
    key = api.value_key("1009")
    hits = [line.number for line in lines if api.value_occurrences(line, key)]
    assert len(hits) >= 2, "the fixture uses 1009 on several lines"
    for number in hits:
        assert "1009" in lines[number - 1].text


def test_token_at_finds_the_word_under_a_column():
    text = "    set_value(event_value=1009, value=0)"
    assert api.token_at(text, text.index("1009") + 1)[0] == "1009"
    assert api.token_at(text, text.index("event_value") + 2)[0] == "event_value"
    assert api.token_at(text, 0) is None


def test_hex_written_values_are_found_by_a_decimal_key():
    line = api.parse_line("    set_value(event_value=0x3F1)", 1)
    assert api.value_occurrences(line, api.value_key("1009"))


###--------------------------------------------- Folding ------------------------------------------------###

def test_foldable_blocks_are_the_ones_with_a_body(lines):
    spans = api.foldable(lines)
    block = line_with_head(lines, "if_value")
    assert spans[block.number] == block.block_end
    assert 1 in spans, "the event block wraps the script and folds like any other"
    # a plain command opens nothing
    assert line_with_head(lines, "set_flag").number not in spans


def test_folding_hides_the_body_and_the_closing_brace(lines):
    block = line_with_head(lines, "if_value")
    hidden = api.hidden_lines(lines, {block.number})
    assert hidden == set(range(block.number + 1, block.block_end + 1))
    assert block.number not in hidden, "the opener stays visible"


def test_a_fold_inside_a_fold_adds_nothing():
    """Nested folds are already hidden by the outer one, so unfolding the outer
    block restores whatever state the inner ones were left in."""
    nested = api.parse_code(
        "event Main {\n"
        "    header(0x00000003)\n"
        "    entry(loc_000C)\n"
        "    label(loc_000C)\n"
        "    if_value(event_value=1009, not=0) {\n"
        "        if_value(event_value=1008, not=0) {\n"
        "            set_flag(flag=1)\n"
        "        }\n"
        "    }\n"
        "    end_script(yield=1)\n"
        "}\n"
    )
    outer, inner = [line.number for line in nested if line.head == "if_value"]
    assert api.foldable(nested)[outer] > api.foldable(nested)[inner]
    assert (api.hidden_lines(nested, {outer, inner})
            == api.hidden_lines(nested, {outer}))


def test_folding_never_touches_the_script(code, lines, script_bytes):
    """Folding is presentation. Both views hide rows; neither edits the text."""
    for folded in ({}, {1}, set(api.foldable(lines))):
        assert api.render_code(lines) == code
        assert api.compile_code(code) == script_bytes


def test_fold_summary_counts_the_hidden_lines(lines):
    block = line_with_head(lines, "if_value")
    summary = api.fold_summary(lines, block.number)
    assert summary.startswith(" ... }")
    assert str(block.block_end - block.number - 1) in summary


###------------------------------------------ Packed operands -------------------------------------------###

def test_character_splits_into_an_id_and_a_variant():
    specs = api.packed_spec(api.COMMANDS.get("play_character_animation"), "character")
    assert specs is not None
    assert api.split_packed(0x000326AC, specs) == {"character_number": 0x26AC, "character_variant": 0x03}


def test_the_split_uses_type_where_the_command_reads_a_type():
    """Byte 2 is a variant on most commands and a type selector on a few. The
    command's own parameters say which, and the two must not be conflated."""
    typed = api.packed_spec(api.COMMANDS.get("control_character_sub_animation"), "character")
    plain = api.packed_spec(api.COMMANDS.get("play_character_animation"), "character")
    assert [name for name, _s, _m in typed] == ["character_number", "character_type"]
    assert [name for name, _s, _m in plain] == ["character_number", "character_variant"]


def test_composing_only_touches_the_named_fields():
    specs = api.packed_spec(None, "character")
    assert api.compose_packed(0xAB0326AC, {"character_variant": 0x07}, specs) == 0xAB0726AC
    assert api.compose_packed(0xAB0326AC, {"character_number": 1}, specs) == 0xAB030001
    # the top byte is not in any spec and no handler has been shown to read it,
    # so an edit that never named it must leave it alone
    assert api.compose_packed(0xAB0326AC, {}, specs) == 0xAB0326AC


def test_a_plain_field_has_no_parts():
    assert api.packed_spec(api.COMMANDS.get("set_flag"), "flag") is None


def test_writing_the_parts_instead_of_the_word_silently_drops_the_character():
    """Why the editor recomposes the packed word instead of writing the halves.

    `character=` also carries the implicit `explicit_char=1`. Spelling the line
    with `character_number=` instead loses it, and the command compiles to
    "act on the script's default character" -- different bytes, no error. If
    this test ever starts failing because the parts became a real input form,
    the inspector could write them directly; until then it must not."""
    script = ("event Main {\n    header(0x00000003)\n    entry(loc_000C)\n    label(loc_000C)\n"
              "    play_character_animation(character=9900, animation_group=0x00000001, animation=0x1)\n"
              "    end_script(yield=1)\n}\n")
    parts = script.replace("character=9900", "character_number=9900")
    assert api.validate_code(parts) is None, "it compiles, which is what makes it dangerous"
    assert api.compile_code(parts) != api.compile_code(script)
    back = api.decompile_code(api.compile_code(parts), "Main")
    assert "character" not in back.split("play_character_animation")[1].split("\n")[0]


###------------------------------------------ Named id operands -----------------------------------------###

def test_id_fields_offer_the_domain_the_decompiler_names_them_from():
    """The picker appears exactly where the decompiler would append a `// name`
    comment, because both read the same field-to-domain map."""
    animation = api.COMMANDS.get("play_character_animation")
    assert api.symbol_domain(animation, "character_number") == "character"
    assert api.symbol_domain(api.COMMANDS.get("change_inventory"), "item_id") == "item"
    assert api.symbol_domain(api.COMMANDS.get("configure_battle"), "battle_bgm") == "bgm"
    assert api.symbol_domain(api.COMMANDS.get("configure_battle"), "battle_map") == "location"


def test_a_per_command_override_is_honoured():
    """`id` means a location on the background commands and nothing in general."""
    assert api.symbol_domain(api.COMMANDS.get("load_background"), "id") == "location"
    assert api.symbol_domain(api.COMMANDS.get("set_primitive_slot"), "id") is None


def test_the_packed_character_word_gets_no_picker():
    """Picking a name for `character=` would write an id over a word that also
    carries the variant. The id half is a field of its own; that is what picks."""
    animation = api.COMMANDS.get("play_character_animation")
    assert api.symbol_domain(animation, "character") is None
    assert api.symbol_domain(animation, "character_number") == "character"


def test_flags_are_not_offered_as_a_list():
    """Names cover 175 of 8,191 flags, so a list would hide more than it shows."""
    assert api.symbol_domain(api.COMMANDS.get("set_flag"), "flag") is None
    assert "flag" not in api.PICKABLE_DOMAINS


def test_character_choices_include_the_abstraction_codes():
    choices = dict(api.domain_choices("character"))
    assert choices[1] == "Jack"
    assert choices[9900] == "current character"      # the most common operand there is
    assert choices[9901] == "party slot 1"


def test_every_pickable_domain_has_entries():
    for domain in api.PICKABLE_DOMAINS:
        assert api.domain_choices(domain), domain


###----------------------------------------- Insertion templates ---------------------------------------###

def test_every_palette_entry_has_a_template():
    missing = [name for name, _family, _summary in api.COMMANDS.palette_entries()
               if not api.command_template(name)]
    assert not missing, f"palette entries with nothing to insert: {missing}"


def test_templates_assemble_where_they_can():
    """Everything insertable should land as a working line. `option` is only
    legal inside a `choose` and `raw` needs an EVDSRC line from the author, so
    those two are stubs by nature."""
    head = 'event Main {\n    header(0x00000003)\n    entry(loc_000C)\n    label(loc_000C)\n'
    tail = '    end_script(yield=1)\n}\n'
    stubs = []
    for name, _family, _summary in api.COMMANDS.palette_entries():
        if name in ("event", "header", "header_extra", "headerExtra", "entry", "marker_table"):
            continue  # file-level directives; the fixture already has them
        template = api.command_template(name, 4)
        if api.validate_code(head + template + "\n" + tail) is not None:
            stubs.append(name)
    assert stubs == ["option", "raw"], f"unexpected non-assembling templates: {stubs}"


def test_block_templates_win_over_the_jump_example():
    """`if_value` is documented with `goto=loc_090C`, a label from the script
    that example came from. Inserting that elsewhere would assemble as a jump to
    the raw offset 0x090C, so the block form is what gets inserted."""
    template = api.command_template("if_value", 4)
    assert template.splitlines()[0].rstrip().endswith("{")
    assert "goto=" not in template


def test_unique_label_avoids_collisions(lines):
    assert api.unique_label(lines) == "loc_new"
    with_label = api.parse_code(api.render_code(lines) + "label(loc_new)\n")
    assert api.unique_label(with_label) == "loc_new1"


###----------------------------------------------- Handler ----------------------------------------------###

def test_handler_round_trips_bytes(script_bytes):
    node = VfsNode(name="test.evd")
    handler = EVDHandler(script_bytes)
    payload = handler.prepare_editor_data(node, script_bytes)
    assert payload.warning == ""
    assert payload.lines
    assert handler.decode_editor_data(node, EvdSavePayload(payload.code)) == script_bytes


def test_handler_rejects_a_non_evd():
    node = VfsNode(name="test.evd")
    handler = EVDHandler(b"NOPE" + b"\x00" * 32)
    with pytest.raises(EvdError):
        handler.prepare_editor_data(node, b"NOPE" + b"\x00" * 32)


def test_handler_rejects_a_bad_save(script_bytes):
    node = VfsNode(name="test.evd")
    handler = EVDHandler(script_bytes)
    with pytest.raises(api.EvdCompileError):
        handler.decode_editor_data(node, EvdSavePayload("event Main {\n    not_a_command()\n}\n"))


def test_handler_properties_reports_the_script(script_bytes):
    text = EVDHandler(script_bytes).properties()
    assert "header flags: 3" in text
    assert "commands:" in text


def test_stats_count_what_the_toolbar_shows(script_bytes, lines):
    stats = api.script_stats(script_bytes, lines)
    assert stats.byte_size == len(script_bytes)
    assert stats.command_count == sum(1 for l in lines if l.kind == api.KIND_COMMAND)
    assert stats.block_count == 1
    assert stats.label_count == 1


def test_event_name_is_sanitised_to_an_identifier():
    """EVD files are named `516_01`, which is not a legal EVDCODE event name."""
    assert api.event_name_for("516_01.evd") == "Event_516_01_evd"
    assert api.event_name_for("Main") == "Main"
    assert api.event_name_for("") == "Main"
