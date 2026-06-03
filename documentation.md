# Radiata Stories Modding Tool — Technical Documentation

A modular toolkit for unpacking, reverse-engineering, editing, and rebuilding game archives.

---

## Guide for Handlers/Editors

### Handlers - Raw data processors (Background Thread)

#### Decorators

```
@Registry.register(name, extensions, categories, supported_actions, is_fallback)

- name (required) = Semantic name for the handler used as the key for the profile in the registry
- extensions (optional) = The preferred specifier for when the logic should be applied. Preferred since the extension is matched against the raw data.
- categories (optional) = The alternative specifier for when the logic should be applied. Matches against the semantic tags in descriptor.json.
- supported_actions (*required) = Required for logic to fire. Supports two types of declaration:
    1. tuple(ActionDef) the preferred method 
    2. dict(str, ActionDef) alternative method gets translated into type 1. Migth be removed in the future old method...
- is_fallback (optional) = defaults to False. Used only to declare global handlers
```

#### Contracts

BaseHandler

- Enforced:
  1. `get_file_tree` : Creates nodes for the tree model.
  2. `rebuild_node` : Rebuilds a modified node into acceptable original format.
  3. `get_raw_node` : Returns a buffer of the raw data of a node. With priority for pending modifications.
- Editor:
  1. `prepare_editor_data`  : Process raw node data into an editor specific format.
  2. `decode_editor_data` : Process a modified editor specific format back into the original format.
- Recommended:
  1. `execute_action` : Routes action keys to their custom functions

Sub-Contracts

- PhysicalHandler: Sub-contract used for physical to virtual processing. Source is type `Path` and overrides`rebuild_node` to output to disk.
- ContainerHandler: Sub-contract used for virtual nodes that expand the virtual file system. Source is type `bytes`. All data passing between handlers happens in bytes, after a handler receives the bytes they can change the format.
- LeafHandler: Used for handlers that don't need to manage the file system, in otherwords they only care about the raw data in isolation. Stubs all 'Enforced' functions for convenience. Source is type `bytes`.


  
### Editors - Translated data viewers (Main thread)

Decorators: 

```
@Registry.register_editor(name, handler, extensions, categories, is_fallback)

- name (required) = Semantic name for the editor used as the key for the profile in the registry
- handler (required) = The class name for the handler that will process the raw data on a background thread for the editor
- extensions (optional) = The preferred specifier for when the editor is acceptable. Preferred since the extension is matched against the raw data.
- categories (optional) = The alternative specifier for when the editor is acceptable. Matches against the semantic tags in descriptor.json.
- is_fallback (optional) = defaults to False. Used only to declare global editors
```

Contract:

BaseEditor

- Enforced:
  1. `populate_ui`  : Called after data is received. Transitions the UI to a 'ready' state for modifying.
- Recommended:
  1. `begin_loading`  : Override to specify the editors loading state for when the handler is processing data.
  2. `receive_data` : Called by dispatcher when handler returns a processed payload. Should verify payload and call `populate_ui`
  3. `get_modified_data`  : For custom payload types will need to be overriden from the default bytes.
  4. `show_load_error`  : Override for specific load errors

Sub-Contract

- BaseViewer: Convenience base for immutable editors, stubs all mutation handling functions.

---

## Architecture overview

```
main.py
 └── discover_handlers/editors         # imports all handlers/editors → decorators register
 └── Dispatcher                        # bridge between UI and logic
      ├── VfsManager                   # tree state, HID lookups, threading lock
      ├── ModTracker                   # modification/staging state
      ├── TaskCoordinator              # QThreadPool wrapper
      └── VfsNavigator                 # tree traversal and rollup
           └── Registry                # format profile lookup (O(1) dicts)
```

---

## Module reference

### `core/registry.py` — Format Registry

Central service locator for all registered handlers and editors.

**Decorators**
To register handler/editors `@Registry.register(Fields)` with `Fields` from `FormatProfile` below.
To link a handler to an editor assign them to the same `name` insuring that atleast one of the registrations contains the `extensions` or `categories` you want to work on.

**`FormatProfile`** (frozen dataclass)

| Field | Type | Purpose |
|---|---|---|
| `name` | `str` | Display name — stamped as `_plugin_name` on the decorated class |
| `handler_class` | `type[BaseHandler]` | Format logic implementation |
| `extensions` | `tuple[str, ...]` | File extension matches (e.g. `('.kods',)`) |
| `actions` | `tuple[ActionDef, ...]` | Actions the handler exposes |
| `editor_class` | `type[BaseEditor] \| None` | Optional paired editor widget |
| `categories` | `tuple[str, ...]` | Semantic category matches |
| `is_fallback` | `bool` | Last-resort match; editor fallbacks become global editors |

**Key methods**

| Method | Returns | Notes |
|---|---|---|
| `get_profile(node)` | `FormatProfile \| None` | Extension match → category match → fallback |
| `get_handler(source)` | `type[BaseHandler] \| None` | Path or VfsNode |
| `get_editor(node)` | `list[type[BaseEditor]]` | Format-specific first, then global editors |
| `get_action(node, name)` | `ActionDef \| None` | Profile actions → `GLOBAL_ACTIONS` |

**`GLOBAL_ACTIONS`** — `(Export, Import)` — available on every non-hidden, non-empty node.

**`_global_editors`** — editors registered with `is_fallback=True`; appended to every `get_editor` result.

**Registration pattern**

```python
@Registry.register(
    name='Kods Archiver',
    extensions=('.kods',),
    supported_actions=(
        ActionDef('Unpack',     ActionType.TREE_EXPAND),
        ActionDef('Properties', ActionType.DIALOG),
    ),
)
class KodsHandler(ContainerHandler): ...
```

```python
@Registry.register(name='Hex Editor', extensions=(), is_fallback=True)
class HexEditorWidget(BaseEditor): ...
```

---

### `core/workers.py` — Actions and Threading

**`ActionType`** (Enum)

| Value | Dispatcher behaviour |
|---|---|
| `TREE_EXPAND` | Payload is a `VfsNode` — insert children into VFS |
| `PROCESS` | Payload is `bytes`/`str` — pass to editor |
| `DIALOG` | Payload is `str` — show in descriptor panel or dialog |
| `EXPORT` | Write bytes to disk |
| `IMPORT` | Read bytes from disk into node |

**`ActionDef`** (frozen dataclass) — `name: str`, `action_type: ActionType`. Name is the execute key and context menu label.

**`ActionResult`** (dataclass) — `action_name`, `node`, `status`, `payload`, `message`.

**`EditorPayload`** (dataclass) — `node`, `data`

**`Actions`** — static class. All background task logic in one place.

| Method | Thread | Purpose |
|---|---|---|
| `prepare_editor(handler, node, navigator)` | Worker | Unwrap → resolve headers → `prepare_editor` → `EditorPayload`|
| `dispatch(action_def, node, navigator)` | Worker | Single entry point for all node actions; routes by `ActionType` |
| `rebuild_iso(handler, root, navigator, staged, path)` | Worker | Rollup + sequential ISO write |
| `verify_iso(handler)` | Worker | xxhash3-128 full-disk hash |
| `export_node(node, path, navigator)` | Worker | Unwrap chain → write to disk |
| `_run_handler_action(...)` | Worker | Unwrap → resolve headers → `execute_action` |
| `_import_node(node, path)` | Worker | Read file → payload bytes |

**`TaskCoordinator`** — wraps a private `QThreadPool` (max 4 threads). Never touches `globalInstance()`. Returns `TaskSignals` for each started task.

**Convention** inside every `Actions` method:

- `log_callback(msg)` → user-facing messages → `LoggingWindow`
- `logger.*` → system/debug output, filtered by log level
- `progress_callback(pct, label)` → drives the progress bar

---

### `core/contracts.py` — Handler and Editor Contracts

**`BaseHandler`**

- `get_identity()` — reads `_plugin_name` stamped by decorator; override only for runtime-dependent strings
- `execute_action(node, action_name, progress_callback, log_callback, **kwargs)` — optional; base logs a warning
- `get_file_tree()`, `rebuild_node()`, `get_raw_node()` — abstract
- `prepare_editor_data(node, raw_bytes)` — Default returns bytes, override to return custom formats (QImage, gtTf... etc)

**Specialisations**

| Class | Source | Use case |
|---|---|---|
| `PhysicalHandler` | `Path` — opens file handle | ISO; writes to disk, returns `bool` |
| `ContainerHandler` | `bytes` — wraps in `BytesIO` | Kods, SLZ, chain files; returns `bytes` |
| `LeafHandler` | `bytes` — wraps in `BytesIO` | FIS, IO; stubs for tree/rebuild, logic in `execute_action` |

**`BaseEditor`** (template-method pattern)

- `begin_loading(node)` — temp UI while the BG thread fetches the data from the handler
- `receive_data(result)` — Called when the data fetch is completed -> `_populate_ui(result[data])`
- `_populate_ui(data)` — **abstract** — Populates the UI with the fetched data
- `get_modified_data()` — returns `_original_data` by default (read-only editors don't override)
- `apply_changes()` — emits `apply_requested(node, bytes)`, clears dirty flag
- `discard_changes()` — calls `_populate_ui(_original_data)`, clears dirty flag
- `request_node_data(target_node)` — fetches sibling node bytes via injected `_data_resolver`
- `cleanup()` — releases node reference and data resolver; called by `EditorPage` before `deleteLater()`

**Read-Only mixin - Convenience**  (provides no-ops/passthroughs for mutable functions)

```python
class BaseViewer(BaseEditor):
    is_mutable = False
    def set_dirty(self, _): pass
    def apply_changes(self): pass
    def discard_changes(self): pass
    def get_modified_data(self): return self._original_data
```

`EditorPage._on_back` checks `editor.is_mutable` before showing the unsaved-changes prompt.

---

### `core/node.py` — VFS Data Structures

**`VfsNode`** — pure data container.

| Attribute | Purpose |
|---|---|
| `name`, `category`, `extension` | Display and routing metadata |
| `offset`, `size` | Position in parent container |
| `header` | First 32 bytes of raw data |
| `target` | List of datacenter header HIDs (for `.kods` nodes) |
| `_id_path` | Hierarchical ID tuple — positional, assigned by `append_child` |
| `pending_data` | Unsaved edit bytes; checked first by `get_node_data` |
| `status` | `NodeStatus.UNMODIFIED / MODIFIED / STAGED` |
| `compressed_header` | `SlzHeader` from `CompressorHandler` |
| `is_physical` | True for direct ISO nodes |
| `is_hidden` | Filters from UI and global actions |
| `expansion_pending` | Set `True` while async expansion is in progress |
| `_expansion_event` | `threading.Event` — navigator waits on this during HID resolution |

**`VfsManager`**

Threading contract: all structural reads/writes protected by `self._lock` (`threading.RLock`). Qt signals emitted **after** lock release so tree model slots can safely query the manager.

| Method | Notes |
|---|---|
| `insert_children(parent, children)` | Sets `_id_path`, registers nodes, emits `children_inserted` outside lock |
| `register_node(node)` | Adds to `nodes_by_id` under lock |
| `partition_hids(hids)` | Lock-protected snapshot → `HidPartition(resolved, unresolved)` |
| `find_deepest_ancestor(hid)` | Returns deepest registered ancestor for expansion requests |
| `get_node_by_id(hid)` | O(1) lookup |

**`ModTracker`** — tracks `modified_nodes` and `rebuild_queue` as sets of `VfsNode`. Emits `state_changed(modified_count, staged_count)`.

---

### `core/navigator.py` — VFS Traversal

**`VfsNavigator`** — constructed with `(vfs, data_reader, expansion_callback)`.

**Two-phase HID resolution** (`resolve_data_from_hid`):

1. **Phase 1** — `vfs.partition_hids()` — lock-protected snapshot, no blocking
2. **Phase 2** — `_expand_pending()` — for each unresolved HID, find deepest ancestor, call `expansion_callback(ancestor, wait_event)`, block on `wait_event.wait(timeout=10s)`. Lock is never held during this phase so the main thread is free to process the queued signal.
3. **Phase 3** — re-read in original HID order; result order matches input

**Expansion states** handled in `_expand_pending`:

| State | Behaviour |
|---|---|
| `expansion_pending=True` | Wait on `_expansion_event` — another task is already running |
| `has children, target not found` | Re-expand (node expanded without correct headers) |
| `no children, not pending` | First expansion |

**`rollup_nodes(staged)`** — bottom-up repack. Finds deepest nodes, groups by parent, calls `handler.rebuild_node`, sets `parent.pending_data`, repeats until all nodes are physical.

**`unwrap_chain(node)`** — walks from physical root to virtual target, decompressing each layer via the registered handler. HID depth > 2 triggers `get_buffer_data` for composite buffer nodes.

---

### `core/dispatcher.py` — Routing

Bridge between UI and logic. Holds `vfs`, `tracker`, `active_handler`, `nav`, `task_coordinator`.

| Method | Thread | Notes |
|---|---|---|
| `load_source(Path)` | Main | `_load_physical` → opens handler, builds tree, fires `verify_iso` task |
| `load_source(VfsNode)` | Main | Reads `profile.primary_expand_action()` — no extension hardcoding |
| `get_node_data(node)` | Any | `pending_data` → physical handler → `nav.unwrap_chain` |
| `execute_node_action(node, name)` | Main (starts worker) | `Registry.get_action` → `Actions.dispatch` |
| `start_iso_rebuild(path)` | Main (starts worker) | `Actions.rebuild_iso` — rollup + write both on worker thread |
| `_expand_node(node, event)` | Background | Emits `expand_requested` (QueuedConnection); background thread blocks on event |
| `_handle_expand_request(node, event)` | Main | Reads `primary_expand_action`, starts `Actions.dispatch` task, calls `event.set()` on completion |
| `_on_action_complete(success, result)` | Main | Routes by `ActionType` — no action-name string matching |

---

### `core/handlers/` — Format Implementations

#### `iso_handler.py` — `IsoHandler(PhysicalHandler)`

Disc layout:

```
[sector 0 → toc_offset]          disc header + volume descriptor
[toc_offset]                      scrambled TOC  (0x1200 × 3 × 4 bytes)
[toc_offset + toc_size → end]     sequential file data, sector-aligned
```

TOC integrity: the scrambled value of entry 0 equals `params.signature` (`0x27D51556`). This is reconstructed exactly in `_build_toc` by pre-inverting with the seed before scrambling. A signature mismatch assertion fires before any bytes are written to disk.

Rebuild — 5-step sequential write:
1. Copy disc header (`_stream_copy` 0 → `toc_offset`)
2. Reserve zeroed TOC space
3. Sequential file write — entry 0 and aliases (sentinel LBA) are skipped; normal entries advance `current_offset`
4. Build + verify TOC; seek back to write at `toc_offset`
5. Patch ISO9660 Volume Descriptor volume space size field (sector 16, offset 80)

File deletion on failure is scoped to the **output path only**.

#### `compression_handler.py` — `CompressorHandler(ContainerHandler)`

SLZ/SLE chained compressed files. Four modes via `SlzMode`:

| Mode | Algorithm |
|---|---|
| 0 | STORE (copy) |
| 1 | LZSS |
| 2 | LZSS + RLE |
| 3 | LZSS16 (word-aligned) |

SLE files are XOR-scrambled with a 16-byte key + rolling mod. Chain: `next_file_offset == 0` signals end of chain. BCB nodes (banked audio) are sector-aligned.

#### `kods_handler.py` — `KodsHandler(ContainerHandler)`

Two-source architecture: internal header (magic `Kods`) and external datacenter headers resolved via `target` HID list. Rebuild strategies: composite buffer patch (Layer 1) and static archive patch (Layer 3).

#### `chain_handler.py` — `ChainHandler(ContainerHandler)`

Chained sequential format (`.fps`, `.fas`, `.rmac`, …). Each entry: 4-byte magic + `payload_size` + `offset_from_last_file` + `next_file_offset` (all little-endian uint32). All headers rebuilt on every write — unmodified nodes preserve original payload bytes but get fresh headers so offsets remain valid after any size change.

#### `fis_handler.py` — `FisHandler(LeafHandler)` + `FisEditorWidget(ReadOnlyEditor)`

PS2 PSMT8 / PSMT4 textures decoded directly to `QImage` (no PIL). Decoding: palette → CLUT interleave (8bpp) → optional unswizzle → index-to-RGBA flat buffer → `QImage(Format_RGBA8888).copy()`. The `.copy()` is required because `QImage` with a raw buffer is non-owning. Export to PNG via `QImage.save()`.

#### `io_handler.py` — `IOHandler(LeafHandler)`

Global Import/Export implementation. Registered with `is_fallback=True`; paired with `GLOBAL_ACTIONS` so every eligible node gets Export/Import in its context menu.

---

### `ui/ui_core.py` — UI Layer

**Page stack** (`QStackedWidget`):

| Index | Widget | Trigger |
|---|---|---|
| 0 | `WelcomePage` | App launch / ISO close |
| 1 | `WorkspaceWidget` | ISO loaded |
| 2 | `StagingPage` | "Review & Rebuild" button |
| 3 | `RebuildStatusPage` | Rebuild initiated |
| 4 | `EditorPage` | "Open in …" context menu action / double-click |

**`FileeDescriptorPanel`** — right panel of workspace, shown on node selection.

Sources: `_DESCRIPTORS` JSON (loaded once from `data/descriptors.json`) and the async `Properties`  handler action result.

| Section | Source |
|---|---|
| Header (name, HID) | VfsNode + descriptor JSON `title` |
| Tags | descriptor JSON `tags` |
| Description | descriptor JSON `description` |
| Properties | async `Properties` action — updated when `action_complete` arrives |
| File Info | VfsNode raw metadata |

**`EditorPage`** — full-screen editor host.

- `load_editor(editor, node)` — calls `cleanup()` on previous editor, swaps widget, updates title bar
- `_on_back()` — checks `editor.is_mutable`; prompts Save / Discard / Cancel if dirty

**`WorkspaceController`**

`handle_tree_select` — loads descriptor panel immediately, then fires `Properties` action if registered. Auto-launches `editors[0]` (most specific) via `launch_editor`.

`handle_context_menu` — builds one menu item per editor from `Registry.get_editor(node)`, labelled `"{role} in {plugin_name}"`. Most specific editor is bold. Format actions from `profile.actions` + `GLOBAL_ACTIONS`, sorted by `_ACTION_TYPE_PRIORITY`.

`handle_action_result` — routes by `ActionType` via `Registry.get_action`; never matches on action name strings. `DIALOG` results for `Properties` are routed to the descriptor panel if the node matches the current selection.


---

### `ui/theme_manager.py` — `ui/assets/style_sheet.qss` & `ui/assets/font_sheet.qss`

`class ExampleTheme` — Stores all the pallete/size variables.

`ThemeManager` — Injects stored `ExampleTheme` values into qss

`QSS` split for optimization:

- `style_sheet.qss` — Static UI elements. Reloaded on theme change.
- `font_sheet.qss` — Dynamic UI elements. Reloaded when zoom adjusted and theme change.

---

### `ui/widgets/hex_editor.py` — `HexEditorWidget(BaseEditor)`

Registered as global fallback editor (`is_fallback=True`).

Features: 18-column table (offset | 16 hex cells | ASCII), modified-byte highlighting (amber), hex search with wrap-around, inspector bar (u8/i8/u16/u32/f32 LE+BE at cursor), four copy formats (hex, Python literal, C array, ASCII), paste hex from clipboard, fill-with-zero, Ctrl+S apply shortcut.

---

### `core/handlers/__init__.py` & `ui/editors/__init__.py` — Startup Registration

```python
from core.handlers import discover_handlers
from ui.editors import discover_editors
discover_handlers()
discover_editors()
```

All `@Registry.register` decorators fire here. Every new handler or editor is automatically discovered via `__init__.py` in it's directory.

---

## Data flows

### 1. File system expansion

```
UI click → route_action(node, TREE_EXPAND ActionDef)
  → dispatcher.execute_node_action
    → Registry.get_action → Actions.dispatch (worker thread)
      → Actions._run_handler_action
          → navigator.unwrap_chain       (bytes for this node)
          → navigator.resolve_data_from_hid  (datacenter headers, two-phase)
          → handler.execute_action → get_file_tree → VfsNode tree
  → _on_action_complete (main thread)
      → vfs.register_node + vfs.insert_children
        → children_inserted signal → VfsTreeModel.on_children_inserted
          → beginInsertRows / endInsertRows
      → tree_view.expand(proxy_index)
```

### 2. Datacenter HID resolution

```
Background thread: resolve_data_from_hid([hid, ...])
  Phase 1: vfs.partition_hids()  ← lock-protected snapshot, instant
  Phase 2: for each unresolved hid:
      vfs.find_deepest_ancestor(hid) → ancestor node
      expansion_callback(ancestor, wait_event)
        → expand_requested.emit (QueuedConnection)
          → main thread: _handle_expand_request
              → Actions.dispatch task (new worker)
                → on complete: vfs.insert_children → wait_event.set()
      wait_event.wait(timeout=10s)  ← background thread blocks here
  Phase 3: re-read nodes in original HID order
```

### 3. Data retrieval

```
handle_tree_select(node)
  → descriptor_panel.load_node(node)       ← "Loading..." placeholder, immediate
  → dispatcher.execute_node_action(Properties)  ← async
      → Actions.dispatch (worker)
        → handler.execute_action('Properties') → str payload
      → handle_action_result (main thread)
        → descriptor_panel.set_properties_text(str) → Fills placeholder

"Open in Hex Editor" → launch_editor(node, editor)
  → editor.begin_loading(node)
  → editor_page.load_editor(editor, node)       ← "Loading..." placeholder, immediate
  → stack.setCurrentWidget(editor_page)
  → dispatcher.load_editor(node, editor)  ← async
      navigator.unwrap_chain → resolve_headers → process data
  → controller._on_editor_data_ready(success, payload, editor) → Any payload -default bytes
      verify payload → signal to editor
  → editor.receive_data(bytes, data_resolver=dispatcher.get_node_data) → override for non-byte payloads
  → editor._populate_ui(data)                  → Fills placeholder
```

### 4. Data commit (Build ISO)

```
StagingPage → tracker.confirm_and_rebuild()
  → rebuild_initiated signal → MainWindow.start_rebuild
    → QFileDialog (save path)
    → dispatcher.start_iso_rebuild(output_path)
      → Actions.rebuild_iso (worker thread)
          → navigator.rollup_nodes(staged)     ← bottom-up repack
              for each virtual layer (deepest → physical):
                handler.rebuild_node(parent, modified_children) → bytes
                parent.pending_data = bytes
          → handler.rebuild_node(root, physical_staged, output_path)
              1. copy disc header
              2. reserve TOC space
              3. sequential file write
              4. build + verify TOC signature → seek back + write
              5. patch ISO9660 volume descriptor
      → _on_rebuild_finished → tracker.clear() + rebuild_complete signal
```

---

### Rebuild:
**Recursion** Post order
**Disk** Front to back

## Current TODO list:

- Perfect rebuilding Kods containers -> `/core/handlers/kods_container.py`
- FIS Undo/Revert seem to revert canvas state not image state fix the wiring -> `/ui/editors/fis_editor.py`
- Standardize saving mechanics for BaseEditors -> `/ui/ui_core.py` & `/ui/editors/...`
- Consider changing the contract to recieve data being the abstract over populate ui

## Future TODO list:

- Fix background task bailout/timeout, if spammed infinite loops -> `/core/workers.py`
- FPS chain unpacking needs to be reevaluated since fps heads may contain other fps chains within -> `/core/handlers/chain_handler.py`
- FIS editor decoding CLUT shifts 7F to 80 -> `/core/handlers/fis_leaf.py`
- Support multiple handlers on a file rather than just the most valid. -> `/core/...` & `/ui/ui_core.py`
- Hex editor toggle for bottom values to display in hex or dec -> `/ui/editors/hex_editor.py`
- Staging page diff... This could be greatly improved but I don't want to spend a ton of time on anything beyond the basics to allow better analysis of custom format building -> `/ui/ui_core.py`
- 0FDC unpacking, seems to be an archive of slz format -> `/core/handlers/fdc_handler.py`
- FIS textures, Bank 6, ~Bank 0, Bank 1. Investigation needed. -> `/core/handlers/fis_handler.py`
- Icons? -> `/ui/...`
- seqw handler -> `/core/handler/seqw_handler.py`
- Check stylesheet when there are more elements. Consider implementing generic objects rather than specific -> `/ui/style_sheet.py`


Rebuilding kods nodes returns tuple. Element 1 being the payload. Element 2 being the datacenter header if valid.
After datacenter dependant modification the ModTracker, Dispatcher, and Navigator will need to handle the kods rebuild payloads correctly to ensure that Element 1 is applied to the original modified node, and Element 2 is applied to the target node of the original modified node.