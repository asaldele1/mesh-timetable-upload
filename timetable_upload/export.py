"""Выгрузка плана переноса и результатов загрузки в файл: таблица CSV (для Excel) или текст.

Формат выбирается по расширению имени файла: ``.csv`` — таблица, иначе — текстовый отчёт.
"""

from __future__ import annotations

import csv
import os

from .uploader import DAYS, ApplyResult, Plan, PlannedItem, describe_existing, item_kind

PLAN_COLUMNS = ["Неделя", "Дата", "Действие", "Класс", "День", "Урок", "Предмет", "Группа", "Учитель", "Кабинет",
                "Вид", "Группа МЭШ", "Приоритет", "id группы", "id учителя", "id кабинета", "Звонок / время", "Примечание"]
ERROR_COLUMNS = ["Неделя", "Занятие", "Ошибка"]


def _planned_row(plan: Plan, action: str, p: PlannedItem, note: str = "") -> list:
    l = p.lesson
    slot = f"{p.begin_time}–{p.end_time}" if p.kind == "ec" else p.bell_id
    return [plan.week, p.date.isoformat(), action, l.cls, DAYS[l.day], l.lesson + 1, l.subject, l.group or "",
            l.teacher or "", l.cabinet or "", "ВД" if p.kind == "ec" else "урок", p.group_name,
            f"{p.priority}" + (" (сверх часов)" if p.over_hours else ""), p.group_id,
            p.teacher_id or "", p.room_id or "", slot, "; ".join(x for x in (note, p.note) if x)]


def _existing_row(plan: Plan, action: str, it: dict) -> list:
    """Занятие, как оно есть на сервере (удаляемое): сведения ХроноГрафа для него неизвестны."""
    ec = item_kind(it) == "ec"
    d = it.get("date")
    date = f"{d[0]:04d}-{d[1]:02d}-{d[2]:02d}" if isinstance(d, list) else str(d or "")[:10]
    return [plan.week, date, action, it.get("class_unit_name") or "", "", it.get("lesson_number") or "", "", "", "", "",
            "ВД" if ec else "урок", it.get("ec_group_name" if ec else "group_name") or "", "",
            it.get("ec_group_id" if ec else "group_id") or "", it.get("teacher_id") or "", it.get("room_id") or "",
            f"{it.get('begin_time')}–{it.get('end_time')}" if ec else it.get("bell_id") or "",
            f"на сервере: учитель {it.get('teacher_name') or '—'}, кабинет {it.get('room_name') or it.get('room_id') or '—'}; id {it.get('id')}"]


def plan_rows(plans: list[Plan]) -> list[list]:
    """Строки таблицы плана: по одной на занятие (создать / удалить / отличается / к переносу / пропущено)."""
    rows: list[list] = []
    for plan in plans:
        if plan.existing or plan.to_create or plan.to_delete:      # сверено с сервером
            rows += [_planned_row(plan, "создать", p) for p in plan.to_create]
            rows += [_existing_row(plan, "удалить", it) for it in plan.to_delete]
            rows += [_planned_row(plan, "отличается — не трогаем", p,
                                  f"на сервере: учитель {cur.get('teacher_name') or '—'}, кабинет {cur.get('room_name') or cur.get('room_id') or '—'}")
                     for cur, p in plan.differs]
        else:
            rows += [_planned_row(plan, "к переносу", p) for p in plan.items]
        for l, r in plan.skipped:
            rows.append([plan.week, "", "пропущено", l.cls, DAYS[l.day], l.lesson + 1, l.subject, l.group or "",
                         l.teacher or "", l.cabinet or "", "", "", "", "", "", "", "", r])
    return rows


def plan_text(plan: Plan, verbose: bool = True) -> str:
    """Текстовый отчёт по плану недели — тот же, что печатает команда ``plan``."""
    s = [plan.summary()]
    if verbose or not plan.existing:
        for p in plan.items:
            s.append(f"  + {p.describe()}" + (f" | {p.note}" if p.note and verbose else ""))
    for l, r in plan.skipped:
        s.append(f"  ! {l.cls} {DAYS[l.day]} {l.lesson + 1} {l.subject}{('/' + str(l.group)) if l.group else ''} ({l.teacher}): {r}")
    if plan.existing:
        for cur, p in plan.differs:
            s.append(f"  ~ отличается (оставлено как есть): {p.describe()} | на сервере учитель {cur.get('teacher_name')}, "
                     f"кабинет {cur.get('room_name')}")
        for it in plan.to_delete:
            s.append(f"  - удалить: {describe_existing(it)}")
        for p in plan.to_create:
            s.append(f"  + создать: {p.describe()}")
    return "\n".join(s)


def error_rows(results: list[tuple[Plan, ApplyResult]]) -> list[list]:
    return [[plan.week, what, msg] for plan, res in results for what, msg in res.errors]


def result_text(results: list[tuple[Plan, ApplyResult]]) -> str:
    s = []
    for plan, res in results:
        s.append(f"неделя {plan.week}: создано {res.created}, удалено {res.deleted}, ошибок {len(res.errors)}"
                 + (" — остановлено, остальное не загружено" if res.stopped else ""))
        for what, msg in res.errors:
            s.append(f"    ОШИБКА {what}\n      {msg}")
    return "\n".join(s)


def is_csv(path: str) -> bool:
    return os.path.splitext(path)[1].lower() == ".csv"


def write_csv(path: str, columns: list[str], rows: list[list]) -> None:
    """CSV для Excel: UTF-8 с BOM (иначе Excel читает кириллицу как мусор) и «;» как разделитель
    (запятая в русской локали — десятичная)."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(columns)
        w.writerows(rows)


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")


def save_plans(path: str, plans: list[Plan], header: str = "") -> None:
    """План в файл: таблица (.csv) или текстовый отчёт (остальные расширения)."""
    if is_csv(path):
        write_csv(path, PLAN_COLUMNS, plan_rows(plans))
    else:
        write_text(path, "\n\n".join(([header] if header else []) + [plan_text(p) for p in plans]))


def save_results(path: str, results: list[tuple[Plan, ApplyResult]]) -> None:
    """Итоги загрузки в файл: список ошибок (.csv) или текстовый отчёт."""
    if is_csv(path):
        write_csv(path, ERROR_COLUMNS, error_rows(results))
    else:
        write_text(path, result_text(results))
