# Overview

A desktop application for modding _Radiata Stories_. This application automatically manages the game's file system in-memory, allowing you to modify, extract, and analyze game data without cluttering your local machine with thousands of extracted files. 

It is built with a plugin system, allowing developers to support new file formats, custom data handlers, and editors without needing to rebuild the core application (see `documentation.md` for plugin authoring).

## Use Cases

- __Reverse engineering & Analysis__: Includes a built-in hex editor and hex diffing tool directly on the staging page for quick, precise binary analysis.
- __Datamining__: Browse the file tree, search for specific files, and extract unmodified game files directly to your PC.
- __Asset Modding__: Utilize built-in editors to modify assets on the fly, or use format handlers to translate between proprietary game formats and standard formats.

## Limitations

- The tool currently only manages the game's internal VFS. Modifications to game data outside of this file system are not yet supported.
- Successfully building an ISO does _not_ guarantee that the game will boot or run correctly with those modifications. It is highly recommended to make small, incremental modifications and verify them in-game as you go.

## List of current Plugins

| Plugin            | Format Support          | Features                     |
|-------------------|-------------------------|------------------------------|
| **Texture Editor**| `.fis`                  | Edit & export as PNG         |
| **Audio Player**  | `.020`                  | Playback + WAV export        |
| **Hex Editor**    | Any file                | Raw binary editing           |

## Technical Specifications
- __Languages__: Python, C (heavy data processing)
- __GUI__: Qt/PyQt6
- __Build__: Automated Windows and Linux CI releases with smoke testing via PyInstaller.

### Acknowledgements

Special thanks to project contributors and CUE.
Unofficial and not associated with Square Enix or tri-Ace