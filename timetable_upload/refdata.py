"""Справочные данные МЭШ (классы, группы, учителя, кабинеты, звонки, недели).

Загружаются через API (:func:`fetch`) в каталог-кэш в виде JSON-файлов: с ними можно
работать без входа (разбор, сопоставление, план), а графический интерфейс при каждом
запуске перезагружает их с сервера — id недель ВД, учителя групп и составы меняются.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .mesh_api import MeshClient

FETCH_WORKERS = 6        # параллельных запросов при загрузке справочников (плюс страницы групп внутри)

FILES = ("schedules", "groups", "class_units", "rooms", "teacher_profiles",
         "bells_timetable_assignments", "bells_timetables", "class_levels", "academic_year",
         "ec_activities", "ec_groups", "ec_schedules")



def norm_class(name: str) -> str:
    """'11-В' / '11в' / '11 В' → '11в'."""
    return re.sub(r"[\s\-_.]", "", (name or "")).lower().replace("ё", "е")


def norm_text(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = re.sub(r"[^\w]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


DEFAULTS_FILE = os.path.join(os.path.dirname(__file__), "defaults.json")


def load_defaults() -> dict:
    """Настройки по умолчанию (defaults.json в пакете): синонимы предметов, длительности ВД и т. п."""
    with open(DEFAULTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def ec_course_name(activity_name: str, suffix_re: str) -> str:
    """Название курса ВД без хвоста с параллелью/классами: «Разговоры о важном 2-4», «Математическая логика 7п»,
    «Методы решения физических задач 9абм» → без последнего слова.  suffix_re — из настроек сопоставления."""
    name = (activity_name or "").strip()
    return re.sub(suffix_re, "", name, flags=re.I).strip() if suffix_re else name


def parse_ru_date(s: str | None) -> _dt.date | None:
    if not s:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s[:10], fmt).date()
        except ValueError:
            pass
    return None


@dataclass
class RefData:
    schedules: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    class_units: list = field(default_factory=list)
    rooms: list = field(default_factory=list)
    teacher_profiles: list = field(default_factory=list)
    bells_timetable_assignments: list = field(default_factory=list)
    bells_timetables: list = field(default_factory=list)
    class_levels: list = field(default_factory=list)
    academic_year: dict = field(default_factory=dict)   # учебный год, к которому относится кэш
    ec_activities: list = field(default_factory=list)   # курсы внеурочной деятельности
    ec_groups: list = field(default_factory=list)       # группы ВД (как отдаёт сервер)
    ec_schedules: list = field(default_factory=list)    # расписания ВД по неделям
    fetched_at: _dt.datetime | None = None              # когда загружено с сервера (по времени файлов кэша)

    # ------------------------------------------------------------------ загрузка/сохранение
    @classmethod
    def load(cls, cache_dir, ec_course_suffix: str | None = None) -> RefData:
        """ec_course_suffix — regex хвоста названия курса ВД (Mapping.ec_course_suffix); None — по умолчанию."""
        rd = cls()
        for name in FILES:
            p = os.path.join(cache_dir, name + ".json")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    setattr(rd, name, json.load(f))
        p = os.path.join(cache_dir, "groups.json")
        if os.path.exists(p):
            rd.fetched_at = _dt.datetime.fromtimestamp(os.path.getmtime(p))
        rd._index(ec_course_suffix)
        return rd

    def save(self, cache_dir) -> None:
        os.makedirs(cache_dir, exist_ok=True)
        for name in FILES:
            with open(os.path.join(cache_dir, name + ".json"), "w", encoding="utf-8") as f:
                json.dump(getattr(self, name), f, ensure_ascii=False, indent=1)

    def summary(self) -> str:
        ay = self.academic_year
        return ((f"загружено {self.fetched_at:%d.%m.%Y %H:%M}; " if self.fetched_at else "")
                + f"учебный год {ay.get('name', '?')} (id {ay.get('id', '?')}): "
                f"недель: {len(self.schedules)}, групп: {len(self.groups)}, классов: {len(self.class_units)}, "
                f"кабинетов: {len(self.rooms)}, учителей: {len(self.teachers)}"
                f"{f' (+ удалённых {len(self.teacher_profiles) - len(self.teachers)})' if len(self.teachers) < len(self.teacher_profiles) else ''}, "
                f"расписаний звонков: {len(self.bells_timetables)} (назначений: {len(self.bells_timetable_assignments)}); "
                f"ВД: курсов {len(self.ec_activities)}, групп {len(self.ec_groups)}, недель {len(self.ec_schedules)}")

    # ------------------------------------------------------------------ индексы
    def _index(self, ec_course_suffix: str | None = None) -> None:
        if ec_course_suffix is None:
            ec_course_suffix = load_defaults()["ec_course_suffix"]
        self.schedule_by_week = {s["week_number"]: s for s in self.schedules}
        self.group_by_id = {g["id"]: g for g in self.groups}
        self.class_unit_by_id = {c["id"]: c for c in self.class_units}
        self.class_unit_by_name: dict[str, dict] = {}
        for c in self.class_units:
            for key in (c.get("name"), c.get("unit_name"), c.get("display_name")):
                if key:
                    self.class_unit_by_name.setdefault(norm_class(key), c)
        self.room_by_id = {r["id"]: r for r in self.rooms}
        # профили запрашиваются вместе с удалёнными (как делает веб-интерфейс, чтобы знать имена по старым
        # занятиям), но в работе — сопоставление, выбор, учителя групп — участвуют только действующие
        self.teacher_by_id = {t["id"]: t for t in self.teacher_profiles}
        self.teachers = [t for t in self.teacher_profiles if not t.get("deleted")]
        self.bells_by_id = {b["id"]: b for b in self.bells_timetables}
        self.groups_by_class: dict[int, list] = {}
        # нагрузка учителей по неделям (teacher_loads): группа → учитель → неделя → часов
        self.loads_by_group: dict[int, dict[int, dict[int, int]]] = {}
        for g in self.groups:
            g["teachers"] = [t for t in g.get("teachers") or [] if not self.teacher_deleted(t.get("id"))]
            for t in g["teachers"]:
                for ld in t.get("teacher_loads") or []:
                    if ld.get("week_number") is not None and ld.get("hours") is not None:
                        self.loads_by_group.setdefault(g["id"], {}).setdefault(t["id"], {})[ld["week_number"]] = ld["hours"]
            ids = set(g.get("class_unit_ids") or [])
            if g.get("class_unit_id"):
                ids.add(g["class_unit_id"])
            for cu in ids:
                self.groups_by_class.setdefault(cu, []).append(g)
        self.subject_names = sorted({g["subject_name"] for g in self.groups if g.get("subject_name")})
        # ВД: группы приводятся к виду обычных групп (subject_name, teachers, class_unit_ids, student_ids,
        # is_metagroup, kind="ec"), чтобы сопоставление занятий работало одинаково
        self.ec_schedule_by_week = {s["week_number"]: s for s in self.ec_schedules}
        activity_by_id = {a["id"]: a for a in self.ec_activities}
        self.ec_group_by_id: dict[int, dict] = {}
        self.ec_groups_by_class: dict[int, list] = {}
        for raw in self.ec_groups:
            act = activity_by_id.get(raw.get("ec_activity_id")) or {}
            course = ec_course_name(act.get("name") or re.sub(r"^группа\s+", "", raw.get("name") or "", flags=re.I),
                                    ec_course_suffix)
            teachers = [{"id": tid, "full_name": (self.teacher_by_id.get(tid) or {}).get("name")}
                        for tid in raw.get("teacher_ids") or [] if not self.teacher_deleted(tid)]
            cus = list(raw.get("class_unit_ids") or [])
            g = {"kind": "ec", "id": raw["id"], "name": (raw.get("name") or "").strip(), "subject_name": course,
                 "activity_name": act.get("name"), "class_unit_ids": cus, "student_ids": raw.get("student_ids") or [],
                 "teachers": teachers, "is_metagroup": len(cus) > 1}
            self.ec_group_by_id[g["id"]] = g
            for cu in cus:
                self.ec_groups_by_class.setdefault(cu, []).append(g)
        self.ec_course_names = sorted({g["subject_name"] for g in self.ec_group_by_id.values() if g["subject_name"]})

    def group_hours(self, g: dict, week: int, teacher_id: int | None = None) -> int | None:
        """Часов группы на неделе по нагрузке учителей (teacher_loads): нагрузка учителя занятия, а если
        у него записи нет — сумма по всем учителям группы.  None — данных нет (группы ВД, группы без
        нагрузки): лимит часов не проверяется."""
        loads = self.loads_by_group.get(g["id"])
        if not loads:
            return None
        if teacher_id in loads and week in loads[teacher_id]:
            return loads[teacher_id][week]
        vals = [w[week] for w in loads.values() if week in w]
        return sum(vals) if vals else None

    def group_hours_span(self, g: dict, teacher_id: int | None = None) -> str:
        """Часы группы в неделю по неделям года, где они есть (нули — каникулы и недели без нагрузки —
        не учитываются): «1» или «1–2»; «0» — нагрузки нет ни на одной неделе; «» — данных нет."""
        vals = {h for wk in self.schedule_by_week if (h := self.group_hours(g, wk, teacher_id)) is not None}
        if not vals:
            return ""
        vals = vals - {0} or {0}
        return str(min(vals)) if len(vals) == 1 else f"{min(vals)}–{max(vals)}"

    def check_academic_year(self, client: MeshClient) -> None:
        """Кэш должен относиться к учебному году сессии: id недель, групп и звонков в разных годах разные,
        и запись с чужими id уйдёт не туда либо будет отвергнута сервером."""
        cached = self.academic_year.get("id")
        if cached is None or int(cached) == client.ay:
            return
        raise ValueError(f"кэш справочников относится к учебному году {self.academic_year.get('name')} (id {cached}), "
                         f"а вход выполнен в год {client.session_info.academic_year.get('name')} (id {client.ay}): "
                         "загрузите справочники заново либо укажите тот же учебный год при входе")

    # ------------------------------------------------------------------ поиск
    def teacher_deleted(self, teacher_id) -> bool:
        """Профиль учителя известен и помечен удалённым (сервер такого не примет)."""
        tp = self.teacher_by_id.get(teacher_id)
        return tp is not None and bool(tp.get("deleted"))

    def class_unit(self, name: str) -> dict | None:
        return self.class_unit_by_name.get(norm_class(name))

    def any_group(self, group_id: int) -> dict | None:
        """Группа по id — обычная или ВД."""
        return self.group_by_id.get(group_id) or self.ec_group_by_id.get(group_id)

    def bells_timetable_for(self, class_unit_id: int, on: _dt.date) -> dict | None:
        best = None
        for a in self.bells_timetable_assignments:
            if a["class_unit_id"] != class_unit_id:
                continue
            b, e = parse_ru_date(a.get("begin_date")), parse_ru_date(a.get("end_date"))
            if (b is None or b <= on) and (e is None or on <= e):
                best = a
        return self.bells_by_id.get(best["bells_timetable_id"]) if best else None

    @staticmethod
    def bell_for(timetable: dict, weekday: int, lesson_number: int) -> dict | None:
        """Звонок урока ``lesson_number`` (с 1) в день ``weekday`` (1 = понедельник)."""
        for day in timetable.get("bells_day_timetables") or []:
            if day.get("week_day_id") != weekday:
                continue
            for bell in day.get("bells") or []:
                if bell.get("is_lesson") and bell.get("study_ordinal") == lesson_number:
                    return bell
        return None

    def lesson_numbers(self, timetable: dict, weekday: int) -> list[int]:
        for day in timetable.get("bells_day_timetables") or []:
            if day.get("week_day_id") == weekday:
                return sorted(b["study_ordinal"] for b in day.get("bells") or []
                              if b.get("is_lesson") and b.get("study_ordinal") is not None)
        return []


# ---------------------------------------------------------------------- получение через API
def fetch(client: MeshClient, cache_dir, class_level_ids: list[int] | None = None,
          message: Callable[[str], None] | None = None) -> RefData:
    """Все справочники с сервера — в кэш на диске.  Списки независимы и запрашиваются параллельно
    (группы — самый долгий, их страницы тоже идут параллельно, см. ``MeshClient.get_pages``);
    в кэш попадает только полный набор: при ошибке любого запроса старый кэш остаётся."""
    rd = RefData()
    rd.academic_year = client.session_info.academic_year

    def by_levels(fn):
        def run():
            seen: dict[int, dict] = {}
            for lvl in class_level_ids:
                for x in fn(class_level_id=lvl):
                    seen[x["id"]] = x
            return list(seen.values())
        return run if class_level_ids else fn

    tasks: dict[str, Callable[[], list]] = {
        "schedules": client.schedules, "class_levels": client.class_levels, "rooms": client.rooms,
        "groups": by_levels(client.groups), "class_units": by_levels(client.class_units),
        "teacher_profiles": by_levels(client.teacher_profiles),
        "bells_timetable_assignments": client.bells_timetable_assignments, "bells_timetables": client.bells_timetables,
        "ec_activities": client.ec_activities, "ec_groups": client.ec_groups, "ec_schedules": client.ec_schedules,
    }
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futures = {name: ex.submit(fn) for name, fn in tasks.items()}
        for name, f in futures.items():
            setattr(rd, name, f.result())
            if message:
                message(f"получено: {name}")
    rd.fetched_at = _dt.datetime.now()
    rd.save(cache_dir)
    rd._index()
    return rd
