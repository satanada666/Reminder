# PyInstaller: сборка под текущую ОС.  Запуск:  pyinstaller napominalka.spec
#   Windows -> dist/Napominalka.exe        (один файл)
#   Linux   -> dist/Napominalka            (один файл)
#   macOS   -> dist/Napominalka.app        (приложение)
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

sys.path.insert(0, str(Path(SPECPATH)))
from icon_art import save_all  # noqa: E402

assets = save_all(Path(SPECPATH) / "build_assets")
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

datas = collect_data_files("customtkinter") + collect_data_files("speech_recognition")
hidden = collect_submodules("pystray") + ["PIL._tkinter_finder"]
if IS_WIN:
    hidden += ["winotify"]

a = Analysis(
    ["napominalka.pyw"],
    pathex=[SPECPATH],
    datas=datas,
    hiddenimports=hidden,
    excludes=["matplotlib", "pandas", "scipy", "pytest"],
)
pyz = PYZ(a.pure)
icon = str(assets / ("icon.ico" if IS_WIN else "icon.icns" if IS_MAC else "icon.png"))

if IS_MAC:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Napominalka",
              console=False, icon=icon, argv_emulation=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="Napominalka")
    app = BUNDLE(
        coll, name="Napominalka.app", icon=icon, bundle_identifier="kz.napominalka",
        info_plist={
            "CFBundleName": "Напоминалка",
            "CFBundleDisplayName": "Напоминалка",
            "CFBundleShortVersionString": "1.0.0",
            "NSMicrophoneUsageDescription": "Микрофон нужен, чтобы надиктовать задачу голосом.",
            "NSHighResolutionCapable": True,
        },
    )
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="Napominalka",
              console=False, icon=icon, upx=False)
