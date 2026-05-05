# Radiata Modding Tool 2.0 +

Post 2.0 the tool handles the filesystem automatically in memory. The goal is to browse the virtual file system in memory to extract, analyze, and rebuild the ISO data directly. It uses a node based approach where each virtual file is stored as a `VfsNode`.

## Key components

### Logic
- **Dispatcher**: Manages the active I/O stream, maintains the _buffer_cache for virtual layers, and performs recursive data retrieval. It acts as the "Stateless Factory" for handlers. Type 1. `active_handler` = handler for physical files. Type 2. `temp_handler` = handler for virtual files. The distinction is made because `active_handler` stays open from ISO mount while `temp_handler` is only open during transactions.2
- **Contracts**: Interace definitions. `BaseHandler` manages format-specific logic (Unpacking/Rebuilding...) while `BaseEditor` handles the data-viewing UI. The **only** place for performing I/O
- **Registry**: Global lookup service for matching file signatures/extensions as well as actions to their respective Handlers and Editors.
- **VfsNode**: Holds the data of the node
- **VfsManager**: Tracks node relationships (HID), physical disk offsets, and Dirty State (pending modifications).
- **ActionResolver**: Determines context-aware capabilities by merging global actions with format-specific handler actions.

### UI
- **MainWindow**: Initializes the `QMainWindow`, separates concerns, and contacts dispatcher for iso
- **MenuBar**: Create and handles the menu bar for the main window (may be separated by UI - Logic in future)
- **WelcomePage**: All logic for the welcome page
- **WorkspaceController**: Signals for the workspace page -> Translates user interactions into commands for the `ActionResolver` and `Dispatcher`.
- **WorkspaceWidget**: UI for the workspace page
- **TreeModel**: Contains read-only node data to display in the `tree_view` widget
- **CategoryModel**: Contains the data to display in category widget
- **CategoryProxyModel**: Contains the proxy data for the category applied to `tree_view`

### Utility
- **Logger**: Logging system using PyQt signals to bridge standard Python logging into the UI console. With level to color output.


# Data flow
#### 1. File System Expansion (Modifying the Tree)
Used for: ISO mounting, Archive unpacking (`.kod`), and Decompression (`.slz`).
1. `WorkspaceController::route_action` intercepts a UI event ('Unpack' or 'Decompress').
2. `Dispatcher::load_source` identifies the correct `BaseHandler` via the Registry.
3. **Execution**:
     1. **Physical File**: Instantiates and keeps the handler open as `active_handler` for the life of the workspace
     2. **Virtual File**: Instantiate a temp handler inside a `with` for memory management
        1. **Datacenter**: Recursively fetch raw header(s) -> Go to 2.
4. `BaseHandler::get_file_tree` parses the container metadata and returns a "detached" `VfsNode` tree.
5. `VfsManager` signals to `VfsTreeModel::on_node_registered` that changes are incoming
6. `Dispatcher` integrates the children nodes into the main tree and registers them with the `VfsManager` to track physical/virtual relationships.
7. `VfsManager` signals to `VfsTreeModel::on_node_registered` that changes are finished and to re-draw the UI.
8. **UI Integration**: 
     1. **Full Reset**:`WorkspaceController` notifies the Qt Model (`layoutChanged`) to re-draw the tree.
     2. **Node Insertion**: `WorkspaceController` passes nodes to `VfsTreeModel::add_nodes` wrapping the mutation in `beginInsertRows` and `endInsertRows` for memory management.

#### 2. Data Retrieval (Opening an Editor)
Used for: Hex View.
1. A `BaseEditor` variant is initialized and requests data for its target node.
2. `Dispatcher::get_node_data` checks the Priority Gate:
    * **Level 1**: If `node.pending_data` exists (unsaved edits), return that immediately.
    * **Level 2**: If not, call `BaseHandler::process_node` to fetch the bytes.
3.   **Extraction**: `BaseHandler` performs the physical I/O (seek/read) or logical transformation (decryption).
4. `Dispatcher` passes the resulting bytes to the `BaseEditor`.
5. `BaseEditor::load_node` populates the UI widgets with the data.

#### 3. Data Commit (Rebuilding ISO)
Used for: Pushing editor changes to ISO
...

### Rebuild:
**Recursion** Post order
**Files** Front to back


## Current TODO list:

- Temp file scanning for SLZ / Kods headers with statistics outputting -> `/core/handler/compressor_handler.py` & `/core/handler/kods_handler.py`
- Better File statistics / display -> `/core/handlers/`
- Further investigate the composite Kods unpacking before deciding rebuild strategy -> `/core/handler/kods_handler.py` & `/core/handler/compressor_handler.py` & `/core/dispatcher'py`
- Setup Asynchronous worker `QThreads` -> `/core/workers.py`
- Improved ISO detection -> `/core/handlers/iso_handler.py`
- Improved UI for hex editor -> `/ui/widgets/hex_editor.py`
- node import -> `/core/handlers/`
- node export -> `/core/handlers/`
- Proper Status Logging for rebuild -> `/core/handlers/iso_handler.py`
- Saved settings json -> `/ui/`

## Future TODO list:

- Stagin/Commiting Menu improvements -> `/ui/ui_core.py`
- Smart cache for editors -> `/core/dispatcher.py`
- .pk support -> `/core/handler/iso_handler.py` & `/core/extension_overrides.py`
- UI improvements (settings, standardized theme.......)

# Rough Roadmap

- ~~Qt Window~~
- ~~Iso dump~~
- ~~Logging~~
- ~~Hex Editor~~
- ~~Decompression~~
- Kods Unpacking
- ~~Iso rebuilding~~
- ~~Compression~~
- Kods Packing
  
# Fully finished features

- Qt Window
- Iso Handler
- Compressor Handler
- Kods Handler
- Hex Editor
- ~~Logger~~