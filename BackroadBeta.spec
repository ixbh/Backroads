from pathlib import Path


project_dir = Path(SPECPATH)

a = Analysis(
    [str(project_dir / "desktop_launcher.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[
        (str(project_dir / "viewer.html"), "."),
        (str(project_dir / "manifest.webmanifest"), "."),
        (str(project_dir / "BETA_README.txt"), "."),
        (str(project_dir / "DATA_LICENSE.txt"), "."),
        (str(project_dir / "data" / "backroads.sqlite3"), "data"),
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["psycopg2", "osmium", "pyproj", "pytest"],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Backroad Beta",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Backroad Beta",
)
