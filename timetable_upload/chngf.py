"""Чтение файлов расписания ХроноГраф 3.0 Мастер (*.chngf).

Формат восстановлен по образцам файлов и по спецификации COM-интерфейса
(Document/Spec в дистрибутиве программы).  Файл — простой архив из трёх «файлов»
(NewTimeTable.dat, info.txt, Info.dat); всё содержательное лежит в
NewTimeTable.dat.

Базовые примитивы:
    i32   — little-endian int32
    i16   — little-endian int16
    u8    — байт
    f64   — double (даты — TDateTime Delphi, дни от 30.12.1899)
    str   — i32 1000 (маркер), i32 длина, байты в cp1251
Каждый сериализованный объект начинается с пары i32 (typeId, version).

Сетка уроков у любого объекта хранится плоским массивом длиной
DayCount*LessonCount*WeekCount, индекс = (week*DayCount + day)*LessonCount + lesson
(проверено по неделе длиной 1; для WeekCount>1 порядок недель — предположение).
"""

from __future__ import annotations

import datetime as _dt
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field

STR_MARKER = 1000
LIST_MARKER = 2000
LIST_ITEM_MARKER = 2001
TDATETIME_EPOCH = _dt.datetime(1899, 12, 30)

# идентификаторы типов сериализованных объектов
T_CLSGRP = 1
T_TEACHER_LESSON = 2
T_CABINET = 3
T_CABINET_LESSON = 4
T_TEACHER = 5
T_CLS = 6
T_CLS_LESSON = 7
T_CLSGRP_LESSONS = 8
T_SUBJECT = 10
T_SUBJ_CLS = 11
T_SUBJ_TEACHER = 12
T_LANG = 13
T_KEYBASE = 14
T_BASE_SUBJECT = 19
T_STUDENT = 14          # (14, 9) — ученик в «колледж»-классе


class ChngfError(ValueError):
    pass


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.p = pos

    def i32(self) -> int:
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def i16(self) -> int:
        v = struct.unpack_from("<h", self.d, self.p)[0]
        self.p += 2
        return v

    def u8(self) -> int:
        v = self.d[self.p]
        self.p += 1
        return v

    def f64(self) -> float:
        v = struct.unpack_from("<d", self.d, self.p)[0]
        self.p += 8
        return v

    def raw(self, n: int) -> bytes:
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def s(self) -> str:
        m = self.i32()
        if m != STR_MARKER:
            raise ChngfError(f"ожидался маркер строки по смещению {self.p - 4:#x}, получено {m}")
        n = self.i32()
        return self.raw(n).decode("cp1251")

    def expect(self, *values: int, what: str = "") -> list[int]:
        got = [self.i32() for _ in values]
        if list(values) != got:
            raise ChngfError(f"{what or 'заголовок'}: ожидалось {values}, получено {got} (смещение {self.p:#x})")
        return got


# ----------------------------------------------------------------------------
# Модель данных
# ----------------------------------------------------------------------------

@dataclass
class KeyBase:
    """IAKeyBase: группы ключей классогрупп (идентичные группы / потоки)."""
    keys: list[tuple[int, int, int]] = field(default_factory=list)   # (key, groupIdx, identIdx)
    groups: list[list[int]] = field(default_factory=list)           # группы ключей
    ident_groups: list[list[int]] = field(default_factory=list)     # группы идентичных ключей


@dataclass
class Cabinet:
    index: int
    name: str
    capacity: int
    floor: int
    busy: list[int]          # 2 = кабинет недоступен на этом уроке
    tag: int = 0


@dataclass
class TeacherSlot:
    busy: int                # 0 — свободен, 1 — нежелательно, 2 — нельзя
    cabinet: int             # индекс кабинета, -1 — не назначен
    has_lesson: bool         # у преподавателя на этом уроке есть занятие (с назначенным кабинетом)


@dataclass
class Teacher:
    index: int
    last_name: str           # либо «Фамилия И.О.» целиком, если ФИО не разбито
    first_name: str
    middle_name: str
    slots: list[TeacherSlot]
    max_work_days: int = -1  # «Рабочие дни»: не больше стольких рабочих дней в неделю, -1 — не задано
    home_cabinet: int = -1   # индекс закреплённого кабинета, -1 — нет
    code: str = ""
    raw_header: list[int] = field(default_factory=list)

    @property
    def name(self) -> str:
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(p for p in parts if p)

    @property
    def short_name(self) -> str:
        """«Фамилия И.О.»"""
        if not self.first_name and not self.middle_name:
            return self.last_name
        ini = "".join(f"{p[0]}." for p in (self.first_name, self.middle_name) if p)
        return f"{self.last_name} {ini}"


@dataclass
class ClsGrp:
    """Классогруппа: множество учеников одного класса, изучающих один предмет."""
    index: int
    key: int
    group: int               # 0 — весь класс, иначе номер группы 1..N
    nagr: int                # часов в неделю
    pairs: bool
    amount: int = 0          # численность (AmountGr)
    lessons: list[bool] = field(default_factory=list)   # сетка занятий (LessonPresent)
    subject: int = -1        # индекс предмета
    cls: int = -1            # индекс класса
    teacher: int = -1        # индекс преподавателя, -1 — не назначен
    lang: int = -1           # индекс иностранного языка, -1 — нет


@dataclass
class Student:
    number: int
    last_name: str
    first_name: str
    middle_name: str
    gender: int


@dataclass
class Cls:
    index: int
    name: str
    full_name: str
    busy: list[int]          # 2 — класс не может заниматься на уроке
    parallel: int = -1       # номер параллели
    home_cabinet: int = -1   # индекс закреплённого кабинета, -1 — нет
    man: int = 0             # мальчиков
    woman: int = 0           # девочек
    students: list[Student] = field(default_factory=list)
    raw_header: list[int] = field(default_factory=list)


@dataclass
class Parallel:
    number: int
    name: str
    prefix: str
    classes: list[int]       # индексы классов


@dataclass
class SubjCls:
    cls: int
    nagr: int
    clsgrps: list[int]       # индексы классогрупп
    flag: int = 1


@dataclass
class SubjTeacher:
    teacher: int
    clsgrps: list[int]


@dataclass
class Subject:
    index: int
    id: int
    name: str
    short_name: str
    subj_teachers: list[SubjTeacher]
    subj_classes: list[SubjCls]
    cabinets: list[int]
    raw: list[int] = field(default_factory=list)


@dataclass
class Lang:
    """Иностранный язык; классогруппа с языком в расписании показывается под названием языка."""
    index: int
    name: str
    short_name: str
    teachers: list[int]
    clsgrps: list[int]


@dataclass
class Lesson:
    """Одно занятие в расписании (одна классогруппа в одном слоте)."""
    day: int                 # 0 = понедельник
    lesson: int              # 0 = первый урок
    week: int                # 0 = первая неделя
    cls: str
    subject: str             # название в расписании (для ин.яз. — название языка)
    subject_short: str
    base_subject: str        # название предмета по учебному плану
    group: int               # 0 = весь класс
    teacher: str             # полное имя из файла («Фамилия Имя Отчество» или «Фамилия И.О.»)
    teacher_index: int
    cabinet: str             # '' если не назначен
    clsgrp_index: int
    clsgrp_key: int
    potok: tuple[str, ...] = ()   # классы «потока» (занятие идёт совместно у нескольких классов), включая свой

    @property
    def lang_used(self) -> bool:
        return self.subject != self.base_subject


@dataclass
class Timetable:
    title: str
    project_name: str
    version: str
    saved_at: _dt.datetime | None
    day_count: int
    lesson_count: int
    week_count: int
    teach_year: str
    half_year_name: str
    period_begin: _dt.date | None
    period_end: _dt.date | None
    lesson_begin: list[_dt.time]
    lesson_end: list[_dt.time]
    cabinets: list[Cabinet]
    teachers: list[Teacher]
    clsgrps: list[ClsGrp]
    classes: list[Cls]
    parallels: list[Parallel]
    subjects: list[Subject]
    langs: list[Lang]
    ident_base: KeyBase
    potok_base: KeyBase
    extra_base: KeyBase
    school_name: str = ""
    header: list[int] = field(default_factory=list)

    @property
    def slot_count(self) -> int:
        return self.day_count * self.lesson_count * self.week_count

    def slot(self, day: int, lesson: int, week: int = 0) -> int:
        return (week * self.day_count + day) * self.lesson_count + lesson

    def unslot(self, slot: int) -> tuple[int, int, int]:
        lesson = slot % self.lesson_count
        rest = slot // self.lesson_count
        return rest % self.day_count, lesson, rest // self.day_count

    def potok_classes(self) -> dict[int, tuple[str, ...]]:
        """Индекс классогруппы → кортеж названий классов её потока (только для потоков из >1 класса)."""
        by_key = {g.key: g for g in self.clsgrps}
        out: dict[int, tuple[str, ...]] = {}
        for grp in self.potok_base.groups:
            members = [by_key[k] for k in grp if k in by_key]
            names = tuple(sorted({self.classes[g.cls].name for g in members if g.cls >= 0}))
            if len(names) > 1:
                for g in members:
                    out[g.index] = names
        return out

    def iter_lessons(self) -> Iterator[Lesson]:
        """Все занятия расписания в порядке класс → неделя → день → урок."""
        potok = self.potok_classes()
        by_cls: dict[int, list[ClsGrp]] = {}
        for g in self.clsgrps:
            if g.cls >= 0:
                by_cls.setdefault(g.cls, []).append(g)
        for cls in self.classes:
            for g in sorted(by_cls.get(cls.index, []), key=lambda x: x.index):
                for slot, present in enumerate(g.lessons):
                    if not present:
                        continue
                    day, lesson, week = self.unslot(slot)
                    subj = self.subjects[g.subject]
                    subj_name, subj_short = subj.name, subj.short_name
                    if g.lang >= 0:
                        subj_name, subj_short = self.langs[g.lang].name, self.langs[g.lang].short_name
                    teacher = self.teachers[g.teacher] if g.teacher >= 0 else None
                    cab = ""
                    if teacher is not None:
                        ts = teacher.slots[slot]
                        if ts.cabinet >= 0:
                            cab = self.cabinets[ts.cabinet].name
                    yield Lesson(
                        day=day, lesson=lesson, week=week,
                        cls=cls.name, subject=subj_name, subject_short=subj_short,
                        base_subject=subj.name, group=g.group,
                        teacher=teacher.name if teacher else "",
                        teacher_index=g.teacher,
                        cabinet=cab,
                        clsgrp_index=g.index, clsgrp_key=g.key,
                        potok=potok.get(g.index, ()),
                    )

    def lessons_by_class(self) -> dict[str, list[Lesson]]:
        out: dict[str, list[Lesson]] = {}
        for lesson in self.iter_lessons():
            out.setdefault(lesson.cls, []).append(lesson)
        for v in out.values():
            v.sort(key=lambda l: (l.week, l.day, l.lesson, l.group, l.subject))
        return out


# ----------------------------------------------------------------------------
# Разбор
# ----------------------------------------------------------------------------

def _tdatetime(v: float) -> _dt.datetime:
    return TDATETIME_EPOCH + _dt.timedelta(days=v)


def _daytime(v: float) -> _dt.time:
    secs = round((v % 1.0) * 86400)
    return (_dt.datetime.min + _dt.timedelta(seconds=secs)).time()


def _read_keybase(r: Reader) -> KeyBase:
    r.expect(T_KEYBASE, 1, what="KeyBase")
    kb = KeyBase()
    n = r.i32()
    kb.keys = [(r.i32(), r.i32(), r.i32()) for _ in range(n)]
    for target in (kb.groups, kb.ident_groups):
        g = r.i32()
        for _ in range(g):
            c = r.i32()
            # пары (ключ, индекс ключа в общем списке); храним только ключи
            target.append([(r.i32(), r.i32())[0] for _ in range(c)])
    return kb


def _read_student(r: Reader) -> Student:
    r.expect(T_STUDENT, 9, what="Student")
    number = r.i32()
    r.i32()
    r.s(); r.s(); r.s()
    r.i32()
    last, first, middle = r.s(), r.s(), r.s()
    r.u8()
    gender = r.i32()                       # 1 — м, 2 — ж (по образцу)
    for _ in range(7):
        r.i32()
    for _ in range(4):
        r.s()
    r.i32(); r.i32()
    r.s()
    return Student(number, last, first, middle, gender)


def _read_grid_dims(r: Reader, tt_dims: tuple[int, int, int], what: str) -> None:
    """Пять int32 перед сеткой: ?, ?, LessonCount, DayCount, WeekCount (первые два в образцах 9 и 2)."""
    _, _, lc, dc, wc = (r.i32() for _ in range(5))
    day_count, lesson_count, week_count = tt_dims
    if (lc, dc, wc) != (lesson_count, day_count, week_count):
        raise ChngfError(f"{what}: размерность сетки {(lc, dc, wc)} не совпадает с заголовком {tt_dims}")


def parse_timetable_dat(data: bytes) -> Timetable:
    r = Reader(data)
    r.i32()                      # -1
    r.i32()                      # 0
    r.i32()                      # 38
    version = r.s()
    header = [r.i32() for _ in range(8)]
    day_count, lesson_count, week_count = header[5], header[6], header[7]
    dims = (day_count, lesson_count, week_count)
    nslots = day_count * lesson_count * week_count
    if not (1 <= day_count <= 7 and 1 <= lesson_count <= 18 and 1 <= week_count <= 2):
        raise ChngfError(f"неправдоподобные размеры сетки: {dims}")

    ident_base = _read_keybase(r)
    potok_base = _read_keybase(r)
    extra_base = _read_keybase(r)   # в образцах всегда пустая
    teach_year = r.s()
    half_year_name = r.s()
    period_begin = _tdatetime(r.f64()).date()
    period_end = _tdatetime(r.f64()).date()
    r.i32(); r.i32(); r.u8(); r.i32()      # 1 1 1 1 — назначение неизвестно (полугодие/смены?)
    n = r.i32()
    lesson_begin = [_daytime(r.f64()) for _ in range(n)]
    n = r.i32()
    lesson_end = [_daytime(r.f64()) for _ in range(n)]
    r.i32(); r.u8()

    # --- кабинеты
    cabinets = []
    for i in range(r.i32()):
        r.expect(T_CABINET, 3, what="Cabinet")
        _read_grid_dims(r, dims, "Cabinet")
        name = r.s()
        capacity, floor = r.i32(), r.i32()
        busy = []
        for _ in range(nslots):
            r.expect(T_CABINET_LESSON, 1, what="CabinetLesson")
            busy.append(r.u8())
        tag = r.i32()
        cabinets.append(Cabinet(i, name, capacity, floor, busy, tag))

    # --- преподаватели
    teachers = []
    for i in range(r.i32()):
        r.expect(T_TEACHER, 11, what="Teacher")
        flag = r.u8()
        max_work_days = r.i32()
        _read_grid_dims(r, dims, "Teacher")
        names = (r.s(), r.s(), r.s())
        a, b = r.i32(), r.i32()                # a = 1 — есть закреплённый кабинет с индексом b (иначе 0/11)
        items = [r.i32() for _ in range(r.i32())]   # список (в демо — [0] у части преподавателей; языки?)
        hdr = [a, b, len(items), r.i32(), r.u8(), r.u8(), r.u8()]
        slots = []
        for _ in range(nslots):
            r.expect(T_TEACHER_LESSON, 2, what="TeacherLesson")
            busy = r.i16()
            cab = r.i32()
            has = r.u8()
            slots.append(TeacherSlot(busy, cab, bool(has)))
        code = r.s()                           # код/табельный номер ('' в новых файлах)
        tail = [r.i32(), r.i32(), r.i32()]     # -1 0 0
        teachers.append(Teacher(i, *names, slots=slots, max_work_days=max_work_days, code=code,
                                home_cabinet=b if a == 1 else -1,
                                raw_header=[flag, max_work_days] + hdr + tail))

    # --- классогруппы (без сеток)
    clsgrps = []
    for i in range(r.i32()):
        r.expect(T_CLSGRP, 3, what="ClsGrp")
        r.i32()                                # 0
        _read_grid_dims(r, dims, "ClsGrp")
        group = r.i32()
        amount = r.i32()                       # численность классогруппы (AmountGr)
        nagr = r.i32()
        key = r.i32()
        pairs = r.u8()
        clsgrps.append(ClsGrp(i, key, group, nagr, bool(pairs), amount=amount))

    # --- классы
    classes = []
    for i in range(r.i32()):
        r.expect(T_CLS, 11, what="Cls")
        students = [_read_student(r) for _ in range(r.i32())]   # только в «колледж»-классах
        h1 = [r.i32()]                         # индекс закреплённого кабинета или -1
        _read_grid_dims(r, dims, "Cls")
        h2 = [r.i32() for _ in range(4)]       # 0 -1 smena? 0
        flag = r.u8()
        man, woman = r.i32(), r.i32()
        name, full_name = r.s(), r.s()
        h3 = [r.i32()]                         # -1 (классный руководитель?)
        busy = []
        for _ in range(nslots):
            r.expect(T_CLS_LESSON, 1, what="ClsLesson")
            busy.append(r.i16())
        tail = [r.i32(), r.i32(), r.i32()]
        classes.append(Cls(i, name, full_name, busy, man=man, woman=woman, students=students,
                           home_cabinet=h1[0],
                           raw_header=h1 + h2 + [flag] + h3 + tail))

    # --- параллели
    r.expect(LIST_MARKER, what="список параллелей")
    parallels = []
    for _ in range(r.i32()):
        r.expect(LIST_ITEM_MARKER, what="параллель")
        number = r.i32()
        name, prefix = r.s(), r.s()
        idx = [r.i32() for _ in range(r.i32())]
        parallels.append(Parallel(number, name, prefix, idx))
        for ci in idx:
            classes[ci].parallel = number

    # --- предметы
    subjects = []
    for i in range(r.i32()):
        r.expect(T_SUBJECT, 5, what="Subject")
        sid = r.i32()
        name, short = r.s(), r.s()
        raw = [r.i32(), r.i32(), r.i16()]
        sts = []
        for _ in range(r.i32()):
            ti = r.i32()
            r.expect(T_SUBJ_TEACHER, 1, what="SubjTeacher")
            sts.append(SubjTeacher(ti, [r.i32() for _ in range(r.i32())]))
        scs = []
        for _ in range(r.i32()):
            ci = r.i32()
            r.expect(T_SUBJ_CLS, 2, what="SubjCls")
            flag = r.u8()
            nagr = r.i32()
            scs.append(SubjCls(ci, nagr, [r.i32() for _ in range(r.i32())], flag))
        cabs = [r.i32() for _ in range(r.i32())]
        subjects.append(Subject(i, sid, name, short, sts, scs, cabs, raw))
        for st in sts:
            for gi in st.clsgrps:
                clsgrps[gi].teacher = st.teacher
        for sc in scs:
            for gi in sc.clsgrps:
                clsgrps[gi].subject = i
                clsgrps[gi].cls = sc.cls

    # --- сетки занятий классогрупп
    n = r.i32()
    if n != len(clsgrps):
        raise ChngfError(f"число сеток ({n}) не совпадает с числом классогрупп ({len(clsgrps)})")
    for g in clsgrps:
        r.expect(T_CLSGRP_LESSONS, 2, what="ClsGrpLessons")
        dc, lc, wc = r.i32(), r.i32(), r.i32()
        if (dc, lc, wc) != dims:
            raise ChngfError(f"сетка классогруппы {g.index}: размерность {(dc, lc, wc)} != {dims}")
        g.lessons = [b != 0 for b in r.raw(nslots)]

    # --- иностранные языки
    langs = []
    for i in range(r.i32()):
        r.expect(T_LANG, 2, what="Lang")
        name, short = r.s(), r.s()
        tidx = [r.i32() for _ in range(r.i32())]
        gidx = [r.i32() for _ in range(r.i32())]
        r.i32()                                # tag
        langs.append(Lang(i, name, short, tidx, gidx))
        for gi in gidx:
            clsgrps[gi].lang = i

    # --- хвост: справочник базовых предметов и сведения о школе (в файлах старых сборок его нет)
    school_name = ""
    try:
        for _ in range(r.i32()):
            r.expect(T_BASE_SUBJECT, 1, what="BaseSubject")
            r.i32()
            r.s()
        r.expect(2010, 1, what="сведения о школе")
        r.s(); r.s()                           # город, код региона
        r.i32()
        school_name = r.s()
    except (ChngfError, struct.error, IndexError, UnicodeDecodeError):
        pass

    return Timetable(
        title="", project_name="", version=version, saved_at=None,
        day_count=day_count, lesson_count=lesson_count, week_count=week_count,
        teach_year=teach_year, half_year_name=half_year_name.strip(),
        period_begin=period_begin, period_end=period_end,
        lesson_begin=lesson_begin, lesson_end=lesson_end,
        cabinets=cabinets, teachers=teachers, clsgrps=clsgrps, classes=classes,
        parallels=parallels, subjects=subjects, langs=langs,
        ident_base=ident_base, potok_base=potok_base, extra_base=extra_base,
        school_name=school_name, header=header,
    )


def read_archive(data: bytes) -> tuple[dict[str, bytes], dict]:
    """Разбирает контейнер .chngf; возвращает файлы и метаданные заголовка."""
    r = Reader(data)
    if r.s() != "arc header":
        raise ChngfError("не файл ХроноГраф (нет 'arc header')")
    meta: dict = {}
    meta["h"] = [r.i32() for _ in range(4)]
    r.u8()
    r.i32()
    meta["title"] = r.s()
    meta["comment"] = r.s()
    meta["project_name"] = r.s()
    meta["saved_at"] = _tdatetime(r.f64())
    meta["h2"] = [r.i32() for _ in range(5)]
    meta["version"] = r.s()
    r.s()
    nfiles = r.i32()
    files: dict[str, bytes] = {}
    for _ in range(nfiles):
        name = r.s()
        size = r.i32()
        files[name] = r.raw(size)
    return files, meta


def load(path) -> Timetable:
    with open(path, "rb") as f:
        data = f.read()
    try:
        files, meta = read_archive(data)
        if "NewTimeTable.dat" not in files:
            raise ChngfError(f"в архиве нет NewTimeTable.dat (есть {list(files)})")
        tt = parse_timetable_dat(files["NewTimeTable.dat"])
    except ChngfError as e:
        raise ChngfError(f"{path}: {e}") from e
    except (struct.error, IndexError, UnicodeDecodeError, OverflowError, ValueError) as e:
        # обрезанный или чужой файл: разбор вышел за границу данных либо наткнулся на мусор
        raise ChngfError(f"{path}: файл повреждён или имеет другой формат ({e.__class__.__name__}: {e})") from e
    tt.title = meta["title"]
    tt.project_name = meta["project_name"]
    tt.saved_at = meta["saved_at"]
    return tt
