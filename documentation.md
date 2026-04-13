# Radiata Modding Tool 2.0 +

Post 2.0 the tool handles the filesystem automatically in memory. The goal is to browse the virtual file system in memory to extract, analyze, and rebuild the ISO data directly. It uses a node based approach where each virtual file is stored as a `VfsNode`.

## Key components

### Logic
- **Dispatcher**: Manages the active I/O stream, maintains the _buffer_cache for virtual layers, and performs recursive data retrieval. It acts as the "Stateless Factory" for handlers.
- **Contracts**: Interace definitions. `BaseHandler` manages format-specific logic (Unpacking/Rebuilding) while `BaseEditor` handles the data-viewing UI.
- **Registry**: Global lookup service for matching file signatures/extensions to their respective Handlers and Editors.
- **VfsNode**: Holds the data of the node
- **VfsManager**: Tracks node relationships (HID), physical disk offsets, and Dirty State (pending modifications). It does not perform I/O.
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


### Rebuild:
**Recursion** Post order
**Files** Front to back

## Finished TODO list:

- ~~Decouple UI components from main_window for readability and scalability -> `/ui/widgets/hex_editor.py`~~
- ~~ISO signature checking -> `/core/iso_handler.py`~~
- ~~SHA-1 ISO checking for USA, JPN, proto, or Modified -> `core/iso_handler.py`~~
- ~~Storing header in node for quick information lookup -> `/core/handlers/iso_handler.py` & `/models/vfs_node`~~
- ~~Debug/log window -> `/ui/widgets/`~~
- ~~Fix Logging -> `/core/logger.py` & `/ui/widgets/log_page.py`~~
- ~~Implement debugging repr and str as needed -> Everywhere~~
- ~~Rewrite windowing system For `QMainWindow` move to `RadiataModTool` only -> `/ui/main_window.py` & `/ui/widgets/workspace_page.py`~~
- ~~`QListWidget` to toggle tree category -> `/ui_core.py`~~
- ~~Rewrite the signal system for qt -> `/ui_core.py` & `/ui/`~~
- ~~Fix Logging exit python-C++ wrapper fighting -> `/core/logger.py`~~
- ~~Wrap the `self.log_signal.emit` in a `PyQt6.QtCore.QMetaObject.invokeMethod` to ensure the logging doesn't block the main thread logic -> `/core/logger.py`~~
- ~~Go over iso_handler for deprecated code and better node handoff -> `/core/handlers/iso_handler.py`~~
- ~~Change hierarchy logging str to tuple -> `/models/vfs_node.py`~~
- ~~Rewrite windowing system For stack try others `QTabWidget`, `QTabBar`, `QStackedWidget`~~

## Current TODO list:

- Connect CompressorHandler -> `/core/handlers/compression_handler.py`
- Connect KodsHandler -> `/core/handlers/kods_handler.py`
- Fully implement the context resolver -> `/core/registry.py.ActionResolver`
- Setup Asynchronous worker `QThreads` -> `/core/workers.py`
- Rewrite tree proxy signal handling and widget init currently scuffy -> `/ui_core.py`
- (For recursive virtual files. Wait until after all single level kods/slz is implemented)Redefine concerns for Dispatch/Node/Registry/Contract/Resolver -> `/core/`
- Staging/Commiting for edits. -> `/core/dispatcher.py`

## Future TODO list:

- Smart cache for recursive virtual files -> `/core/dispatcher.py`
- Separate ui_core/models as needed/prefered -> `/ui_core.py` & `/ui/tree_model.py`
- Search tree view -> `/ui_core.py`
- Non standard compressed format support. Chainded/Packed archives -> `/core/handler/compression_handler.py`
- Custom UI for hex editor so that it functions as intended (hex stays in place/edits have visible feedback) -> `/ui/widgets/hex_editor.py`
- Importing kods archiving including datacenter targeting -> `/core/handlers/kods_handler.py`
- UI improvements (settings, standardized theme.......)

# Rough Roadmap

- ~~Qt Window~~
- ~~Iso dump~~
- ~~Logging~~
- ~~Hex Editor~~
- Decompression
- Kods Unpacking
- Iso rebuilding
- Compression
- Kods Packing
  
# Fully finished features

- Qt Window
- Iso Handler
- Compressor Handler
- Kods Handler
- Hex Editor
- ~~Logger~~