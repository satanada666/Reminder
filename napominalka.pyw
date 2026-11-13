"""Напоминалка для Windows: задачи текстом или голосом, напоминания за день (раз в 6 ч)
и в день срока (каждый час). Запуск: pythonw napominalka.pyw  [--hidden]"""
from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import tkinter as tk
import winsound
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reminder_core as core  # noqa: E402

APP_NAME = "Напоминалка"
CHECK_EVERY_MS = 30_000
STARTUP_VBS = (Path(core.os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup"
               / "Napominalka.vbs")

core.DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=core.DATA_DIR / "napominalka.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
log = logging.getLogger("napominalka")

# необязательные библиотеки — без них программа работает, просто без функции
try:
    import numpy as np
    import sounddevice as sd
    import speech_recognition as sr
    HAVE_VOICE = True
except Exception:  # pragma: no cover
    HAVE_VOICE = False
try:
    import pystray
    from PIL import Image, ImageDraw
    HAVE_TRAY = True
except Exception:  # pragma: no cover
    HAVE_TRAY = False
try:
    from winotify import Notification, audio
    HAVE_TOAST = True
except Exception:  # pragma: no cover
    HAVE_TOAST = False

STAGE_TITLE = {"before": "Завтра срок!", "due": "СЕГОДНЯ срок!", "overdue": "ПРОСРОЧЕНО!"}


def single_instance() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\NapominalkaMutex_v1")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


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
        audio_data = sr.AudioData(pcm, self.RATE, 2)
        return sr.Recognizer().recognize_google(audio_data, language="ru-RU")


# ====================================================================== приложение
class App:
    def __init__(self, start_hidden: bool):
        self.store = core.TaskStore()
        self.ui_queue: queue.Queue = queue.Queue()
        self.recorder = VoiceRecorder() if HAVE_VOICE else None
        self.recording = False
        self.open_popups: dict[str, tk.Toplevel] = {}
        self.tray = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("760x520")
        self.root.minsize(620, 400)
        self._build_ui()
        self.refresh_list()

        self.root.protocol("WM_DELETE_WINDOW", self.hide_window if HAVE_TRAY else self.quit)
        if HAVE_TRAY:
            self._start_tray()
        if start_hidden and HAVE_TRAY:
            self.root.withdraw()

        self.root.after(500, self.check_reminders)
        self.root.after(100, self._drain_queue)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        style = ttk.Style()
        style.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10))
        f = ttk.Frame(self.root, padding=10)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="Задача (можно сказать «купить хлеб завтра» или «отчёт 20 ноября»):",
                  font=("Segoe UI", 10)).grid(row=0, column=0, columnspan=4, sticky="w")
        self.text_var = tk.StringVar()
        self.entry = ttk.Entry(f, textvariable=self.text_var, font=("Segoe UI", 12))
        self.entry.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)
        self.entry.bind("<Return>", lambda e: self.add_task())
        self.entry.bind("<KeyRelease>", lambda e: self._autodate())
        self.mic_btn = ttk.Button(f, text="🎤 Сказать", command=self.toggle_voice, width=16)
        self.mic_btn.grid(row=1, column=3, sticky="ew", padx=(6, 0))
        if not HAVE_VOICE:
            self.mic_btn.state(["disabled"])

        row2 = ttk.Frame(f)
        row2.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(2, 8))
        ttk.Label(row2, text="Срок:").pack(side="left")
        self.date_var = tk.StringVar(value=(date.today() + timedelta(days=1)).strftime("%d.%m.%Y"))
        ttk.Entry(row2, textvariable=self.date_var, width=12, font=("Segoe UI", 11)).pack(side="left", padx=4)
        for label, days in (("Сегодня", 0), ("Завтра", 1), ("+3 дня", 3), ("+неделя", 7)):
            ttk.Button(row2, text=label, width=9,
                       command=lambda d=days: self.date_var.set(
                           (date.today() + timedelta(days=d)).strftime("%d.%m.%Y"))).pack(side="left", padx=2)
        ttk.Button(row2, text="➕ Добавить", command=self.add_task).pack(side="right")

        self.status_var = tk.StringVar(value="Готово." if HAVE_VOICE else
                                       "Голос недоступен: pip install sounddevice numpy SpeechRecognition")
        ttk.Label(f, textvariable=self.status_var, foreground="#555").grid(
            row=3, column=0, columnspan=4, sticky="w")

        cols = ("text", "due", "left")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", selectmode="browse")
        for c, title, w, anchor in (("text", "Задача", 420, "w"), ("due", "Срок", 100, "center"),
                                    ("left", "Осталось", 130, "center")):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "text"))
        self.tree.grid(row=4, column=0, columnspan=4, sticky="nsew", pady=6)
        self.tree.tag_configure("due", background="#ffd6d6")
        self.tree.tag_configure("overdue", background="#ff9e9e")
        self.tree.tag_configure("before", background="#fff3c4")
        self.tree.tag_configure("done", foreground="#999")
        self.tree.bind("<Double-1>", lambda e: self.mark_done())

        bottom = ttk.Frame(f)
        bottom.grid(row=5, column=0, columnspan=4, sticky="ew")
        ttk.Button(bottom, text="✔ Выполнено", command=self.mark_done).pack(side="left")
        ttk.Button(bottom, text="📅 Перенести срок", command=self.change_due).pack(side="left", padx=4)
        ttk.Button(bottom, text="🗑 Удалить", command=self.delete_task).pack(side="left")
        self.show_done = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="Показывать выполненные", variable=self.show_done,
                        command=self.refresh_list).pack(side="left", padx=12)
        self.autostart = tk.BooleanVar(value=STARTUP_VBS.exists())
        ttk.Checkbutton(bottom, text="Запускать с Windows", variable=self.autostart,
                        command=self.toggle_autostart).pack(side="right")

        f.columnconfigure(0, weight=1)
        f.rowconfigure(4, weight=1)
        self.entry.focus_set()

    def _autodate(self):
        d, _ = core.extract_date(self.text_var.get())
        if d:
            self.date_var.set(d.strftime("%d.%m.%Y"))

    def refresh_list(self):
        self.tree.delete(*self.tree.get_children())
        today = date.today()
        tasks = sorted(self.store.tasks, key=lambda t: (t.done, t.due))
        for t in tasks:
            if t.done and not self.show_done.get():
                continue
            tag = "done" if t.done else (core.stage(t, today) or "")
            self.tree.insert("", "end", iid=t.id, tags=(tag,),
                             values=(t.text, t.due_date.strftime("%d.%m.%Y"), core.human_left(t, today)))
        active = sum(1 for t in self.store.tasks if not t.done)
        self.root.title(f"{APP_NAME} — активных задач: {active}")

    def _selected(self) -> core.Task | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Выбери задачу в списке.", parent=self.root)
            return None
        return self.store.get(sel[0])

    # ------------------------------------------------------------------ действия
    def add_task(self):
        raw = self.text_var.get().strip()
        if not raw:
            return
        found, text = core.extract_date(raw)
        try:
            due = core.parse_user_date(self.date_var.get())
        except ValueError as e:
            messagebox.showerror(APP_NAME, f"Срок: {e}\nПример: 20.11.2026", parent=self.root)
            return
        if found:  # дата, сказанная в тексте, уже подставлена в поле — убираем её из текста
            raw = text or raw
        if due < date.today() and not messagebox.askyesno(
                APP_NAME, "Срок уже прошёл. Всё равно добавить?", parent=self.root):
            return
        t = self.store.add(raw, due)
        log.info("добавлена задача %s: %s до %s", t.id, t.text, t.due)
        self.text_var.set("")
        self.date_var.set((date.today() + timedelta(days=1)).strftime("%d.%m.%Y"))
        self.status_var.set(f"Добавлено: «{t.text}» — срок {due:%d.%m.%Y} ({core.human_left(t, date.today())})")
        self.refresh_list()
        self.root.after(300, self.check_reminders)

    def mark_done(self, task_id: str | None = None):
        t = self.store.get(task_id) if task_id else self._selected()
        if not t:
            return
        t.done = not t.done if not task_id else True
        self.store.save()
        self.refresh_list()

    def delete_task(self):
        t = self._selected()
        if t and messagebox.askyesno(APP_NAME, f"Удалить «{t.text}»?", parent=self.root):
            self.store.remove(t.id)
            self.refresh_list()

    def change_due(self):
        t = self._selected()
        if not t:
            return
        win = tk.Toplevel(self.root)
        win.title("Новый срок")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=t.text, wraplength=300).pack(padx=12, pady=(12, 4))
        var = tk.StringVar(value=t.due_date.strftime("%d.%m.%Y"))
        e = ttk.Entry(win, textvariable=var, font=("Segoe UI", 12), width=14)
        e.pack(padx=12, pady=4)
        e.focus_set()

        def ok(*_):
            try:
                t.due = core.parse_user_date(var.get()).isoformat()
            except ValueError as err:
                messagebox.showerror(APP_NAME, str(err), parent=win)
                return
            t.done, t.last_notified, t.snoozed_until = False, None, None
            self.store.save()
            self.refresh_list()
            win.destroy()

        e.bind("<Return>", ok)
        ttk.Button(win, text="OK", command=ok).pack(pady=(4, 12))

    def toggle_autostart(self):
        try:
            if self.autostart.get():
                pyw = Path(sys.executable).with_name("pythonw.exe")
                script = Path(__file__).resolve()
                STARTUP_VBS.write_text(
                    'CreateObject("WScript.Shell").Run """{}"" ""{}"" --hidden", 0, False\r\n'.format(pyw, script),
                    encoding="utf-16")
                self.status_var.set("Автозапуск включён.")
            else:
                STARTUP_VBS.unlink(missing_ok=True)
                self.status_var.set("Автозапуск выключен.")
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Не удалось: {e}", parent=self.root)

    # ------------------------------------------------------------------ голос
    def toggle_voice(self):
        if not self.recording:
            try:
                self.recorder.start()
            except Exception as e:
                messagebox.showerror(APP_NAME, f"Микрофон не открылся:\n{e}", parent=self.root)
                return
            self.recording = True
            self.mic_btn.configure(text="⏹ Стоп")
            self.status_var.set("🔴 Говорите... затем нажмите «Стоп».")
        else:
            self.recording = False
            self.mic_btn.configure(text="⏳ Распознаю...")
            self.mic_btn.state(["disabled"])
            threading.Thread(target=self._recognize_worker, daemon=True).start()

    def _recognize_worker(self):
        try:
            text = self.recorder.stop_and_recognize()
            self.ui_queue.put(("voice_ok", text))
        except sr.UnknownValueError:
            self.ui_queue.put(("voice_err", "Не расслышал, попробуй ещё раз."))
        except sr.RequestError as e:
            self.ui_queue.put(("voice_err", f"Нет связи с сервисом распознавания (нужен интернет): {e}"))
        except Exception as e:
            log.exception("voice")
            self.ui_queue.put(("voice_err", f"Ошибка: {e}"))

    def _on_voice(self, kind: str, payload: str):
        self.mic_btn.state(["!disabled"])
        self.mic_btn.configure(text="🎤 Сказать")
        if kind == "voice_err":
            self.status_var.set(payload)
            return
        text = payload[:1].upper() + payload[1:]
        self.text_var.set(text)
        d, _ = core.extract_date(text)
        if d:
            self.date_var.set(d.strftime("%d.%m.%Y"))
            self.status_var.set(f"Распознано, срок {d:%d.%m.%Y}. Проверь и нажми «Добавить» (Enter).")
        else:
            self.status_var.set("Распознано. Срок не услышал — выбери его и нажми «Добавить».")
        self.entry.focus_set()
        self.entry.icursor("end")

    # ------------------------------------------------------------------ напоминания
    def check_reminders(self):
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
            self.refresh_list()
        except Exception:
            log.exception("check_reminders")
        self.root.after(CHECK_EVERY_MS, self.check_reminders)

    def notify(self, t: core.Task, st: str):
        title = STAGE_TITLE[st]
        body = f"{t.text}\nСрок: {t.due_date:%d.%m.%Y}"
        if HAVE_TOAST:
            try:
                n = Notification(app_id=APP_NAME, title=title, msg=body, duration="long")
                n.set_audio(audio.Reminder, loop=False)
                n.show()
            except Exception:
                log.exception("toast")
        else:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        self._popup(t, title, st)

    def _popup(self, t: core.Task, title: str, st: str):
        win = tk.Toplevel(self.root)
        self.open_popups[t.id] = win
        win.title(f"{APP_NAME}: {title}")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        color = {"before": "#fff3c4", "due": "#ffd6d6", "overdue": "#ff9e9e"}[st]
        win.configure(bg=color)
        tk.Label(win, text=title, font=("Segoe UI", 16, "bold"), bg=color).pack(padx=20, pady=(16, 4))
        tk.Label(win, text=t.text, font=("Segoe UI", 13), bg=color, wraplength=420,
                 justify="center").pack(padx=20, pady=4)
        nxt = "через 6 часов" if st == "before" else "через час"
        tk.Label(win, text=f"Срок: {t.due_date:%d.%m.%Y}   •   следующее напоминание {nxt}",
                 font=("Segoe UI", 9), bg=color, fg="#444").pack(padx=20, pady=(0, 10))

        def close():
            self.open_popups.pop(t.id, None)
            win.destroy()

        def done():
            self.mark_done(t.id)
            close()

        def snooze(hours: int):
            t.snoozed_until = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
            self.store.save()
            close()

        btns = tk.Frame(win, bg=color)
        btns.pack(pady=(0, 16))
        ttk.Button(btns, text="✔ Выполнено", command=done).pack(side="left", padx=4)
        ttk.Button(btns, text="Отложить на 3 ч", command=lambda: snooze(3)).pack(side="left", padx=4)
        ttk.Button(btns, text="OK", command=close).pack(side="left", padx=4)
        win.protocol("WM_DELETE_WINDOW", close)

        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        x = win.winfo_screenwidth() - w - 30
        y = win.winfo_screenheight() - h - 80 - 30 * (len(self.open_popups) - 1)
        win.geometry(f"+{x}+{max(y, 0)}")
        win.lift()
        win.focus_force()

    # ------------------------------------------------------------------ трей
    def _start_tray(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((4, 4, 60, 60), fill=(220, 60, 50))
        d.rectangle((29, 14, 35, 34), fill="white")
        d.rectangle((29, 34, 46, 40), fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Открыть", lambda: self.ui_queue.put(("show", None)), default=True),
            pystray.MenuItem("Выход", lambda: self.ui_queue.put(("quit", None))),
        )
        self.tray = pystray.Icon("napominalka", img, APP_NAME, menu)
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
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App(start_hidden="--hidden" in sys.argv).run()


if __name__ == "__main__":
    main()
