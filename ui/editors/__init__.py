import pkgutil
import importlib
import sys
import json
from pathlib import Path

def discover_editors():
    """Dynamically import all modules in this package to trigger registration."""
    # Discovery in a frozen build
    if getattr(sys, 'frozen', False): 
        print("\n--- [DEBUG] FROZEN EDITOR DISCOVERY START ---")
        
        # Resolve the asset directory inside the temporary PyInstaller mount
        if not hasattr(sys, '_MEIPASS'):
            print('FATAL no MEIPASS')
            return
        base_path = Path(sys._MEIPASS)
        manifest_file = base_path / 'ui' / 'assets' / 'build_manifest.json'
        
        if not manifest_file.exists():
            print(" ❌ CRITICAL: build_manifest.json missing from bundle assets!")
            print(f"--- [DEBUG] END DISCOVERY. Total imported: 0 ---\n")
            return

        try:
            with open(manifest_file, 'r') as f:
                manifest_data = json.load(f)
        except Exception as e:
            print(f" ❌ CRITICAL: Failed to parse manifest layout: {e}")
            return

        discovered_count = 0
        # Iterate and import everything recorded during compilation
        for module_name in manifest_data.get("hiddenimports", []):
            if module_name.startswith("ui.editors."):
                print(f" 🔍 Found in Manifest: {module_name}")
                try:
                    importlib.import_module(module_name)
                    print(f"    ✅ Successfully imported {module_name}")
                    discovered_count += 1
                except Exception as e:
                    print(f"    ❌ FAILED to import {module_name}: {e}")
                    
        print(f"--- [DEBUG] END DISCOVERY. Total imported: {discovered_count} ---\n")
        return
    # Discovery from source
    for loader, module_name, is_pkg in pkgutil.walk_packages(__path__, __name__ + "."):
        if not is_pkg:
            importlib.import_module(module_name)