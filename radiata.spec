# radiata.spec - Windows: onefile | Linux: onedir (for AppImage) - MacOS not yet implemented
import glob, sys, os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# Ensure correct module discovery
sys.path.insert(0, os.path.abspath('.'))

# Automatically trace and include every python module inside these packages in the manifest
hiddenimports = [
    'core.handlers.iso_container',
    *collect_submodules('core.handlers'),
    *collect_submodules('ui.editors'),
]

datas = [
    ('ui/assets/static_sheet.qss', 'ui/assets'),
    ('ui/assets/dynamic_sheet.qss', 'ui/assets'),
    ('ui/assets/radi_metadata.json', 'ui/assets'),
    ('ui/assets/app_icon.png', 'ui/assets'),
]
datas.extend(collect_data_files('core'))
datas.extend(collect_data_files('ui'))

# Prebuilt native libs (compressor + TAC) -> bundle at native/ (loaders look in _MEIPASS/native)
binaries = [(p, 'native') for p in glob.glob('native_build/*')]

a = Analysis(
    ['main.py'], 
    pathex=[], 
    binaries=binaries, 
    datas=datas,
    hiddenimports=hiddenimports, 
    hookspath=[], 
    runtime_hooks=[], 
    excludes=[],
    noarchive=False,
    optimize=0
)
             
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

exe_kargs = {
    'name': 'RadiataModdingTool',
    'debug': False,
    'bootloader_ignore_signals': False,
    'icon': icon_target,
    'strip': False,
    'upx': False,
    'console': False,
    'disable_windowed_traceback': False,
}

if sys.platform == 'win32': # Windows: Onefile 
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        **exe_kargs,
        onefile=True
    )
else: # Linux & todo MacOS: Onedir
    exe = EXE(
        pyz, 
        a.scripts, 
        [], 
        exclude_binaries=True, 
        **exe_kargs
    )
    coll = COLLECT(
        exe, 
        a.binaries, 
        a.datas, 
        strip=False,
        upx=True,
        upx_exclude=[],
        name='RadiataModdingTool'
    )

    if sys.platform == 'darwin':
        app = BUNDLE(
            coll, 
            name='RadiataModdingTool.app',
            bundle_identifier='com.radiata.moddingtool',
            icon=icon_target
        )
