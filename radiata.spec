# radiata.spec — PyInstaller onedir bundle
import glob, sys
datas = [
    ('ui/assets/static_sheet.qss', 'ui/assets'),
    ('ui/assets/dynamic_sheet.qss', 'ui/assets'),
    ('ui/assets/radi_metadata.json', 'ui/assets'),
]
# Prebuilt native libs (compressor + TAC) -> bundle at native/ (loaders look in _MEIPASS/native)
binaries = [(p, 'native') for p in glob.glob('native_build/*')]

a = Analysis(['main.py'], pathex=[], binaries=binaries, datas=datas,
             hiddenimports=[], hookspath=[], runtime_hooks=[], excludes=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='RadiataModdingTool',
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, name='RadiataModdingTool')

if sys.platform == 'darwin':
    app = BUNDLE(coll, name='RadiataModdingTool.app',
                 bundle_identifier='com.radiata.moddingtool')
