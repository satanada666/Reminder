"""Всё, что зависит от ОС: один экземпляр, звук, системное уведомление, автозапуск.
Windows / Linux / macOS."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
FROZEN = getattr(sys, "frozen", False)          # запущено из собранного exe/app

# окно консоли не мигает при вызове git/powershell из pythonw/exe
NO_WINDOW = {"creationflags": 0x08000000} if IS_WIN else {}

_lock_handle = None


def single_instance(name: str = "NapominalkaMutex_v1") -> bool:
    """True, если это первый запущенный экземпляр."""
    global _lock_handle
    if IS_WIN:
        import ctypes
        _lock_handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\" + name)
        return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    import fcntl
    lock = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"{name}.lock"
    _lock_handle = open(lock, "w")
    try:
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def show_error_box(title: str, text: str):
    if IS_WIN:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    else:
        print(f"{title}: {text}", file=sys.stderr)


def enable_dpi_awareness():
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def beep():
    try:
        if IS_WIN:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        elif IS_MAC:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
        elif shutil.which("paplay"):
            subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"],
                             stderr=subprocess.DEVNULL)
        else:
            print("\a", end="", flush=True)
    except Exception:
        pass


def toast(app_name: str, title: str, msg: str, icon: str = "") -> bool:
    """Системное уведомление ОС. Возвращает True, если удалось (со звуком)."""
    try:
        if IS_WIN:
            from winotify import Notification, audio
            n = Notification(app_id=app_name, title=title, msg=msg, duration="short", icon=icon)
            n.set_audio(audio.Reminder, loop=False)
            n.show()
            return True
        if IS_MAC:
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{esc(msg)}" with title "{esc(title)}" '
                              f'subtitle "{esc(app_name)}" sound name "Glass"'])
            return True
        if shutil.which("notify-send"):
            args = ["notify-send", "-a", app_name, "-u", "critical", title, msg]
            if icon:
                args[1:1] = ["-i", icon]
            subprocess.Popen(args)
            beep()
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------- автозапуск
def launch_command() -> list[str]:
    """Как запустить программу свёрнутой в трей."""
    if FROZEN:
        exe = Path(sys.executable)
        if IS_MAC and ".app/Contents/MacOS" in str(exe):
            return ["/usr/bin/open", "-a", str(exe.parents[2]), "--args", "--hidden"]
        return [str(exe), "--hidden"]
    py = Path(sys.executable)
    if IS_WIN and py.with_name("pythonw.exe").exists():
        py = py.with_name("pythonw.exe")
    return [str(py), str(Path(__file__).resolve().with_name("napominalka.pyw")), "--hidden"]


def _autostart_path() -> Path:
    if IS_WIN:
        return (Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup"
                / "Napominalka.vbs")
    if IS_MAC:
        return Path.home() / "Library/LaunchAgents/kz.napominalka.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart/napominalka.desktop"


def autostart_enabled() -> bool:
    return _autostart_path().exists()


def set_autostart(enable: bool):
    p = _autostart_path()
    if not enable:
        p.unlink(missing_ok=True)
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    cmd = launch_command()
    if IS_WIN:
        line = " ".join(f'""{c}""' if not c.startswith("--") else c for c in cmd)
        p.write_text(f'CreateObject("WScript.Shell").Run "{line}", 0, False\r\n', encoding="utf-16")
    elif IS_MAC:
        args = "".join(f"<string>{c}</string>" for c in cmd)
        p.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>kz.napominalka</string>
<key>ProgramArguments</key><array>{args}</array>
<key>RunAtLoad</key><true/>
</dict></plist>
""", encoding="utf-8")
    else:
        exec_line = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        p.write_text(f"""[Desktop Entry]
Type=Application
Name=Напоминалка
Exec={exec_line}
X-GNOME-Autostart-enabled=true
""", encoding="utf-8")


# ---------------------------------------------------------------- git
def git_root(path: Path) -> Path | None:
    if not shutil.which("git"):
        return None
    try:
        out = subprocess.run(["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10, **NO_WINDOW)
        return Path(out.stdout.strip()) if out.returncode == 0 else None
    except Exception:
        return None


def git_sync(data_file: Path) -> str:
    """git add + commit + push файла с задачами. Возвращает текст для статуса."""
    root = git_root(data_file.parent)
    if not root:
        return "⚠ Папка с задачами не в git-репозитории."

    def run(*args, timeout=60):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout, **NO_WINDOW)

    rel = data_file.resolve().relative_to(root.resolve()).as_posix()
    run("add", rel)
    if run("diff", "--cached", "--quiet", "--", rel).returncode == 0:
        msg = "Изменений в задачах нет"
    else:
        from datetime import datetime
        r = run("commit", "-m", f"Задачи: {datetime.now():%d.%m.%Y %H:%M}", "--", rel)
        if r.returncode != 0:
            return "⚠ git commit: " + (r.stderr or r.stdout).strip()[-200:]
        msg = "Задачи закоммичены"
    r = run("push", timeout=120)
    if r.returncode != 0:
        return f"{msg}, но push не прошёл: " + r.stderr.strip()[-200:]
    if msg.startswith("Изменений"):
        return "☁ Изменений нет — на GitHub всё актуально."
    return f"☁ {msg} и отправлены на GitHub."
