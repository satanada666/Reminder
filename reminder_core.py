"""Логика напоминалки без GUI: хранение задач, разбор дат из речи/текста, расписание."""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- настройки
INTERVAL_DAY_BEFORE = timedelta(hours=6)   # за 1 день до срока — раз в 6 часов
INTERVAL_DUE_DAY = timedelta(hours=1)      # в день срока — каждый час
INTERVAL_OVERDUE = timedelta(hours=1)      # просрочено — тоже каждый час, пока не отметишь
QUIET_FROM = 23                            # тихие часы: с 23:00 ...
QUIET_TO = 7                               # ... до 07:00 напоминаний нет

APP_DIR = Path(__file__).resolve().parent


def _user_data_dir() -> Path:
    import sys
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home())) / "Napominalka"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Napominalka"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "napominalka"


# Из исходников задачи лежат в папке проекта (data/), чтобы их коммитить в git.
# Собранный exe/app хранит их в папке пользователя ОС.
# Переменная NAPOMINALKA_DATA задаёт любую другую папку (нужно для тестов).
if os.environ.get("NAPOMINALKA_DATA"):
    DATA_DIR = Path(os.environ["NAPOMINALKA_DATA"])
elif getattr(__import__("sys"), "frozen", False):
    DATA_DIR = _user_data_dir()
else:
    DATA_DIR = APP_DIR / "data"
DATA_FILE = DATA_DIR / "tasks.json"
_OLD_DATA_FILE = Path(os.environ.get("APPDATA", Path.home())) / "Napominalka" / "tasks.json"
if "NAPOMINALKA_DATA" not in os.environ and not DATA_FILE.exists() and _OLD_DATA_FILE.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)       # перенос со старого места
    DATA_FILE.write_bytes(_OLD_DATA_FILE.read_bytes())


# ---------------------------------------------------------------- модель
@dataclass
class Task:
    text: str
    due: str                                # YYYY-MM-DD
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    done: bool = False
    last_notified: str | None = None        # ISO datetime последнего напоминания
    snoozed_until: str | None = None        # ISO datetime, до которого молчим

    @property
    def due_date(self) -> date:
        return date.fromisoformat(self.due)


class TaskStore:
    def __init__(self, path: Path = DATA_FILE):
        self.path = path
        self.tasks: list[Task] = []
        self.load()

    def load(self):
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.tasks = [Task(**t) for t in raw]

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(t) for t in self.tasks], ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def add(self, text: str, due: date) -> Task:
        t = Task(text=text.strip(), due=due.isoformat())
        self.tasks.append(t)
        self.save()
        return t

    def get(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def remove(self, task_id: str):
        self.tasks = [t for t in self.tasks if t.id != task_id]
        self.save()


# ---------------------------------------------------------------- расписание
def stage(task: Task, today: date) -> str | None:
    """None — рано напоминать; 'before' — завтра срок; 'due' — сегодня; 'overdue' — просрочено."""
    if task.done:
        return None
    d = (task.due_date - today).days
    if d == 1:
        return "before"
    if d == 0:
        return "due"
    if d < 0:
        return "overdue"
    return None


INTERVALS = {"before": INTERVAL_DAY_BEFORE, "due": INTERVAL_DUE_DAY, "overdue": INTERVAL_OVERDUE}


def is_quiet(now: datetime) -> bool:
    h = now.hour
    if QUIET_FROM > QUIET_TO:
        return h >= QUIET_FROM or h < QUIET_TO
    return QUIET_FROM <= h < QUIET_TO


def should_notify(task: Task, now: datetime) -> str | None:
    """Возвращает стадию, если пора напомнить, иначе None."""
    st = stage(task, now.date())
    if st is None or is_quiet(now):
        return None
    if task.snoozed_until and now < datetime.fromisoformat(task.snoozed_until):
        return None
    if task.last_notified:
        last = datetime.fromisoformat(task.last_notified)
        # новая стадия (например, наступил сам день срока) — напоминаем сразу
        if stage(task, last.date()) == st and now - last < INTERVALS[st]:
            return None
    return st


def human_left(task: Task, today: date) -> str:
    if task.done:
        return "выполнено"
    d = (task.due_date - today).days
    if d < 0:
        return f"просрочено на {-d} дн."
    if d == 0:
        return "СЕГОДНЯ"
    if d == 1:
        return "завтра"
    return f"через {d} дн."


# ---------------------------------------------------------------- разбор даты
MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTH_RE = (r"(января|январь|февраля|февраль|марта|март|апреля|апрель|мая|май|июня|июнь|"
             r"июля|июль|августа|август|сентября|сентябрь|октября|октябрь|ноября|ноябрь|"
             r"декабря|декабрь)")
WEEKDAYS = [
    (r"понедельник\w*", 0), (r"вторник\w*", 1), (r"сред[аеуы]", 2), (r"четверг\w*", 3),
    (r"пятниц[аеуы]", 4), (r"суббот[аеуы]", 5), (r"воскресень[еяю]", 6),
]
NUM_WORDS = {"один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
             "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
_PREP = r"(?:\b(?:в|во|к|ко|до|на)\s+)?"


def _month_num(word: str) -> int:
    w = word.lower()
    for stem, n in sorted(MONTHS.items(), key=lambda kv: -len(kv[0])):
        if w.startswith(stem):
            return n
    raise ValueError(word)


def _future(d_day: int, d_month: int, year: int | None, today: date) -> date:
    if year:
        return date(year, d_month, d_day)
    cand = date(today.year, d_month, d_day)
    return cand if cand >= today else date(today.year + 1, d_month, d_day)


def extract_date(text: str, today: date | None = None) -> tuple[date | None, str]:
    """Ищет в тексте дату ('завтра', '20 ноября', '15.11', 'в пятницу', 'через 3 дня').
    Возвращает (дата или None, текст без найденной фразы)."""
    today = today or date.today()
    patterns: list[tuple[str, callable]] = [
        (_PREP + r"\bпослезавтра\b", lambda m: today + timedelta(days=2)),
        (_PREP + r"\bзавтра\b", lambda m: today + timedelta(days=1)),
        (_PREP + r"\bсегодня\b", lambda m: today),
        (r"\bчерез\s+(\d+|" + "|".join(NUM_WORDS) + r")?\s*(день|дня|дней|недел[юиь])\b",
         lambda m: today + timedelta(
             days=(int(m.group(1)) if (m.group(1) or "").isdigit() else NUM_WORDS.get(m.group(1) or "", 1))
             * (7 if m.group(2).startswith("недел") else 1))),
        (_PREP + r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b",
         lambda m: _future(int(m.group(1)), int(m.group(2)),
                           (int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)) if m.group(3) else None,
                           today)),
        (_PREP + r"\b(\d{1,2})(?:-?го|-?е)?\s+" + _MONTH_RE + r"(?:\s+(\d{4})(?:\s*(?:года|г\.?))?)?",
         lambda m: _future(int(m.group(1)), _month_num(m.group(2)),
                           int(m.group(3)) if m.group(3) else None, today)),
    ]
    for wd_re, wd in WEEKDAYS:
        patterns.append((_PREP + r"\b" + wd_re + r"\b",
                         lambda m, wd=wd: today + timedelta(days=((wd - today.weekday()) % 7) or 7)))

    for pat, fn in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if not m:
            continue
        try:
            d = fn(m)
        except ValueError:
            continue
        rest = (text[:m.start()] + " " + text[m.end():])
        rest = re.sub(r"\s{2,}", " ", rest).strip(" ,.-")
        return d, rest
    return None, text.strip()


def parse_user_date(s: str, today: date | None = None) -> date:
    """Разбор того, что введено в поле 'Срок': 15.11.2026, 15.11, завтра, пятница..."""
    today = today or date.today()
    s = s.strip()
    if not s:
        raise ValueError("пустая дата")
    d, _ = extract_date(s, today)
    if d is None:
        raise ValueError(f"не понимаю дату: {s}")
    return d
