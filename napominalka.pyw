"""Напоминалка для Windows: задачи текстом или голосом, напоминания за день (раз в 6 ч)
и в день срока (каждый час). Запуск: pythonw napominalka.pyw  [--hidden]"""
from __future__ import annotations

import ctypes
import json
import logging
import queue
import sys
import threading
import tkinter as tk
import winsound
from datetime import date, datetime, timedelta
from pathlib import Path

import customtkinter as ctk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reminder_core as core  # noqa: E402

APP_NAME = "Напоминалка"
CHECK_EVERY_MS = 30_000
STARTUP_VBS = (Path(core.os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup"
               / "Napominalka.vbs")
SETTINGS_FILE = core.DATA_DIR / "settings.json"
ICON_FILE = core.DATA_DIR / "icon.ico"

core.DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=core.DATA_DIR / "napominalka.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
log = logging.getLogger("napominalka")

try:
    import numpy as np
    import sounddevice as sd
    import speech_recognition as sr
    HAVE_VOICE = True
except Exception:  # pragma: no cover
    HAVE_VOICE = False
try:
    import pystray
    HAVE_TRAY = True
except Exception:  # pragma: no cover
    HAVE_TRAY = False
try:
    from winotify import Notification, audio
    HAVE_TOAST = True
except Exception:  # pragma: no cover
    HAVE_TOAST = False
from PIL import Image, ImageDraw

# ====================================================================== палитра (светлая, тёмная)
BG = ("#F2F3F7", "#15161B")
CARD = ("#FFFFFF", "#21232A")
CARD_HOVER = ("#F8F9FC", "#272A32")
BORDER = ("#E3E5EE", "#2F323B")
TEXT = ("#1A1C23", "#ECEDF2")
MUTED = ("#6E7383", "#969CAB")
FIELD = ("#F5F6FA", "#1A1C22")
ACCENT = ("#4F6BFF", "#6A80FF")
ACCENT_HOVER = ("#3E58E8", "#5870F0")
GHOST_HOVER = ("#ECEEF4", "#2C2F38")
SEG = dict(fg_color=("#E6E8EF", "#2A2D35"), unselected_color=("#E6E8EF", "#2A2D35"),
           unselected_hover_color=("#DCDFE8", "#323641"), selected_color=("#FFFFFF", "#454A57"),
           selected_hover_color=("#FFFFFF", "#4E5463"), text_color=("#1A1C23", "#ECEDF2"))
GREEN = ("#16A34A", "#22C55E")
GREEN_HOVER = ("#13883E", "#1DAE52")
RED = "#EF4444"

STATUS_STYLE = {  # стадия: (цвет полоски/текста, фон плашки)
    "overdue": (("#DC2626", "#F87171"), ("#FDE2E2", "#4A2228")),
    "due": (("#E5484D", "#FF6B6F"), ("#FDECEC", "#3D2329")),
    "before": (("#D97706", "#FBBF24"), ("#FEF3DC", "#3B3120")),
    "later": (("#5B6275", "#A3A9B8"), ("#EEF0F5", "#2B2E37")),
    "done": (("#16A34A", "#4ADE80"), ("#E3F6EA", "#1E3327")),
}
POPUP_TITLE = {"before": "ЗАВТРА СРОК", "due": "СЕГОДНЯ СРОК", "overdue": "ПРОСРОЧЕНО"}
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\NapominalkaMutex_v1")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(s: dict):
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def pretty_date(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}" + ("" if d.year == date.today().year else f" {d.year}")


def make_icon(size: int = 256) -> Image.Image:
    """Колокольчик на скруглённом синем квадрате."""
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 4, fill=(79, 107, 255))
    w = (255, 255, 255)
    d.ellipse((s * .30, s * .20, s * .70, s * .60), fill=w)            # купол
    d.rectangle((s * .30, s * .40, s * .70, s * .66), fill=w)          # тело
    d.rounded_rectangle((s * .22, s * .62, s * .78, s * .70), radius=s * .04, fill=w)  # юбка
    d.ellipse((s * .44, s * .70, s * .56, s * .82), fill=w)            # язычок
    d.ellipse((s * .46, s * .13, s * .54, s * .21), fill=w)            # ушко
    return img


def mode_idx() -> int:
    return 1 if ctk.get_appearance_mode() == "Dark" else 0


# ====================================================================== голос
class VoiceRecorder:
    RATE = 16000

    def __init__(self):
        self.frames: list = []
        self.stream = None

    def start(self):
        self.frames = []
        self.stream = sd.InputStream(samplerate=self.RATE, channels=1, dtype="int16",
                                     callback=lambda data, *_: self.frames.append(data.copy()))
        self.stream.start()

    def stop_and_recognize(self) -> str:
        self.stream.stop()
        self.stream.close()
        self.stream = None
        if not self.frames:
            return ""
        pcm = np.concatenate(self.frames).tobytes()
        return sr.Recognizer().recognize_google(sr.AudioData(pcm, self.RATE, 2), language="ru-RU")


# ====================================================================== карточка задачи
class TaskCard(ctk.CTkFrame):
    def __init__(self, master, app: "App", t: core.Task, today: date, fonts: dict):
        super().__init__(master, corner_radius=14, fg_color=CARD, border_width=1, border_color=BORDER)
        st = "done" if t.done else (core.stage(t, today) or "later")
        color, pill_bg = STATUS_STYLE[st]

        ctk.CTkFrame(self, width=4, height=10, corner_radius=2, fg_color=color).grid(
            row=0, column=0, rowspan=2, sticky="ns", padx=(10, 0), pady=12)

        check = ctk.CTkButton(
            self, text="✓" if t.done else "", width=26, height=26, corner_radius=13,
            border_width=2, border_color=color, font=fonts["check"],
            fg_color=color if t.done else "transparent", hover_color=GHOST_HOVER if not t.done else color,
            text_color=("#FFFFFF", "#15161B"), command=lambda: app.toggle_done(t.id))
        check.grid(row=0, column=1, rowspan=2, padx=(12, 10), pady=12)

        title = ctk.CTkLabel(self, text=t.text, font=fonts["task_done"] if t.done else fonts["task"],
                             text_color=MUTED if t.done else TEXT, anchor="w", justify="left",
                             wraplength=380)
        title.grid(row=0, column=2, sticky="sw", pady=(10, 0))
        ctk.CTkLabel(self, text=f"до {pretty_date(t.due_date)}, {WEEKDAYS[t.due_date.weekday()].lower()}",
                     font=fonts["meta"], text_color=MUTED, anchor="w").grid(
            row=1, column=2, sticky="nw", pady=(0, 10))

        ctk.CTkLabel(self, text=core.human_left(t, today), font=fonts["pill"], text_color=color,
                     fg_color=pill_bg, corner_radius=10, height=24).grid(
            row=0, column=3, rowspan=2, padx=8, ipadx=8)

        tools = ctk.CTkFrame(self, fg_color="transparent")
        tools.grid(row=0, column=4, rowspan=2, padx=(0, 10))
        for sym, tip, cmd in (("⟲", "Перенести срок", lambda: app.change_due(t.id)),
                              ("✕", "Удалить", lambda: app.delete_task(t.id))):
            ctk.CTkButton(tools, text=sym, width=32, height=32, corner_radius=10, font=fonts["tool"],
                          fg_color="transparent", hover_color=GHOST_HOVER, text_color=MUTED,
                          command=cmd).pack(side="left", padx=1)
        self.grid_columnconfigure(2, weight=1)

        for w in (self, title):
            w.bind("<Enter>", lambda e: self.configure(fg_color=CARD_HOVER))
            w.bind("<Leave>", lambda e: self.configure(fg_color=CARD))


# ====================================================================== всплывающее напоминание
class ReminderPopup(tk.Toplevel):
    WIDTH = 400

    def __init__(self, app: "App", t: core.Task, st: str, slot: int):
        super().__init__(app.root)
        self.app, self.t = app, t
        mi = mode_idx()
        # прозрачный цвет = цвет рамки ±1, чтобы сглаженные углы не давали ореол
        rgb = [int(BORDER[mi][i:i + 2], 16) for i in (1, 3, 5)]
        key = "#%02x%02x%02x" % (rgb[0], rgb[1], rgb[2] + 1 if rgb[2] < 255 else 254)
        self.overrideredirect(True)
        self.configure(bg=key)
        self.attributes("-transparentcolor", key, "-topmost", True, "-alpha", 0.0)

        color, pill_bg = STATUS_STYLE[st]
        f = app.fonts
        card = ctk.CTkFrame(self, corner_radius=18, fg_color=CARD, bg_color=key,
                            border_width=1, border_color=BORDER)
        card.pack(fill="both", expand=True)
        # распорка задаёт ширину (CTk сам масштабирует её под DPI экрана)
        ctk.CTkFrame(card, width=self.WIDTH, height=1, corner_radius=0, fg_color=CARD).pack(padx=18, pady=(2, 0))

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(16, 0))
        ctk.CTkLabel(head, text=POPUP_TITLE[st], font=f["pill"], text_color=color, fg_color=pill_bg,
                     corner_radius=10, height=24).pack(side="left", ipadx=8)
        ctk.CTkLabel(head, text=f"  🔔 {APP_NAME}", font=f["meta"], text_color=MUTED).pack(side="left")
        ctk.CTkButton(head, text="✕", width=28, height=28, corner_radius=8, font=f["tool"],
                      fg_color="transparent", hover_color=GHOST_HOVER, text_color=MUTED,
                      command=self.close).pack(side="right")

        ctk.CTkLabel(card, text=t.text, font=f["popup_title"], text_color=TEXT, anchor="w",
                     justify="left", wraplength=self.WIDTH - 40).pack(fill="x", padx=20, pady=(12, 2))
        nxt = "через 6 часов" if st == "before" else "через час"
        ctk.CTkLabel(card, text=f"Срок: {pretty_date(t.due_date)}  ·  снова напомню {nxt}",
                     font=f["meta"], text_color=MUTED, anchor="w").pack(fill="x", padx=20)

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=18, pady=(14, 18))
        ctk.CTkButton(btns, text="✓  Выполнено", height=38, corner_radius=12, font=f["button"],
                      fg_color=GREEN, hover_color=GREEN_HOVER, command=self.done).pack(
            side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(btns, text="Отложить 3 ч", height=38, corner_radius=12, font=f["button"],
                      fg_color=FIELD, hover_color=GHOST_HOVER, text_color=TEXT, border_width=1,
                      border_color=BORDER, command=lambda: self.snooze(3)).pack(
            side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(btns, text="ОК", width=64, height=38, corner_radius=12, font=f["button"],
                      fg_color="transparent", hover_color=GHOST_HOVER, text_color=TEXT,
                      command=self.close).pack(side="left")

        # перетаскивание мышью
        for w in (card, head):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = self.winfo_screenwidth() - w - 24
        y = self.winfo_screenheight() - 72 - (h + 12) * (slot + 1)
        self.geometry(f"+{x}+{max(y, 8)}")
        self._fade(0.0)

    def _fade(self, a: float):
        a = min(a + 0.12, 1.0)
        self.attributes("-alpha", a)
        if a < 1.0:
            self.after(16, self._fade, a)

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.winfo_x(), e.y_root - self.winfo_y()

    def _drag_move(self, e):
        self.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def close(self):
        self.app.open_popups.pop(self.t.id, None)
        self.destroy()

    def done(self):
        self.app.set_done(self.t.id, True)
        self.close()

    def snooze(self, hours: int):
        self.t.snoozed_until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
        self.app.store.save()
        self.close()


# ====================================================================== приложение
class App:
    def __init__(self, start_hidden: bool):
        self.settings = load_settings()
        ctk.set_appearance_mode(self.settings.get("theme", "system"))
        ctk.set_default_color_theme("blue")

        self.store = core.TaskStore()
        self.ui_queue: queue.Queue = queue.Queue()
        self.recorder = VoiceRecorder() if HAVE_VOICE else None
        self.recording = False
        self.open_popups: dict[str, ReminderPopup] = {}
        self.tray = None
        self.filter = "Активные"
        self._list_sig = None
        self._undo: core.Task | None = None
        self._undo_job = None

        self.root = ctk.CTk(fg_color=BG)
        self.root.title(APP_NAME)
        self.root.geometry("760x680")
        self.root.minsize(600, 520)
        try:
            if not ICON_FILE.exists():
                make_icon().save(ICON_FILE, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
            self.root.iconbitmap(str(ICON_FILE))
        except Exception:
            log.exception("icon")

        self.fonts = {
            "h1": ctk.CTkFont("Segoe UI", 26, "bold"),
            "sub": ctk.CTkFont("Segoe UI", 13),
            "h2": ctk.CTkFont("Segoe UI", 16, "bold"),
            "entry": ctk.CTkFont("Segoe UI", 15),
            "task": ctk.CTkFont("Segoe UI", 14),
            "task_done": ctk.CTkFont("Segoe UI", 14, overstrike=True),
            "meta": ctk.CTkFont("Segoe UI", 12),
            "pill": ctk.CTkFont("Segoe UI", 11, "bold"),
            "button": ctk.CTkFont("Segoe UI", 13, "bold"),
            "small": ctk.CTkFont("Segoe UI", 12),
            "tool": ctk.CTkFont("Segoe UI Symbol", 15),
            "check": ctk.CTkFont("Segoe UI Symbol", 13, "bold"),
            "mic": ctk.CTkFont("Segoe UI Emoji", 18),
            "popup_title": ctk.CTkFont("Segoe UI", 17, "bold"),
        }
        self._build_ui()
        self.render_list(force=True)

        self.root.protocol("WM_DELETE_WINDOW", self.hide_window if HAVE_TRAY else self.quit)
        if HAVE_TRAY:
            self._start_tray()
        if start_hidden and HAVE_TRAY:
            self.root.withdraw()

        self.root.after(800, self.check_reminders)
        self.root.after(100, self._drain_queue)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        f = self.fonts
        pad = 22

        # шапка
        head = ctk.CTkFrame(self.root, fg_color="transparent")
        head.pack(fill="x", padx=pad, pady=(18, 10))
        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(left, text=APP_NAME, font=f["h1"], text_color=TEXT).pack(anchor="w")
        self.sub_lbl = ctk.CTkLabel(left, text="", font=f["sub"], text_color=MUTED)
        self.sub_lbl.pack(anchor="w")
        theme_names = {"system": "Авто", "light": "Светлая", "dark": "Тёмная"}
        self.theme_seg = ctk.CTkSegmentedButton(
            head, values=list(theme_names.values()), font=f["small"], height=30, corner_radius=10, **SEG,
            command=lambda v: self.set_theme({n: k for k, n in theme_names.items()}[v]))
        self.theme_seg.set(theme_names[self.settings.get("theme", "system")])
        self.theme_seg.pack(side="right")

        # карточка ввода
        card = ctk.CTkFrame(self.root, corner_radius=18, fg_color=CARD, border_width=1, border_color=BORDER)
        card.pack(fill="x", padx=pad)
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(16, 10))
        self.entry = ctk.CTkEntry(
            row, height=48, corner_radius=14, font=f["entry"],
            fg_color=FIELD, border_color=BORDER, border_width=1, text_color=TEXT,
            placeholder_text="Что нужно сделать?  Например: «сдать отчёт 20 ноября»",
            placeholder_text_color=MUTED)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda e: self.add_task())
        self.entry.bind("<KeyRelease>", lambda e: self._autodate())
        self.mic_btn = ctk.CTkButton(row, text="🎤", width=48, height=48, corner_radius=24, font=f["mic"],
                                     fg_color=FIELD, hover_color=GHOST_HOVER, text_color=TEXT,
                                     border_width=1, border_color=BORDER, command=self.toggle_voice)
        self.mic_btn.pack(side="left", padx=(10, 0))
        if not HAVE_VOICE:
            self.mic_btn.configure(state="disabled")

        row2 = ctk.CTkFrame(card, fg_color="transparent")
        row2.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkLabel(row2, text="Срок", font=f["small"], text_color=MUTED).pack(side="left", padx=(2, 8))
        self.date_var = tk.StringVar(value=(date.today() + timedelta(days=1)).strftime("%d.%m.%Y"))
        ctk.CTkEntry(row2, textvariable=self.date_var, width=118, height=36, corner_radius=10,
                     font=f["task"], fg_color=FIELD, border_color=BORDER, border_width=1,
                     text_color=TEXT, justify="center").pack(side="left")
        self.quick = ctk.CTkSegmentedButton(
            row2, values=["Сегодня", "Завтра", "+3 дня", "Неделя"], font=f["small"], height=36,
            corner_radius=10, command=self._quick_date, **SEG)
        self.quick.pack(side="left", padx=10)
        self.quick.set("Завтра")
        ctk.CTkButton(row2, text="Добавить", height=36, width=120, corner_radius=10, font=f["button"],
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.add_task).pack(side="right")

        status_row = ctk.CTkFrame(card, fg_color="transparent")
        status_row.pack(fill="x", padx=18, pady=(0, 12))
        self.status_lbl = ctk.CTkLabel(
            status_row, font=f["meta"], text_color=MUTED, anchor="w",
            text="Нажми 🎤 и скажи задачу со сроком, или напиши её — дата подставится сама."
            if HAVE_VOICE else "Голос недоступен: pip install sounddevice numpy SpeechRecognition")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.undo_btn = ctk.CTkButton(status_row, text="Отменить", width=90, height=24, corner_radius=8,
                                      font=f["pill"], fg_color="transparent", text_color=ACCENT,
                                      hover_color=GHOST_HOVER, command=self.undo_delete)

        # заголовок списка + фильтр
        lh = ctk.CTkFrame(self.root, fg_color="transparent")
        lh.pack(fill="x", padx=pad, pady=(18, 6))
        ctk.CTkLabel(lh, text="Задачи", font=f["h2"], text_color=TEXT).pack(side="left")
        self.filter_seg = ctk.CTkSegmentedButton(lh, values=["Активные", "Выполненные", "Все"],
                                                 font=f["small"], height=30, corner_radius=10,
                                                 command=self._set_filter, **SEG)
        self.filter_seg.set(self.filter)
        self.filter_seg.pack(side="right")

        self.list_frame = ctk.CTkScrollableFrame(self.root, fg_color="transparent", corner_radius=0)
        self.list_frame.pack(fill="both", expand=True, padx=(pad - 4, pad - 14))
        self.list_frame.grid_columnconfigure(0, weight=1)

        # низ
        foot = ctk.CTkFrame(self.root, fg_color="transparent")
        foot.pack(fill="x", padx=pad, pady=(6, 14))
        self.autostart = tk.BooleanVar(value=STARTUP_VBS.exists())
        ctk.CTkSwitch(foot, text="Запускать вместе с Windows", variable=self.autostart, font=f["small"],
                      text_color=TEXT, progress_color=ACCENT, command=self.toggle_autostart).pack(side="left")
        ctk.CTkLabel(foot, text=f"Тихие часы {core.QUIET_FROM}:00–{core.QUIET_TO:02d}:00",
                     font=f["meta"], text_color=MUTED).pack(side="right")
        self.entry.focus_set()

    def set_status(self, text: str):
        self.status_lbl.configure(text=text)

    def set_theme(self, mode: str):
        ctk.set_appearance_mode(mode)
        self.settings["theme"] = mode
        save_settings(self.settings)

    def _quick_date(self, v: str):
        days = {"Сегодня": 0, "Завтра": 1, "+3 дня": 3, "Неделя": 7}[v]
        self.date_var.set((date.today() + timedelta(days=days)).strftime("%d.%m.%Y"))

    def _set_filter(self, v: str):
        self.filter = v
        self.render_list(force=True)

    def _autodate(self):
        d, _ = core.extract_date(self.entry.get())
        if d:
            self.date_var.set(d.strftime("%d.%m.%Y"))
            self.quick.set("")

    def render_list(self, force: bool = False):
        today = date.today()
        tasks = sorted(self.store.tasks, key=lambda t: (t.done, t.due))
        if self.filter == "Активные":
            tasks = [t for t in tasks if not t.done]
        elif self.filter == "Выполненные":
            tasks = [t for t in tasks if t.done]
        sig = (self.filter, today, tuple((t.id, t.text, t.due, t.done) for t in tasks))
        active = sum(1 for t in self.store.tasks if not t.done)
        hot = sum(1 for t in self.store.tasks if core.stage(t, today) in ("due", "overdue"))
        sub = f"{WEEKDAYS[today.weekday()]}, {pretty_date(today)}  ·  активных: {active}"
        if hot:
            sub += f"  ·  горит: {hot}"
        self.sub_lbl.configure(text=sub)
        if sig == self._list_sig and not force:
            return
        self._list_sig = sig
        for w in self.list_frame.winfo_children():
            w.destroy()
        if not tasks:
            msg = {"Активные": "Все дела сделаны 🎉\nДобавь новую задачу сверху",
                   "Выполненные": "Пока ничего не выполнено", "Все": "Задач пока нет"}[self.filter]
            ctk.CTkLabel(self.list_frame, text=msg, font=self.fonts["sub"], text_color=MUTED,
                         justify="center").grid(row=0, column=0, pady=60)
            return
        for i, t in enumerate(tasks):
            TaskCard(self.list_frame, self, t, today, self.fonts).grid(
                row=i, column=0, sticky="ew", pady=4, padx=(4, 8))

    # ------------------------------------------------------------------ действия
    def add_task(self):
        raw = self.entry.get().strip()
        if not raw:
            self.entry.focus_set()
            return
        found, text = core.extract_date(raw)
        try:
            due = core.parse_user_date(self.date_var.get())
        except ValueError:
            self.set_status("⚠ Не понял срок. Пример: 20.11.2026, «завтра», «в пятницу».")
            return
        if found:
            raw = text or raw
        raw = raw[:1].upper() + raw[1:]
        t = self.store.add(raw, due)
        log.info("добавлена задача %s: %s до %s", t.id, t.text, t.due)
        self.entry.delete(0, "end"); self.root.focus(); self.entry.focus_set()
        self.date_var.set((date.today() + timedelta(days=1)).strftime("%d.%m.%Y"))
        self.quick.set("Завтра")
        self.set_status(f"✓ Добавлено: «{t.text}» — {pretty_date(due)} ({core.human_left(t, date.today())})")
        if self.filter == "Выполненные":
            self.filter_seg.set("Активные")
            self.filter = "Активные"
        self.render_list(force=True)
        self.root.after(300, self.check_reminders_once)

    def set_done(self, task_id: str, value: bool):
        t = self.store.get(task_id)
        if t:
            t.done = value
            self.store.save()
            self.render_list(force=True)

    def toggle_done(self, task_id: str):
        t = self.store.get(task_id)
        if t:
            self.set_done(task_id, not t.done)
            if t.done:
                self.set_status(f"✓ Готово: «{t.text}»")

    def delete_task(self, task_id: str):
        t = self.store.get(task_id)
        if not t:
            return
        self.store.remove(task_id)
        self._undo = t
        self.set_status(f"Удалено: «{t.text}»")
        self.undo_btn.pack(side="right")
        if self._undo_job:
            self.root.after_cancel(self._undo_job)
        self._undo_job = self.root.after(8000, self._hide_undo)
        self.render_list(force=True)

    def undo_delete(self):
        if self._undo:
            self.store.tasks.append(self._undo)
            self.store.save()
            self.set_status(f"Восстановлено: «{self._undo.text}»")
            self._undo = None
        self._hide_undo()
        self.render_list(force=True)

    def _hide_undo(self):
        self._undo_job = None
        self._undo = None
        self.undo_btn.pack_forget()

    def change_due(self, task_id: str):
        t = self.store.get(task_id)
        if not t:
            return
        dlg = ctk.CTkInputDialog(title="Перенести срок",
                                 text=f"«{t.text}»\n\nНовый срок (20.11.2026, завтра, в пятницу…):",
                                 font=self.fonts["task"], button_fg_color=ACCENT,
                                 button_hover_color=ACCENT_HOVER)
        val = dlg.get_input()
        if not val:
            return
        try:
            t.due = core.parse_user_date(val).isoformat()
        except ValueError:
            self.set_status(f"⚠ Не понял дату «{val}».")
            return
        t.done, t.last_notified, t.snoozed_until = False, None, None
        self.store.save()
        self.set_status(f"Срок перенесён на {pretty_date(t.due_date)}.")
        self.render_list(force=True)

    def toggle_autostart(self):
        try:
            if self.autostart.get():
                pyw = Path(sys.executable).with_name("pythonw.exe")
                script = Path(__file__).resolve()
                STARTUP_VBS.write_text(
                    'CreateObject("WScript.Shell").Run """{}"" ""{}"" --hidden", 0, False\r\n'.format(pyw, script),
                    encoding="utf-16")
                self.set_status("Автозапуск включён.")
            else:
                STARTUP_VBS.unlink(missing_ok=True)
                self.set_status("Автозапуск выключен.")
        except OSError as e:
            self.set_status(f"⚠ Автозапуск: {e}")

    # ------------------------------------------------------------------ голос
    def toggle_voice(self):
        if not self.recording:
            try:
                self.recorder.start()
            except Exception as e:
                self.set_status(f"⚠ Микрофон не открылся: {e}")
                return
            self.recording = True
            self.mic_btn.configure(text="■", fg_color=RED, hover_color="#DC2626", text_color="#FFFFFF",
                                   border_color=RED)
            self.set_status("🔴 Говорите… потом нажмите ■")
        else:
            self.recording = False
            self.mic_btn.configure(text="…", state="disabled")
            self.set_status("Распознаю…")
            threading.Thread(target=self._recognize_worker, daemon=True).start()

    def _recognize_worker(self):
        try:
            self.ui_queue.put(("voice_ok", self.recorder.stop_and_recognize()))
        except sr.UnknownValueError:
            self.ui_queue.put(("voice_err", "Не расслышал, попробуй ещё раз."))
        except sr.RequestError as e:
            self.ui_queue.put(("voice_err", f"⚠ Нет связи с распознаванием (нужен интернет): {e}"))
        except Exception as e:
            log.exception("voice")
            self.ui_queue.put(("voice_err", f"⚠ Ошибка: {e}"))

    def _on_voice(self, kind: str, payload: str):
        self.mic_btn.configure(text="🎤", state="normal", fg_color=FIELD, hover_color=GHOST_HOVER,
                               text_color=TEXT, border_color=BORDER)
        if kind == "voice_err":
            self.set_status(payload)
            return
        text = payload[:1].upper() + payload[1:]
        self.entry.delete(0, "end"); self.entry.insert(0, text)
        d, _ = core.extract_date(text)
        if d:
            self.date_var.set(d.strftime("%d.%m.%Y"))
            self.quick.set("")
            self.set_status(f"Распознано, срок — {pretty_date(d)}. Проверь и нажми Enter.")
        else:
            self.set_status("Распознано. Срок не услышал — выбери его и нажми Enter.")
        self.entry.focus_set()
        self.entry.icursor("end")

    # ------------------------------------------------------------------ напоминания
    def check_reminders_once(self):
        now = datetime.now()
        try:
            for t in self.store.tasks:
                st = core.should_notify(t, now)
                if st and t.id not in self.open_popups:
                    t.last_notified = now.isoformat(timespec="seconds")
                    t.snoozed_until = None
                    self.store.save()
                    log.info("напоминание %s (%s): %s", t.id, st, t.text)
                    self.notify(t, st)
            self.render_list()
        except Exception:
            log.exception("check_reminders")

    def check_reminders(self):
        self.check_reminders_once()
        self.root.after(CHECK_EVERY_MS, self.check_reminders)

    def notify(self, t: core.Task, st: str):
        if HAVE_TOAST:
            try:
                n = Notification(app_id=APP_NAME, title=POPUP_TITLE[st].capitalize(),
                                 msg=f"{t.text}\nСрок: {pretty_date(t.due_date)}", duration="short",
                                 icon=str(ICON_FILE) if ICON_FILE.exists() else "")
                n.set_audio(audio.Reminder, loop=False)
                n.show()
            except Exception:
                log.exception("toast")
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        else:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        self.open_popups[t.id] = ReminderPopup(self, t, st, slot=len(self.open_popups))

    # ------------------------------------------------------------------ трей
    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Открыть", lambda: self.ui_queue.put(("show", None)), default=True),
            pystray.MenuItem("Выход", lambda: self.ui_queue.put(("quit", None))),
        )
        self.tray = pystray.Icon("napominalka", make_icon(64), APP_NAME, menu)
        self.tray.run_detached()

    def hide_window(self):
        self.root.withdraw()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.entry.focus_set()

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "show":
                    self.show_window()
                elif kind == "quit":
                    self.quit()
                    return
                elif kind.startswith("voice"):
                    self._on_voice(kind, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def run(self):
        self.root.mainloop()


def main():
    if not single_instance():
        ctypes.windll.user32.MessageBoxW(None, "Напоминалка уже запущена (значок в трее).", APP_NAME, 0x40)
        return
    App(start_hidden="--hidden" in sys.argv).run()


if __name__ == "__main__":
    main()
