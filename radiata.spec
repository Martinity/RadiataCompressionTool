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

EXCLUDED_LIBS = {
    'libavcodec.so', 'libavformat.so', 'libavutil.so', 'libswresample.so', 'libswscale.so',  # FFmpeg
    'Qt6Quick', 'Qt6Qml', 'Qt6Network' # Remove unused Qt framework bindings
}

a = Analysis(['main.py'], pathex=[], binaries=binaries, datas=datas,
             hiddenimports=hiddenimports, 
             hookspath=[], runtime_hooks=[], excludes=['tkinter', 'matplotlib', 'numpy', 'unittest', 'PyQt6.QtQml'])
a.binaries = [x for x in a.binaries if not any(bad in x[0] for bad in EXCLUDED_LIBS)]
             
pyz = PYZ(a.pure)

icon_target = None
if sys.platform == 'win32':
    icon_path = os.path.join('ui', 'assets', 'app_icon.ico')
    if os.path.exists(icon_path):
        icon_target = icon_path
elif sys.platform == 'darwin':
    icon_path = os.path.join('ui', 'assets', 'app_icon.icns')
    if os.path.exists(icon_path):
        icon_target = icon_path

exe = EXE(pyz, 
          a.scripts, 
          [], 
          exclude_binaries=True, 
          name='RadiataModdingTool',
          icon=icon_target,
          strip=True,
          upx=True,
          console=False, 
          disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, name='RadiataModdingTool')

if sys.platform == 'darwin':
    app = BUNDLE(coll, name='RadiataModdingTool.app',
                 bundle_identifier='com.radiata.moddingtool',
                 icon=icon_target)
