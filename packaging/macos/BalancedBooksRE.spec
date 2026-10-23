# Local internal-use Mac bundle. LICENSE and upstream attribution retained.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files
root=Path(SPECPATH).resolve().parents[1]
datas=[(str(root/'index.html'),'.'),(str(root/'LICENSE'),'.'),(str(root/'alembic.ini'),'.'),(str(root/'.env.example'),'.'),(str(root/'app/static'),'app/static'),(str(root/'app/templates'),'app/templates'),(str(root/'migrations'),'migrations')]
datas += collect_data_files('certifi')
manifest = root / 'app/portfolio/build_manifest.json'
if not manifest.is_file():
    raise ValueError('Run scripts/bbre_build_manifest.py from a clean checkout before building')
datas.append((str(manifest), 'app/portfolio'))
a=Analysis([str(root/'balancedbooks_launcher.py')],pathex=[str(root)],datas=datas,hiddenimports=collect_submodules('app')+collect_submodules('uvicorn')+collect_submodules('alembic')+collect_submodules('xlrd')+['webview.platforms.cocoa','sqlalchemy.dialects.sqlite','logging.config'],excludes=['pytest','tkinter','PyQt5','PyQt6','PySide2','PySide6'])
p=PYZ(a.pure)
e=EXE(p,a.scripts,exclude_binaries=True,name='BalancedBooksRE',console=False)
c=COLLECT(e,a.binaries,a.datas,name='BalancedBooksRE')
app=BUNDLE(c,name='BalancedBooksRE.app',bundle_identifier='com.balancedbooksre.desktop',info_plist={'CFBundleShortVersionString':'0.4.0','CFBundleVersion':'20261023.1','LSMinimumSystemVersion':'14.0','NSHighResolutionCapable':True,'NSDocumentsFolderUsageDescription':'Save accounting statements and exports you request.'})
