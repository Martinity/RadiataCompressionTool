# radiata.spec — PyInstaller onedir bundle
import glob, sys, os, json
from PyInstaller.utils.hooks import collect_submodules
# Force the correct path for module discovery
sys.path.insert(0, os.path.abspath('.'))

# Automatically trace and include every python module inside these packages in the manifest
hiddenimports = collect_submodules('core.handlers') + collect_submodules('ui.editors')
if 'core.handlers.iso_container' not in hiddenimports:
    hiddenimports.append('core.handlers.iso_container')
manifest_path = os.path.join('ui', 'assets', 'build_manifest.json')
os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
with open(manifest_path, 'w') as f:
    json.dump({"hiddenimports": hiddenimports}, f, indent=4)

datas = [
    ('ui/assets/static_sheet.qss', 'ui/assets'),
    ('ui/assets/dynamic_sheet.qss', 'ui/assets'),
    ('ui/assets/radi_metadata.json', 'ui/assets'),
    ('ui/assets/app_icon.png', 'ui/assets/'),
    ('ui/assets/build_manifest.json', 'ui/assets'),
]
# Prebuilt native libs (compressor + TAC) -> bundle at native/ (loaders look in _MEIPASS/native)
binaries = [(p, 'native') for p in glob.glob('native_build/*')]

a = Analysis(['main.py'], pathex=[], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, 
             hookspath=[], runtime_hooks=[], excludes=[])
             
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='RadiataModdingTool',
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, name='RadiataModdingTool')

if sys.platform == 'darwin':
    app = BUNDLE(coll, name='RadiataModdingTool.app',
                 bundle_identifier='com.radiata.moddingtool')
