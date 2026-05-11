# Radiata Stories Tool suite: Technical Documentation

This document provides a highly detailed overview of the system architecture, file system models, binary format handlers, and UI synchronization pipelines of the Radiata Stories Modding & Virtual File System (VFS) Suite.

This suite is a modular, high-performance toolkit designed to unpack, reverse-engineer, edit, and rebuild game archives from the PlayStation 2 game Radiata Stories (tri-Ace engine).

**Documentation will be updated after the next UI pass**


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

- migrate from using an identity method to getting the identity from the class decorator -> `/core/handlers/`
- Setup better object structure for VfsNode rather than large list of bools -> `/core/node.py`
- .pk/audio format support -> `/core/handler/iso_handler.py` & `/core/extension_overrides.py`
- Better File statistics / display -> `/core/handlers/`
- FIS/texture handlers / display -> `/core/handlers/fis_handler.py`
- Further investigate the composite Kods unpacking before deciding rebuild strategy -> `/core/handler/kods_handler.py` & `/core/handler/compressor_handler.py` & `/core/dispatcher'py`
- Saved settings json -> `/ui/`
- Closing application has to close all background threads.
- seqw handler -> `/core/handler/seqw_handler.py`
- wav player (need music when editing obv) -> `/plugins/wav_player.py`

## Future TODO list:

- Check stylesheet when there are more elements. Consider implementing generic objects rather than specific as needed -> `/ui/style_sheet.py`
- Stagin/Commiting Menu improvements -> `/ui/ui_core.py`
- Smart cache for editors -> `/core/dispatcher.py`


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