# Radiata Modding Tool 2.0 +

Post 2.0 the tool handles the filesystem automatically in memory. The goal is to browse the virtual file system in memory to extract, analyze, and rebuild the ISO data directly. It uses a node based approach where each virtual file is stored as a `VfsNode`.

## Key components

### Logic
- **Dispatcher**: Coordinator between UI - Registry - Active Handler
- **Contracts**: Structure plugins must adhear to (BaseHandler - Data logic, BaseEditor - UI)
- **Registry**: Plugin delared profiles, used to determine when a plugin can be called
- **VfsNode**: Holds the data of the node
- **VfsManager**: Manages the relational data of the node

### UI
- **MainWindow**: Initializes the `QMainWindow`, separates concerns, and contacts dispatcher for iso
- **MenuBar**: Create and handles the menu bar for the main window (may be separated by UI - Logic in future)
- **WelcomePage**: All logic for the welcome page
- **WorkspaceController**: Signals for the workspace page -> routed to dispatcher or resolver
- **WorkspaceWidget**: UI for the workspace page
- **TreeModel**: Contains the node data to display in the `tree_view` widget
- **CategoryModel**: Contains the data to display in category widget
- **CategoryProxyModel**: Contains the proxy data for the category applied to `tree_view`

### Utility (Bypass the contracts)
- **Logger**: Pyqt signal from logging. Displayed in log window with level - color

## Execution

### UI:
1. Start with welcome page where it askes for an ISO (takes the whole window)
2. **a.** ISO failed to load -> back to step 1
   **b.** ISO successfully loaded
3. Expandable list of categories on the left. Tree View of the nodes or selected category nodes on the left after the list of categories. Log on the bottom of the window with a clear button. Hex Editor on the right side of the window for the current selected node
4. **a.** Right click on tree view for context window of actions to perform on that node.
   **b.** Click on the hex editor to select a position to write or replace hex.
   **c.** Click on clear log button to clear the log.
   **d.** Click on a category to view only the associated nodes
5. **a.** tree view is expanded based on the new nodes that are unpacked
   **b.** Custom editor is opened over the current window
6. Click on commit changes
7. Rebuilding ISO in progress... With log of rebuild status

### Logic:
1. Node gets triggered from UI
2. Dispatcher connects node to handler and registry
3. **a.** Node handler is recieved for data processing
   **b.** Node registry is recieved for editor (contains handlers for formats, or generic byte handler)
4. **a.** New node gets registered in VfsNode and VfsManager
   **b.** Previously registered node's data is sent to editor
   

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

- Redefine concerns for Dispatch/Node/Registry/Contract/Resolver -> `/core/`
- Rewrite tree proxy signal handling and widget init currently scuffy -> `/ui_core.py`
- Fully implement the resolvers -> `/logic_core.py`
- Context Router for UI menu options -> `/ui/context_router.py`
- Add mounting system in dispatcher for depth diving -> `/core/dispatcher.py`
- Setup Asynchronous worker `QThreads` -> `/core/workers.py`
- Connect compressor class -> `/core/compression_handler.py` & `logic_core.py`
- Type check + comments and docstrings -> Everywhere
- Staging/Commiting for edits. -> `/core/dispatcher.py`

## Future TODO list:

- Non standard compressed format support. Chainded/Packed archives -> `/core/handler/compression_handler.py`
- Custom UI for hex editor so that it functions as intended (hex stays in place/edits have visible feedback) -> `/ui/widgets/hex_editor.py`
- Importing kods archiving including datacenter targeting -> `/core/handlers/kods_handler.py`
- UI improvements (settings, standardized theme.......)

# Rough Roadmap

- ~~Qt Window~~
- ~~Iso dump~~
- ~~Logging~~
- ~~Hex Editor~~
- Compressor
- Iso rebuilding
- Kods Unpacking
- Kods Packing
  
