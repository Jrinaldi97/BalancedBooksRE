# Local internal-use Mac bundle. LICENSE and upstream attribution retained.
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules
root=Path(SPECPATH).resolve().parents[1]
datas=[(str(root/'index.html'),'.'),(str(root/'LICENSE'),'.'),(str(root/'alembic.ini'),'.'),(str(root/'.env.example'),'.'),(str(root/'app/static'),'app/static'),(str(root/'app/templates'),'app/templates'),(str(root/'migrations'),'migrations')]
a=Analysis([str(root/'balancedbooks_launcher.py')],pathex=[str(root)],datas=datas,hiddenimports=collect_submodules('app')+collect_submodules('uvicorn')+collect_submodules('alembic')+['webview.platforms.cocoa','sqlalchemy.dialects.sqlite','logging.config'],excludes=['pytest','tkinter','PyQt5','PyQt6','PySide2','PySide6'])
p=PYZ(a.pure)
e=EXE(p,a.scripts,exclude_binaries=True,name='BalancedBooksRE',console=False)
c=COLLECT(e,a.binaries,a.datas,name='BalancedBooksRE')
app=BUNDLE(c,name='BalancedBooksRE.app',bundle_identifier='com.balancedbooksre.desktop',info_plist={'CFBundleShortVersionString':'0.1.0','CFBundleVersion':'1','LSMinimumSystemVersion':'14.0','NSHighResolutionCapable':True,'NSDocumentsFolderUsageDescription':'Save accounting statements and exports you request.'})
