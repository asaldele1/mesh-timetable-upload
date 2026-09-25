"""Построение плана переноса расписания на неделю МЭШ и его выполнение.

План строится без обращения к серверу (по кэшу справочников и файлу
сопоставления), затем сравнивается с уже существующими занятиями недели и
применяется: лишние удаляются, недостающие создаются.  Повторный запуск с
теми же данными ничего не меняет.
"""

from __future__ import annotations

import datetime as _dt
import fnmatch
import json
import logging
import os
import threading
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from . import chngf
from .mapping import GroupResolver, Mapping, is_ec, relevant_lessons
from .mesh_api import MeshClient, MeshError
from .refdata import RefData, norm_class, parse_ru_date

log = logging.getLogger(__name__)
DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ---------------------------------------------------------------------- что переносим
def select_classes(spec: str | None, tt: chngf.Timetable) -> list[str] | None:
    """Классы ХроноГрафа по описанию: «5а,5б» — имена, «5» — вся параллель, «10*» — шаблон;
    через запятую.  None/пусто — все классы."""
    if not spec or not spec.strip():
        return None
    names = [c.name for c in tt.classes]
    by_norm = {norm_class(n): n for n in names}
    out: list[str] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        key = norm_class(tok)
        if key in by_norm:
            found = [by_norm[key]]
        elif key.isdigit():
            found = [n for k, n in by_norm.items() if k.startswith(key) and not k[len(key):len(key) + 1].isdigit()]
        elif any(ch in key for ch in "*?["):
            found = [n for k, n in by_norm.items() if fnmatch.fnmatchcase(k, key)]
        else:
            found = []
        if not found:
            raise ValueError(f"класс {tok!r} не найден в файле (есть: {', '.join(names)})")
        out.extend(n for n in found if n not in out)
    return out


_DAY_NAMES = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}


def parse_days(spec: str | None) -> set[int] | None:
    """Дни недели: «пн,ср», «1-3», «пн-пт» → индексы с 0 (понедельник).  None — все."""
    if not spec or not spec.strip():
        return None

    def one(tok: str) -> int:
        tok = tok.strip().lower().replace("ё", "е")
        if tok in _DAY_NAMES:
            return _DAY_NAMES[tok]
        if tok.isdigit() and 1 <= int(tok) <= 7:
            return int(tok) - 1
        raise ValueError(f"день недели {tok!r} не распознан (пн…вс или 1…7)")

    out: set[int] = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(one(a), one(b) + 1))
        elif part.strip():
            out.add(one(part))
    return out


def parse_dates(spec: str | None) -> set[_dt.date] | None:
    """Даты: «2027-05-24», «24.05.2027», диапазон «2027-05-24..2027-05-28», через запятую."""
    if not spec or not spec.strip():
        return None

    def one(tok: str) -> _dt.date:
        tok = tok.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
            try:
                return _dt.datetime.strptime(tok, fmt).date()
            except ValueError:
                pass
        raise ValueError(f"дата {tok!r} не распознана (ГГГГ-ММ-ДД или ДД.ММ.ГГГГ)")

    out: set[_dt.date] = set()
    for part in spec.split(","):
        if ".." in part:
            a, b = (one(x) for x in part.split("..", 1))
            out.update(a + _dt.timedelta(days=i) for i in range((b - a).days + 1))
        elif part.strip():
            out.add(one(part))
    return out


@dataclass
class Selection:
    """Что именно переносить: классы, дни недели, конкретные даты, вид занятий."""
    classes: list[str] | None = None      # имена классов ХроноГрафа; None — все
    days: set[int] | None = None          # 0 = понедельник; None — все
    dates: set[_dt.date] | None = None    # None — все даты выбранных недель
    only: str | None = None               # "oo" — только уроки, "ec" — только ВД

    def weeks(self, week1_monday: _dt.date) -> list[int]:
        """Недели, в которые попадают выбранные даты."""
        return sorted({(d - week1_monday).days // 7 + 1 for d in self.dates or ()})

    def weeks_for(self, weeks: list[int] | None, week1_monday: _dt.date) -> list[int]:
        """Недели к переносу: заданные явно и/или недели выбранных дат (пересечение, если есть и то
        и другое); пустой список — ничего не выбрано."""
        if self.dates:
            by_dates = self.weeks(week1_monday)
            return [w for w in by_dates if w in weeks] if weeks else by_dates
        return list(weeks or [])

    def week_dates(self, monday: _dt.date) -> set[_dt.date]:
        """Даты недели, которые входят в выбор (по ним же ограничивается удаление при --replace)."""
        out = {monday + _dt.timedelta(days=i) for i in range(7)
               if self.days is None or i in self.days}
        if self.dates is not None:
            out &= self.dates
        return out


def week1_monday_for(tt: chngf.Timetable | None, override: _dt.date | None = None,
                     academic_year: dict | None = None) -> _dt.date:
    """Понедельник первой учебной недели: заданный явно; иначе неделя с датой начала учебного года
    МЭШ (academic_year из кэша); иначе неделя с 1 сентября по учебному году файла."""
    if override:
        return override
    begin = parse_ru_date((academic_year or {}).get("begin_date"))
    if begin:
        return begin - _dt.timedelta(days=begin.weekday())
    if tt is None:
        raise ValueError("не загружен файл расписания")
    year = None
    if tt.period_begin:
        year = tt.period_begin.year if tt.period_begin.month >= 8 else tt.period_begin.year - 1
    elif tt.teach_year and tt.teach_year[:4].isdigit():
        year = int(tt.teach_year[:4])
    if year is None:
        raise ValueError("не удалось определить учебный год; задайте week1_monday явно")
    sep1 = _dt.date(year, 9, 1)
    return sep1 - _dt.timedelta(days=sep1.weekday())


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@dataclass
class PlannedItem:
    lesson: chngf.Lesson
    date: _dt.date
    class_unit_id: int
    class_unit_name: str
    group_id: int
    group_name: str
    bell_id: int
    teacher_id: int | None
    room_id: int | None
    building_id: int | None
    note: str = ""
    room_guessed: bool = False  # кабинет подобран по умолчанию, а не сопоставлен: при сравнении с сервером не учитывается
    kind: str = "oo"            # "oo" — урок (группа предмета), "ec" — занятие внеурочной деятельности
    begin_time: str = ""        # только для ВД: время по звонку класса
    end_time: str = ""
    duration: str = ""          # «00:45»
    priority: int = 1           # приоритет названия предмета МЭШ, по которому выбрана группа (см. build_plan)
    over_hours: bool = False    # часы группы на неделе исчерпаны у всех приоритетов — поставлено сверх часов

    @property
    def key(self) -> tuple[str, int, str, int | str]:
        if self.kind == "ec":
            return ("ec", self.group_id, self.date.isoformat(), self.begin_time)
        return ("oo", self.group_id, self.date.isoformat(), self.bell_id)

    def describe(self) -> str:
        l = self.lesson
        slot = f"{self.begin_time}–{self.end_time}" if self.kind == "ec" else f"звонок {self.bell_id}"
        return (f"{l.cls} {DAYS[l.day]} {l.lesson + 1}: {l.subject}{('/' + str(l.group)) if l.group else ''} — "
                f"{l.teacher or '—'} [{l.cabinet or '—'}] → {'группа ВД' if self.kind == 'ec' else 'группа'} "
                f"{self.group_id} «{self.group_name}», учитель {self.teacher_id or '—'}, "
                f"кабинет {self.room_id or '—'}, {slot}, {self.date}"
                + (f" [приоритет {self.priority}]" if self.priority > 1 else "") + (" [сверх часов]" if self.over_hours else ""))


@dataclass
class Plan:
    week: int
    schedule_id: int
    ec_schedule_id: int | None = None        # расписание ВД недели; None — на сервере ещё не создано
    dates: set[_dt.date] = field(default_factory=set)   # даты недели, охваченные планом
    only: str | None = None                  # "oo"/"ec" — план охватывает только уроки / только ВД
    cycle_week: int = 0                      # неделя цикла ХроноГрафа (с 0), взятая для этой недели МЭШ
    cycle_weeks: int = 1                     # число недель в цикле файла
    items: list[PlannedItem] = field(default_factory=list)
    skipped: list[tuple[chngf.Lesson, str]] = field(default_factory=list)
    class_unit_ids: set[int] = field(default_factory=set)
    # заполняется после сравнения с сервером
    existing: list[dict] = field(default_factory=list)
    to_create: list[PlannedItem] = field(default_factory=list)
    to_delete: list[dict] = field(default_factory=list)
    differs: list[tuple[dict, PlannedItem]] = field(default_factory=list)   # есть на сервере, но с другим учителем/кабинетом
    unchanged: int = 0

    def summary(self) -> str:
        reasons = Counter(r.split(":")[0] for _, r in self.skipped)
        n_ec = sum(1 for p in self.items if p.kind == "ec")
        span = ""
        if self.dates:
            lo, hi = min(self.dates), max(self.dates)
            span = f", {lo}" + (f" — {hi}" if hi != lo else "") + (f" ({len(self.dates)} дн.)" if len(self.dates) < 7 else "")
        cycle = f", {self.cycle_week + 1}-я неделя цикла" if self.cycle_weeks > 1 else ""
        n_prio = sum(1 for p in self.items if p.priority > 1)
        n_over = sum(1 for p in self.items if p.over_hours)
        s = [f"неделя {self.week}{span}{cycle} (schedule_id {self.schedule_id}, ВД {self.ec_schedule_id or 'не создано'}): "
             f"занятий к переносу {len(self.items)} (уроков {len(self.items) - n_ec}, ВД {n_ec}"
             + (f", по 2-му и далее приоритету предмета {n_prio}" if n_prio else "")
             + (f", сверх часов группы {n_over}" if n_over else "") + f"), пропущено {len(self.skipped)}"]
        for r, n in reasons.most_common():
            s.append(f"    пропуск — {r}: {n}")
        if self.existing or self.to_create or self.to_delete:
            s.append(f"  на сервере сейчас {len(self.existing)}; создать {len(self.to_create)}, "
                     f"удалить {len(self.to_delete)}, без изменений {self.unchanged}, отличаются (не трогаем) {len(self.differs)}")
        return "\n".join(s)


def default_room(rd: RefData, teacher_id: int | None, building_id: int | None) -> tuple[int | None, int | None]:
    """Кабинет по умолчанию — повторяет логику веб-интерфейса МЭШ: здания учителя (сначала здание
    расписания звонков класса), в них — кабинет учителя, иначе первый кабинет здания; в крайнем
    случае — первый кабинет в списке."""
    rooms = [r for r in rd.rooms if not r.get("deleted_at")]
    t = rd.teacher_by_id.get(teacher_id) or {}
    buildings = [b["id"] for b in t.get("buildings") or [] if b.get("id")]
    buildings.sort(key=lambda b: 0 if b == building_id else 1)
    own = [rd.room_by_id[i] for i in t.get("room_ids") or [] if i in rd.room_by_id]
    for pool in (own, rooms):
        for b in buildings:
            r = next((r for r in pool if r.get("building_id") == b), None)
            if r:
                return r["id"], b
    if rooms:
        return rooms[0]["id"], rooms[0].get("building_id")
    return None, None


def build_plan(tt: chngf.Timetable, rd: RefData, m: Mapping, week: int,
               week1_monday: _dt.date, sel: Selection | None = None,
               existing: list[dict] | None = None, replace: bool = False) -> Plan:
    """План на неделю МЭШ.  Для файла с циклом из нескольких недель берётся неделя цикла по номеру
    недели МЭШ: неделя 1 — первая неделя цикла, 2 — вторая и т. д. по кругу.

    Предмету может соответствовать несколько групп МЭШ по приоритету (см. ``GroupResolver.resolve_chain``):
    уроки недели по порядку (пн 1-й урок → сб последний) идут в группу 1-го приоритета, пока не исчерпаны
    её часы на неделе по нагрузке учителя, затем — 2-го и т. д.; если часы исчерпаны у всех, урок встаёт
    в группу 1-го приоритета сверх часов.  Часы считаются по всей неделе, даже когда план ограничен датами
    или видом занятий.  ``existing`` — занятия недели на сервере (``fetch_existing``): те из них, что
    останутся после загрузки (все при ``replace=False``; при ``replace=True`` — вне дат плана), тоже
    занимают часы своих групп.  День вне плана, в который у класса на сервере остаются занятия этого
    вида, уже загружен и сейчас не меняется: его часы берутся с сервера, а уроки файла в этот день часов
    не занимают (иначе загруженная ранее версия дня и версия из файла считались бы вместе).
    """
    sel = sel or Selection()
    sched = rd.schedule_by_week.get(week)
    if not sched:
        raise ValueError(f"в кэше нет расписания для недели {week} (есть недели {min(rd.schedule_by_week)}–{max(rd.schedule_by_week)})")
    ec_sched = rd.ec_schedule_by_week.get(week)
    monday = week1_monday + _dt.timedelta(days=7 * (week - 1))
    cycle_weeks = max(1, tt.week_count)
    plan = Plan(week=week, schedule_id=sched["id"], ec_schedule_id=ec_sched["id"] if ec_sched else None,
                dates=sel.week_dates(monday), only=sel.only,
                cycle_week=(week - 1) % cycle_weeks, cycle_weeks=cycle_weeks)
    seen: dict[tuple, PlannedItem] = {}
    lessons = relevant_lessons(tt, m)
    if sel.classes is not None:
        wanted = set(sel.classes)
        lessons = [l for l in lessons if l.cls in wanted]
    # вся неделя цикла в хронологическом порядке: часы групп считаются по всей неделе, а не по выбранным датам
    lessons = sorted((l for l in lessons if l.week == plan.cycle_week), key=lambda l: (l.day, l.lesson))
    sel_cu_ids = {cu["id"] for l in lessons if (cu := rd.class_unit(m.classes.get(l.cls) or "")) is not None}
    # часы, уже занятые на сервере занятиями, которые останутся; их ключи — чтобы не считать их дважды,
    # когда план попадает в тот же слот
    used: Counter = Counter()
    on_server: set[tuple] = set()
    loaded_days: set[tuple[int, str, str]] = set()     # (класс, дата, вид), где на сервере остаются занятия
    for it in existing or []:
        k = _item_key(it)
        our = _dt.date.fromisoformat(k[2]) in plan.dates and (plan.only is None or k[0] == plan.only)
        cus = _item_classes(it, rd)
        if replace and our and cus and cus <= sel_cu_ids:
            continue                   # будет удалено (или совпадёт с планом и посчитается при постановке)
        on_server.add(k)
        used[k[1]] += 1
        loaded_days.update((cu_id, k[2], k[0]) for cu_id in cus)
    bells_cache: dict[tuple[int, _dt.date], dict | None] = {}
    resolver = GroupResolver(tt, rd, m)

    def in_scope(date: _dt.date, kind: str | None = None) -> bool:
        return date in plan.dates and (kind is None or sel.only is None or kind == sel.only)

    def skip(l: chngf.Lesson, date: _dt.date, why: str, kind: str | None = None) -> None:
        if in_scope(date, kind):
            plan.skipped.append((l, why))

    for l in lessons:
        date = monday + _dt.timedelta(days=l.day)
        cu_name = m.classes.get(l.cls)
        cu = rd.class_unit(cu_name) if cu_name else None
        if not cu:
            skip(l, date, "класс не сопоставлен")
            continue
        teacher_id = m.teachers.get(l.teacher) if l.teacher else None
        if teacher_id is not None and rd.teacher_deleted(teacher_id):    # сервер удалённый профиль не примет
            if not resolver.any_forced(l):
                skip(l, date, f"учитель удалён в МЭШ: {rd.teacher_by_id[teacher_id].get('name')} (id {teacher_id})")
                continue
            teacher_id = None                    # занятия поставит принудительно заданный учитель
        chain = resolver.resolve_chain(l)
        if chain[0].group is None:
            skip(l, date, f"группа не найдена: {chain[0].why}")
            continue
        if (cu["id"], date) not in bells_cache:
            bells_cache[(cu["id"], date)] = rd.bells_timetable_for(cu["id"], date)
        bt = bells_cache[(cu["id"], date)]
        if not bt:
            skip(l, date, "нет расписания звонков: классу не назначено расписание звонков на эту дату")
            continue
        bell = rd.bell_for(bt, l.day + 1, l.lesson + 1)
        if not bell:
            skip(l, date, f"нет звонка: урок {l.lesson + 1} отсутствует в расписании звонков «{bt.get('name')}» "
                          f"({DAYS[l.day]}: уроки {rd.lesson_numbers(bt, l.day + 1)})")
            continue
        begin, end = bell.get("begin_time") or "", bell.get("end_time") or ""
        # выбор звена: первое, у которого учитель привязан к группе и есть часы (или занятие уже стоит на
        # сервере); если часы исчерпаны у всех — первое подходящее по учителю, сверх часов
        chosen = fallback = None
        not_attached, exhausted = [], []
        for link in chain:
            g = link.group
            if g is None:
                continue
            # сервер принимает только учителя, привязанного к группе (удалённые из списка группы уже
            # убраны): берём сопоставленного, принудительно заданного или единственного учителя группы
            tid, note = resolver.teacher_for(l, link)
            if tid is None:
                names = ", ".join(t.get("full_name") or "?" for t in g.get("teachers") or []) or "нет учителей"
                not_attached.append(f"«{g['name']}» — {names}")
                continue
            why = link.why + (f"; {note}" if note else "")
            key = ("ec", g["id"], date.isoformat(), begin) if is_ec(g) else ("oo", g["id"], date.isoformat(), bell["id"])
            cap = rd.group_hours(g, week, tid)
            # слот уже занят потоком (другой класс той же метагруппы) — то же занятие, часы не тратятся
            if key in on_server or key in seen or cap is None or used[g["id"]] < cap:
                chosen = (link, tid, why, key)
                break
            fallback = fallback or (link, tid, why, key)
            exhausted.append(f"«{g['name']}» ({cap} ч)")
        if chosen is None and fallback is None:
            if not_attached:
                skip(l, date, ("учитель не сопоставлен" if teacher_id is None else "учитель не привязан к группе МЭШ")
                     + ": в группе " + "; ".join(not_attached)
                     + " (можно привязать принудительно на вкладке «Группы» или через force_teachers)")
            else:
                skip(l, date, "группа не найдена: " + "; ".join(lk.why for lk in chain if lk.group is None))
            continue
        over_hours = chosen is None
        link, teacher_id, why, key = chosen or fallback
        g = link.group
        if over_hours:
            why += f"; сверх часов: на неделе исчерпаны {', '.join(exhausted)}"
        elif link.prio > 1:
            why += f"; приоритет {link.prio} «{link.name}»: на неделе исчерпаны {', '.join(exhausted)}"
        room_id = m.rooms.get(l.cabinet) if l.cabinet else None
        room_guessed = False
        if room_id:
            building_id = rd.room_by_id[room_id]["building_id"]
        elif is_ec(g):
            # для ВД кабинет не обязателен, а занятость кабинета сервер проверяет — не подставляем
            room_id = building_id = None
            if l.cabinet:
                why += "; кабинет не сопоставлен — занятие без кабинета"
        else:
            # сервер требует кабинет; как и веб-интерфейс, берём кабинет учителя в здании класса,
            # иначе первый кабинет этого здания.  Это догадка: с кабинетом на сервере не сравнивается
            room_id, building_id = default_room(rd, teacher_id, bt.get("building_id"))
            if room_id is None:
                skip(l, date, "нет кабинета: не сопоставлен и не удалось подобрать по умолчанию", "oo")
                continue
            room_guessed = True
            why += ("; кабинет не сопоставлен" if l.cabinet else "; кабинет не задан") + \
                   f" — подставлен «{rd.room_by_id[room_id].get('name')}»"
        item = PlannedItem(l, date, cu["id"], cu["name"], g["id"], g["name"], bell["id"],
                           teacher_id, room_id, building_id, why, room_guessed,
                           priority=link.prio, over_hours=over_hours)
        if is_ec(g):
            # занятие ВД задаётся временем, а не звонком: берём границы звонка класса
            minutes = _minutes(end) - _minutes(begin) if begin and end else 0
            if minutes not in m.ec_durations:
                skip(l, date, f"длительность звонка не подходит для ВД: {begin}–{end} ({minutes} мин; "
                              f"допустимо {'/'.join(map(str, m.ec_durations))})", "ec")
                continue
            item.kind, item.begin_time, item.end_time, item.duration = "ec", begin, end, f"00:{minutes:02d}"
        if item.key in seen:
            # поток: несколько классов ведут на одну метагруппу — одно занятие
            continue
        seen[item.key] = item
        # занятие с сервера в этом слоте уже посчитано; день вне плана, уже загруженный на сервер, посчитан
        # по серверу целиком
        if item.key not in on_server and (in_scope(date, item.kind)
                                          or (cu["id"], date.isoformat(), item.kind) not in loaded_days):
            used[g["id"]] += 1
        if not in_scope(date, item.kind):
            continue
        plan.items.append(item)
        plan.class_unit_ids.add(cu["id"])
    return plan


def plan_week(tt: chngf.Timetable, rd: RefData, m: Mapping, week: int, week1_monday: _dt.date,
              sel: Selection | None = None, client: MeshClient | None = None, replace: bool = False) -> Plan:
    """План недели; с ``client`` — сверенный с сервером (``diff_plan``), причём занятия, которые на
    сервере останутся, учтены в часах групп."""
    plan = build_plan(tt, rd, m, week, week1_monday, sel)
    if client is None:
        return plan
    existing = fetch_existing(client, plan)
    if existing:
        ec_id = plan.ec_schedule_id          # уточнён на сервере в fetch_existing
        plan = build_plan(tt, rd, m, week, week1_monday, sel, existing, replace)
        plan.ec_schedule_id = ec_id
    diff_plan(plan, existing, replace, rd)
    return plan


def item_kind(it: dict) -> str:
    return "ec" if "ec_group_id" in it else "oo"


def _item_key(it: dict) -> tuple[str, int, str, int | str]:
    d = it.get("date")
    if isinstance(d, list):
        ds = f"{d[0]:04d}-{d[1]:02d}-{d[2]:02d}"
    else:
        ds = str(d)[:10]
    if item_kind(it) == "ec":
        return ("ec", it["ec_group_id"], ds, it["begin_time"])
    return ("oo", it["group_id"], ds, it["bell_id"])


def _item_classes(it: dict, rd: RefData) -> set[int]:
    """Классы, к которым относится занятие на сервере."""
    if item_kind(it) == "ec":
        return set(it.get("class_unit_ids") or [])
    g = rd.group_by_id.get(it["group_id"])
    if g:
        return set(g.get("class_unit_ids") or []) | ({g["class_unit_id"]} if g.get("class_unit_id") else set())
    return {it["class_unit_id"]} if it.get("class_unit_id") else set()


def describe_existing(it: dict) -> str:
    if item_kind(it) == "ec":
        return (f"ВД {it.get('date')} {it.get('begin_time')}–{it.get('end_time')} {it.get('ec_group_name')} "
                f"({it.get('teacher_name')}, кабинет {it.get('room_id')}) id {it['id']}")
    return (f"{it.get('class_unit_name')} {it.get('date')} урок {it.get('lesson_number')} {it.get('group_name')} "
            f"({it.get('teacher_name')}, {it.get('room_name')}) id {it['id']}")


def diff_plan(plan: Plan, existing: list[dict], replace: bool, rd: RefData) -> None:
    """Сравнивает план с занятиями недели на сервере.

    replace=False — только добавлять недостающие; существующие занятия не трогаются
    (если у них другой учитель/кабинет — попадают в plan.differs как предупреждение);
    replace=True — кроме того, удалять занятия наших классов на охваченные планом даты,
    которых нет в плане, и пересоздавать отличающиеся.  Кабинет сравнивается только
    сопоставленный: подставленный по умолчанию (room_guessed) — не повод трогать занятие.
    """
    # занятия недели вне выбранных дат (и другого вида, если план только для уроков/ВД) к плану не относятся
    existing = [it for it in existing if _dt.date.fromisoformat(_item_key(it)[2]) in plan.dates
                and (plan.only is None or item_kind(it) == plan.only)]
    plan.existing = existing
    by_key = {_item_key(it): it for it in existing}
    plan.to_create, plan.to_delete, plan.differs, plan.unchanged = [], [], [], 0
    planned_keys = set()
    for p in plan.items:
        planned_keys.add(p.key)
        cur = by_key.get(p.key)
        if cur is None:
            plan.to_create.append(p)
        elif cur.get("teacher_id") != p.teacher_id or (p.room_id and not p.room_guessed and cur.get("room_id") != p.room_id):
            if replace:
                plan.to_delete.append(cur)
                plan.to_create.append(p)
            else:
                plan.differs.append((cur, p))
        else:
            plan.unchanged += 1
    if replace:
        for it in existing:
            if _item_key(it) in planned_keys:
                continue
            cus = _item_classes(it, rd)
            if cus and cus <= plan.class_unit_ids:
                plan.to_delete.append(it)


def sync_ec_schedule(client: MeshClient, plan: Plan) -> None:
    """Берёт id расписания ВД недели с сервера, а не из кэша справочников.

    Сайт пересоздаёт расписания недель ВД — id меняется, и занятие, созданное по устаревшему id, попадает
    в «висящее» расписание: в сетке недели его не видно, но учитель, кабинет и ученики им заняты, а
    выборка занятий по актуальному расписанию его не находит (удалить такое можно только по журналу).
    """
    live = [s for s in client.ec_schedules() or [] if s.get("week_number") == plan.week
            and s.get("academic_year_id") in (None, client.ay)]
    new_id = max(s["id"] for s in live) if live else None
    if len(live) > 1:
        log.warning("расписаний ВД недели %s на сервере несколько: %s — берём %s",
                    plan.week, ", ".join(str(s["id"]) for s in live), new_id)
    if new_id != plan.ec_schedule_id:
        log.warning("расписание ВД недели %s: на сервере id %s, в кэше справочников %s — используем серверный",
                    plan.week, new_id, plan.ec_schedule_id)
    plan.ec_schedule_id = new_id


def fetch_existing(client: MeshClient, plan: Plan) -> list[dict]:
    """Занятия недели на сервере по классам плана: уроки и занятия ВД."""
    if not plan.class_unit_ids:
        return []
    cu_ids = sorted(plan.class_unit_ids)
    items = client.schedule_items(plan.week, cu_ids)
    sync_ec_schedule(client, plan)
    if plan.ec_schedule_id:
        items += client.ec_schedule_items(plan.ec_schedule_id, cu_ids)
    return [it for it in items if not it.get("cancelled")]


@dataclass
class ApplyResult:
    created: int = 0
    deleted: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False      # прервано по should_stop — часть плана не выполнена


StopCheck = Callable[[], bool]      # «пора остановиться?» — опрашивается перед каждым запросом к серверу


ItemKey = tuple[str, int]      # (вид занятия "oo"/"ec", id на сервере): у уроков и занятий ВД разные нумерации


def journal_key(rec: dict) -> ItemKey:
    return (rec.get("kind") or "oo", rec.get("item_id"))


class Journal:
    """Журнал созданных занятий (JSON Lines) — по нему :func:`undo` удаляет ровно то, что создали мы."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()       # запись из нескольких потоков (workers)

    def record(self, plan: Plan, item: PlannedItem, item_id: int) -> None:
        rec = {"created_at": _dt.datetime.now().isoformat(timespec="seconds"), "kind": item.kind,
               "week": plan.week, "schedule_id": plan.ec_schedule_id if item.kind == "ec" else plan.schedule_id,
               "item_id": item_id,
               "class": item.lesson.cls, "class_unit_id": item.class_unit_id, "group_id": item.group_id,
               "group_name": item.group_name, "date": item.date.isoformat(), "lesson": item.lesson.lesson + 1,
               "teacher_id": item.teacher_id, "room_id": item.room_id}
        if item.kind == "ec":
            rec["begin_time"] = item.begin_time
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def remove(self, keys: set[ItemKey]) -> None:
        self.rewrite([r for r in self.read() if journal_key(r) not in keys])

    def rewrite(self, recs: list[dict]) -> None:
        """Заменить содержимое журнала на recs."""
        with self._lock, open(self.path, "w", encoding="utf-8") as f:
            for rec in recs:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


def _created_id(resp) -> int:
    """id созданного объекта из ответа сервера; ответ без id (не JSON, пустой) — ошибка,
    после которой факт создания перепроверяется по серверу."""
    if isinstance(resp, dict) and resp.get("id"):
        return int(resp["id"])
    raise MeshError(200, f"сервер не вернул id созданного объекта: {str(resp)[:200]}", resp)


def run_parallel(fn: Callable, items: Iterable, workers: int, each: Callable[[object, object], bool]) -> bool:
    """Выполняет ``fn(item)`` для всех items, держа в работе не больше ``workers`` запросов сразу.

    ``each(item, outcome)`` вызывается в вызывающем потоке по завершении каждого: outcome — результат
    либо :class:`MeshError`. Вернула True — новых запросов не начинать (начатые довыполняются и тоже
    проходят через ``each``); тогда и run_parallel возвращает True. Прочие исключения — наружу."""
    todo = iter(items)
    stopped = False
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        running: dict = {}

        def submit_next() -> bool:
            for item in todo:
                running[ex.submit(fn, item)] = item
                return True
            return False

        for _ in range(max(1, workers)):
            if not submit_next():
                break
        while running:
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for fut in finished:
                item = running.pop(fut)
                try:
                    outcome = fut.result()
                except MeshError as e:
                    outcome = e
                if each(item, outcome):
                    stopped = True
                elif not stopped:
                    submit_next()
    return stopped


EC_BUSY = "уже существуют уроки в расписании"   # «На данную дату и время у преподавателя, учеников группы ВД или в кабинете …»


def create_ec_without_students(client: MeshClient, group_id: int, create: Callable[[], dict], error: MeshError,
                               restore_errors: list[tuple[str, str]]) -> dict:
    """Обход ошибки :data:`EC_BUSY`: сервер считает занятыми учеников группы (у них в это время уроки),
    хотя занятие ВД им не мешает.  Все ученики временно исключаются из группы, занятие создаётся
    повторно, затем ученики зачисляются обратно с прежними датами — при любом исходе повтора.
    ``error`` — исходная ошибка.  Не удалось вернуть ученика — запись в ``restore_errors`` (её надо
    поправить вручную)."""
    recs = [r for r in client.ec_group_students([group_id]) if r.get("ec_group_id") == group_id and not r.get("deleted_at")]
    if not recs:
        raise MeshError(error.status, f"{error.message} (учеников в группе нет — конфликт по учителю или кабинету)",
                        error.payload) from error
    log.info("группа ВД %s: сервер считает время занятым — временно исключаем %d учеников и повторяем",
             group_id, len(recs))
    removed: list[dict] = []
    try:
        for r in recs:
            client.delete_ec_group_student(r["id"])
            removed.append(r)
        try:
            return create()
        except MeshError as e:
            if EC_BUSY in e.message:
                raise MeshError(e.status, f"{e.message} (и без учеников группы — конфликт по учителю или кабинету)",
                                e.payload) from e
            raise
    finally:
        for r in removed:
            try:
                client.add_ec_group_student(group_id, r["student_id"], r["begin_date"], r["end_date"])
            except MeshError as e:
                log.error("группа ВД %s: не удалось вернуть ученика %s: %s", group_id, r["student_id"], e.message)
                restore_errors.append((f"возврат ученика {r['student_id']} в группу ВД {group_id} "
                                       f"({r['begin_date']}–{r['end_date']})", e.message))


def apply_plan(client: MeshClient, plan: Plan, journal: Journal | None = None,
               stop_on_error: bool = False, progress=None, should_stop: StopCheck | None = None,
               workers: int = 1) -> ApplyResult:
    """Выполняет план: расписание ВД недели (если его нет), затем удаления, затем создания.
    progress(done, total) — обратный вызов; should_stop() == True — остановиться перед очередным
    занятием (сделанное остаётся в журнале, результат помечается ``stopped``); workers — сколько
    запросов слать одновременно (фазы удаления и создания не смешиваются)."""
    res = ApplyResult()
    total = len(plan.to_delete) + len(plan.to_create)
    done = 0
    failed: list[PlannedItem] = []
    ec_error = ""

    def stop_now() -> bool:
        return bool(should_stop and should_stop())

    def step():
        nonlocal done
        done += 1
        if progress:
            progress(done, total)
    if any(p.kind == "ec" for p in plan.to_create):
        # до удалений: если расписание ВД не создать, занятия ВД не создать тоже — лучше узнать заранее.
        # id расписания — с сервера прямо сейчас: между планом и загрузкой сайт мог его пересоздать
        try:
            sync_ec_schedule(client, plan)
            if plan.ec_schedule_id is None:
                plan.ec_schedule_id = _created_id(client.create_ec_schedule(plan.week))
                log.info("создано расписание ВД недели %s: id %s", plan.week, plan.ec_schedule_id)
        except MeshError as e:
            ec_error = e.message
            res.errors.append((f"создание расписания ВД недели {plan.week}", e.message))
            if stop_on_error:
                return res
    deleted: set[ItemKey] = set()

    def delete(it: dict) -> None:
        if item_kind(it) == "ec":
            client.delete_ec_item(it["id"])
        else:
            client.delete_item(it.get("schedule_id") or plan.schedule_id, it["id"])

    def after_delete(it: dict, outcome) -> bool:
        if isinstance(outcome, MeshError):
            res.errors.append((f"удаление: {describe_existing(it)}", outcome.message))
        else:
            res.deleted += 1
            deleted.add((item_kind(it), it["id"]))
            log.debug("удалено: %s", describe_existing(it))
        step()
        return (stop_on_error and bool(res.errors)) or stop_now()

    if plan.to_delete:
        if stop_now():
            res.stopped = True
        elif run_parallel(delete, plan.to_delete, workers, after_delete):
            res.stopped = not (stop_on_error and res.errors)
    if journal and deleted:
        journal.remove(deleted)
    if res.stopped or (stop_on_error and res.errors):
        return res

    group_locks: dict[int, threading.Lock] = {}     # учеников одной группы ВД снимает один поток за раз
    locks_lock = threading.Lock()
    restore_errors: list[tuple[str, str]] = []

    def create_ec(p: PlannedItem) -> dict:
        def once() -> dict:
            return client.create_ec_item(plan.ec_schedule_id, p.group_id, p.date, p.begin_time, p.end_time,
                                         p.duration, p.teacher_id, p.room_id, p.building_id)
        try:
            return once()
        except MeshError as e:
            if EC_BUSY not in e.message:
                raise
            with locks_lock:
                lock = group_locks.setdefault(p.group_id, threading.Lock())
            with lock:
                return create_ec_without_students(client, p.group_id, once, e, restore_errors)

    def create(p: PlannedItem) -> int:
        if p.kind == "ec":
            if plan.ec_schedule_id is None:
                raise MeshError(0, f"расписание ВД недели не создано: {ec_error}")
            created = create_ec(p)
        else:
            created = client.create_item(plan.schedule_id, p.group_id, p.bell_id, p.date,
                                         p.teacher_id, p.room_id, p.building_id)
        item_id = _created_id(created)
        if journal:
            journal.record(plan, p, item_id)      # сразу в рабочем потоке: созданное не потеряется
        log.debug("создано: %s", p.describe())
        return item_id

    def after_create(p: PlannedItem, outcome) -> bool:
        if isinstance(outcome, MeshError):
            res.errors.append((p.describe(), outcome.message))
            failed.append(p)
        else:
            res.created += 1
        step()
        return (stop_on_error and bool(res.errors)) or stop_now()

    if plan.to_create:
        if stop_now():
            res.stopped = True
        elif run_parallel(create, plan.to_create, workers, after_create):
            res.stopped = not (stop_on_error and res.errors)
    res.errors.extend(restore_errors)
    if failed:
        # ответ мог не дойти (обрыв запроса) или прийти без id, а занятие — создаться: сверяемся с сервером
        existing = {_item_key(it): it for it in fetch_existing(client, plan)}
        for p in failed:
            it = existing.get(p.key)
            if it is not None:
                res.created += 1
                res.errors = [e for e in res.errors if e[0] != p.describe()]
                if journal:
                    journal.record(plan, p, it["id"])
    return res


def journal_matches(rec: dict, weeks: list[int] | None, sel: Selection | None) -> bool:
    """Подходит ли запись журнала под выбор недель/классов/дат/вида (None — без ограничения)."""
    if weeks and rec["week"] not in weeks:
        return False
    if sel:
        if sel.classes is not None and rec.get("class") not in sel.classes:
            return False
        if sel.dates is not None and _dt.date.fromisoformat(rec["date"]) not in sel.dates:
            return False
        if sel.days is not None and _dt.date.fromisoformat(rec["date"]).weekday() not in sel.days:
            return False
        if sel.only and rec.get("kind", "oo") != sel.only:
            return False
    return True


def undo(client: MeshClient, journal: Journal, weeks: list[int] | None = None,
         sel: Selection | None = None, items: set[ItemKey] | None = None,
         progress=None, should_stop: StopCheck | None = None, workers: int = 1) -> ApplyResult:
    """Удаляет занятия из журнала (все, по выбору недель/классов/дат либо по ключам (вид, id));
    удалённые записи убираются из журнала.  progress(done, total) — обратный вызов; should_stop()
    и workers — как в :func:`apply_plan` (неудалённое остаётся в журнале)."""
    res = ApplyResult()
    records = journal.read()
    todo = [rec for rec in records if journal_matches(rec, weeks, sel) and rec.get("item_id")
            and (items is None or journal_key(rec) in items)]
    gone: set[int] = set()      # id() удалённых записей
    done = 0

    def delete(rec: dict) -> None:
        try:
            if rec.get("kind") == "ec":
                client.delete_ec_item(rec["item_id"])
            else:
                client.delete_item(rec["schedule_id"], rec["item_id"])
        except MeshError as e:
            if e.status != 404:         # 404 — уже нет, считаем удалённым
                raise

    def after(rec: dict, outcome) -> bool:
        nonlocal done
        if isinstance(outcome, MeshError):
            res.errors.append((f"{'ВД ' if rec.get('kind') == 'ec' else ''}{rec['class']} {rec['date']} "
                               f"урок {rec['lesson']} {rec['group_name']} (id {rec['item_id']})", outcome.message))
        else:
            res.deleted += 1
            gone.add(id(rec))
        done += 1
        if progress:
            progress(done, len(todo))
        return bool(should_stop and should_stop())

    if todo:
        if should_stop and should_stop():
            res.stopped = True
        else:
            res.stopped = run_parallel(delete, todo, workers, after)
    journal.rewrite([rec for rec in records if id(rec) not in gone])
    return res


def copy_week(client: MeshClient, rd: RefData, from_week: int, to_weeks: list[int],
              class_unit_ids: list[int], force: bool = False) -> list[str]:
    """Копирование недели средствами МЭШ (POST .../copy) по каждому классу."""
    out = []
    sched = rd.schedule_by_week.get(from_week)
    if not sched:
        raise ValueError(f"в кэше нет недели {from_week}")
    for cu_id in class_unit_ids:
        cu = rd.class_unit_by_id[cu_id]
        lvl = [cu["class_level_id"]]
        chk = client.copy_check(sched["id"], lvl, cu_id, to_weeks)
        busy = chk.get("busy_weeks") or []
        if busy and not force:
            out.append(f"{cu['name']}: на неделях {[b['week_number'] for b in busy]} уже есть занятия — пропущено (force=True, чтобы копировать поверх)")
            continue
        r = client.copy(sched["id"], lvl, cu_id, to_weeks)
        out.append(f"{cu['name']}: {r.get('message', r)}")
    return out
