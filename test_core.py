"""Проверка логики: python test_core.py"""
from datetime import date, datetime, timedelta

import reminder_core as core

TODAY = date(2026, 11, 13)  # пятница


def d(text):
    return core.extract_date(text, TODAY)


def test_dates():
    assert d("купить хлеб завтра") == (date(2026, 11, 14), "купить хлеб")
    assert d("послезавтра сдать отчёт") == (date(2026, 11, 15), "сдать отчёт")
    assert d("отчёт 20 ноября")[0] == date(2026, 11, 20)
    assert d("отчёт к 5 января")[0] == date(2027, 1, 5)
    assert d("налоги 1 мая 2027 года")[0] == date(2027, 5, 1)
    assert d("позвонить 15.11")[0] == date(2026, 11, 15)
    assert d("позвонить 15.11.2027")[0] == date(2027, 11, 15)
    assert d("через 3 дня забрать посылку") == (date(2026, 11, 16), "забрать посылку")
    assert d("через неделю врач")[0] == date(2026, 11, 20)
    assert d("до пятницы сделать")[0] == date(2026, 11, 20)  # сегодня пятница → следующая
    assert d("в понедельник собрание") == (date(2026, 11, 16), "собрание")
    assert d("в среду")[0] == date(2026, 11, 18)
    assert d("просто текст") == (None, "просто текст")
    assert d("Ремонт 25 марта")[0] == date(2027, 3, 25)


def test_schedule():
    t = core.Task(text="x", due="2026-11-14")
    assert core.stage(t, date(2026, 11, 12)) is None
    assert core.stage(t, date(2026, 11, 13)) == "before"
    assert core.stage(t, date(2026, 11, 14)) == "due"
    assert core.stage(t, date(2026, 11, 15)) == "overdue"

    # за день: первое — сразу, следующее только через 6 часов
    now = datetime(2026, 11, 13, 9, 0)
    assert core.should_notify(t, now) == "before"
    t.last_notified = now.isoformat()
    assert core.should_notify(t, now + timedelta(hours=5, minutes=59)) is None
    assert core.should_notify(t, now + timedelta(hours=6)) == "before"
    # тихие часы
    assert core.should_notify(t, datetime(2026, 11, 13, 23, 30)) is None
    # день срока: сразу утром, даже если вчера напоминали недавно, затем каждый час
    t.last_notified = datetime(2026, 11, 13, 22, 50).isoformat()
    morning = datetime(2026, 11, 14, 7, 0)
    assert core.should_notify(t, morning) == "due"
    t.last_notified = morning.isoformat()
    assert core.should_notify(t, morning + timedelta(minutes=59)) is None
    assert core.should_notify(t, morning + timedelta(hours=1)) == "due"
    # отложено
    t.snoozed_until = (morning + timedelta(hours=3)).isoformat()
    assert core.should_notify(t, morning + timedelta(hours=2)) is None
    # выполнено — тишина
    t.snoozed_until = None
    t.done = True
    assert core.should_notify(t, morning + timedelta(hours=5)) is None


def test_store(tmp=None):
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "tasks.json"
    s = core.TaskStore(p)
    t = s.add("Тест", date(2026, 11, 20))
    assert core.TaskStore(p).get(t.id).text == "Тест"
    s.remove(t.id)
    assert core.TaskStore(p).tasks == []


if __name__ == "__main__":
    test_dates()
    test_schedule()
    test_store()
    print("OK: все тесты прошли")
