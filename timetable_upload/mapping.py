"""Сопоставление сущностей ХроноГрафа с сущностями МЭШ.

Результат хранится в JSON-файле сопоставления (см. :class:`Mapping`), который
пользователь может править руками.  Команда ``match`` заполняет то, что удаётся
сопоставить автоматически, а для остального печатает отчёт с подсказками.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import chngf
from .refdata import RefData, load_defaults, norm_class, norm_text


@dataclass
class Mapping:
    classes: dict[str, str | None] = field(default_factory=dict)      # класс ХроноГрафа → название класса МЭШ
    subjects: dict[str, list[str] | None] = field(default_factory=dict)  # предмет → список названий предметов МЭШ
    teachers: dict[str, int | None] = field(default_factory=dict)     # ФИО → id профиля учителя
    rooms: dict[str, int | None] = field(default_factory=dict)        # кабинет → id кабинета
    room_prefixes: dict[str, int | None] = field(default_factory=dict)  # префикс кабинета → id здания
    groups: dict[str, int | None] = field(default_factory=dict)       # "класс|предмет|группа" → id группы МЭШ
    force_teachers: dict[str, int] = field(default_factory=dict)      # "класс|предмет|группа" → id учителя МЭШ (принудительно)
    ignore_subjects: list[str] = field(default_factory=list)          # предметы, которые не переносим
    ignore_classes: list[str] = field(default_factory=list)
    # настройки (значения по умолчанию — из defaults.json, копируются в файл сопоставления при сохранении)
    subject_synonyms: dict[str, list[str]] = field(default_factory=lambda: load_defaults()["subject_synonyms"])  # norm_text(предмет) → названия в МЭШ
    ec_durations: list[int] = field(default_factory=lambda: load_defaults()["ec_durations"])          # длительности занятий ВД, мин
    ec_course_suffix: str = field(default_factory=lambda: load_defaults()["ec_course_suffix"])        # regex хвоста с классами в названии курса ВД
    group_ordinal_suffix: str = field(default_factory=lambda: load_defaults()["group_ordinal_suffix"])  # regex номера подгруппы в названии группы

    KEYS = ("classes", "subjects", "teachers", "rooms", "room_prefixes", "groups", "force_teachers",
            "ignore_subjects", "ignore_classes")
    SETTINGS = ("subject_synonyms", "ec_durations", "ec_course_suffix", "group_ordinal_suffix")

    @classmethod
    def load(cls, path) -> Mapping:
        """Сопоставление из файла; настройки, которых в файле нет, берутся из defaults.json."""
        m = cls()
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            for k in cls.KEYS + cls.SETTINGS:
                if k in d:
                    setattr(m, k, d[k])
            try:
                m.check_settings()
            except ValueError as e:
                raise ValueError(f"{path}: {e}") from e
        return m

    def check_settings(self) -> None:
        """Настройки правит пользователь (в файле или в окне): проверяем заранее, чтобы ошибка была
        понятной, а не падением где-то в разборе названий групп."""
        for name in ("ec_course_suffix", "group_ordinal_suffix"):
            pattern = getattr(self, name)
            if not isinstance(pattern, str):
                raise ValueError(f"{name}: ожидается строка с регулярным выражением")
            try:
                rx = re.compile(pattern)
            except re.error as e:
                raise ValueError(f"{name}: ошибка в регулярном выражении {pattern!r}: {e}") from e
            if name == "group_ordinal_suffix" and pattern and rx.groups < 1:
                raise ValueError(f"{name}: в регулярном выражении {pattern!r} нужна группа захвата "
                                 r"с номером подгруппы, например (\d+)гр$")
        if not isinstance(self.ec_durations, list) or not all(isinstance(x, int) for x in self.ec_durations):
            raise ValueError("ec_durations: ожидается список целых чисел (минуты)")

    def save(self, path) -> None:
        d = {"_help": [
            "classes: класс ХроноГрафа → название класса в МЭШ (null — не сопоставлен).",
            "subjects: предмет ХроноГрафа → список названий предметов МЭШ или курсов внеурочной деятельности в порядке приоритета (null — не сопоставлен): уроки недели ставятся в группу первого названия, пока не исчерпаны её часы по нагрузке учителя, затем — второго и т. д.; в пределах названия обычные группы имеют приоритет над группами ВД.",
            "teachers: ФИО учителя из ХроноГрафа → id профиля учителя в МЭШ.",
            "rooms: кабинет ХроноГрафа → id кабинета в МЭШ.  room_prefixes: префикс кабинета → id здания (для автоподбора).",
            "groups: 'класс|предмет|номер группы' → id группы МЭШ (обычной или внеурочной деятельности) для первого приоритета, 'класс|предмет|номер группы|2' — для второго и т. д.; заполняется только там, где автоматика не справилась, но можно задать и вручную.",
            "force_teachers: ключ как в groups → id учителя МЭШ, от имени которого ставить занятия в привязанной группе; нужен, когда учитель занятия не привязан к выбранной группе МЭШ (сервер принимает только учителя группы).",
            "ignore_subjects / ignore_classes: что не переносить.",
            "subject_synonyms: нормализованное название предмета ХроноГрафа (строчные буквы, без знаков препинания) → названия предметов/курсов МЭШ, которые подставляются при автосопоставлении.",
            "ec_durations: допустимые длительности занятия ВД в минутах; ec_course_suffix: regex хвоста с классами в названии курса ВД; group_ordinal_suffix: regex номера подгруппы («… 2гр») в названии группы МЭШ.",
        ]}
        for k in self.KEYS + self.SETTINGS:
            d[k] = getattr(self, k)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------------- вспомогательное
def split_person(name: str) -> tuple[str, str, str]:
    """'Иванова М.А.' → ('иванова', 'м', 'а'); 'Иванов Иван Петрович' → ('иванов', 'и', 'п')."""
    s = norm_text(name.replace(".", ". "))
    parts = s.split()
    if not parts:
        return "", "", ""
    last = parts[0]
    ini = [p[0] for p in parts[1:3]]
    return last, (ini[0] if len(ini) > 0 else ""), (ini[1] if len(ini) > 1 else "")


def person_key(name: str) -> str:
    last, a, b = split_person(name)
    return f"{last} {a}{b}".strip()


@dataclass
class RoomName:
    prefix: str
    number: str      # нормализованный номер ('05' → '5', '12/1' → '12-1') либо '' для именованных
    text: str        # нормализованный текст целиком


_ROOM_RE = re.compile(r"^([^\d]*?)(\d+)([a-zа-я]?)(?:[\s/\-]+(\d+|[a-zа-я]))?$", re.I)


def parse_room(name: str) -> RoomName:
    s = (name or "").strip()
    m = _ROOM_RE.match(s)
    if m:
        prefix, num, letter, sub = m.group(1), m.group(2).lstrip("0") or "0", (m.group(3) or "").lower(), m.group(4)
        number = num + letter + (("-" + sub) if sub else "")
        return RoomName(prefix.strip().lower().replace("ё", "е"), number, norm_text(s))
    return RoomName("", "", norm_text(s))


def room_number_of(room: dict) -> str:
    """Номер кабинета МЭШ в том же нормализованном виде, что и parse_room().number."""
    for key in ("number", "name"):
        v = room.get(key) or ""
        rn = parse_room(v)
        if rn.number and not rn.prefix:
            return rn.number
    return ""


# ---------------------------------------------------------------------- отчёт о сопоставлении
@dataclass
class MatchReport:
    lines: list[str] = field(default_factory=list)
    unresolved: int = 0

    def add(self, s: str) -> None:
        self.lines.append(s)

    def problem(self, s: str) -> None:
        self.unresolved += 1
        self.lines.append(s)

    def text(self) -> str:
        return "\n".join(self.lines)


# ---------------------------------------------------------------------- автосопоставление
def match_all(tt: chngf.Timetable, rd: RefData, m: Mapping) -> MatchReport:
    rep = MatchReport()
    lessons = relevant_lessons(tt, m)
    _match_classes(tt, rd, m, rep)
    _match_subjects(lessons, rd, m, rep)
    _match_teachers(tt, lessons, rd, m, rep)
    _match_rooms(lessons, rd, m, rep)
    _match_groups(tt, lessons, rd, m, rep)
    return rep


def relevant_lessons(tt: chngf.Timetable, m: Mapping) -> list[chngf.Lesson]:
    """Занятия, которые подлежат переносу (без игнорируемых классов и предметов)."""
    return [l for l in tt.iter_lessons()
            if l.cls not in m.ignore_classes and l.subject not in m.ignore_subjects]


def _used_subjects(lessons) -> Counter:
    c: Counter = Counter()
    for l in lessons:
        c[l.subject] += 1
    return c


def _match_classes(tt, rd, m, rep):
    rep.add("== Классы ==")
    for cls in tt.classes:
        if cls.name in m.ignore_classes:
            continue
        cur = m.classes.get(cls.name)
        if cur and rd.class_unit(cur):
            continue
        cu = rd.class_unit(cls.name)
        if cu:
            m.classes[cls.name] = cu["name"]
        else:
            m.classes.setdefault(cls.name, None)
            cands = difflib.get_close_matches(norm_class(cls.name), list(rd.class_unit_by_name), n=3, cutoff=0.6)
            rep.problem(f"  класс {cls.name!r}: не найден в МЭШ" + (f"; похожие: {', '.join(cands)}" if cands else "")
                        + ("  (в кэше есть классы только части параллелей?)" if len(rd.class_units) < 20 else ""))
    ok = sum(1 for v in m.classes.values() if v)
    rep.add(f"  сопоставлено {ok} из {len([c for c in tt.classes if c.name not in m.ignore_classes])}")


def _match_subjects(lessons, rd, m, rep):
    rep.add("== Предметы (предметы групп МЭШ и курсы внеурочной деятельности) ==")
    ec_only = set(rd.ec_course_names) - set(rd.subject_names)
    mesh_names = sorted(set(rd.subject_names) | ec_only)
    by_norm = {norm_text(n): n for n in mesh_names}
    used = _used_subjects(lessons)
    for name, cnt in sorted(used.items(), key=lambda x: -x[1]):
        if name in m.ignore_subjects:
            continue
        cur = m.subjects.get(name)
        n = norm_text(name)
        if cur:
            missing = [x for x in cur if x not in mesh_names]
            if missing:
                rep.problem(f"  предмет {name!r}: в сопоставлении указаны названия, которых нет среди групп МЭШ: {', '.join(missing)}")
            if n in by_norm and by_norm[n] not in cur:
                # точное совпадение вне списка (например, новый курс ВД): список задан пользователем и мог быть
                # сужен намеренно — только подсказываем, сами не дописываем
                rep.add(f"  предмет {name!r}: есть точное совпадение {by_norm[n]!r}, не входящее в список — добавьте при необходимости")
            continue
        found: list[str] = []
        if n in by_norm:
            found.append(by_norm[n])
        for syn in m.subject_synonyms.get(n, []):
            if syn in mesh_names and syn not in found:
                found.append(syn)
        if found:
            m.subjects[name] = found
            if all(f in ec_only for f in found):
                rep.add(f"  предмет {name!r} ({cnt} уроков) → курс ВД {', '.join(found)}")
            continue
        m.subjects.setdefault(name, None)
        sugg = difflib.get_close_matches(n, list(by_norm), n=4, cutoff=0.5)
        # плюс названия, содержащие слово из названия предмета
        words = [w for w in n.split() if len(w) > 3]
        for mn in by_norm:
            if any(w in mn for w in words) and mn not in sugg:
                sugg.append(mn)
        variants = [by_norm[x] + (" (ВД)" if by_norm[x] in ec_only else "") for x in sugg[:6]]
        rep.problem(f"  предмет {name!r} ({cnt} уроков): не сопоставлен; варианты: {', '.join(variants)}")
    ok = sum(1 for k, v in m.subjects.items() if v and k in used)
    rep.add(f"  сопоставлено {ok} из {len([s for s in used if s not in m.ignore_subjects])}")


def _match_teachers(tt, lessons, rd, m, rep):
    rep.add("== Учителя ==")
    by_key: dict[str, list[dict]] = defaultdict(list)
    by_last: dict[str, list[dict]] = defaultdict(list)
    for t in rd.teachers:
        if not t.get("name"):
            continue
        last, a, b = split_person(t["name"])
        by_last[last].append(t)
        by_key[f"{last} {a}"].append(t)
    in_groups = {t.get("id") for g in rd.groups for t in g.get("teachers") or []}
    used_teachers = {l.teacher_index for l in lessons if l.teacher_index >= 0}
    total = 0
    for t in tt.teachers:
        if t.index not in used_teachers:
            continue
        total += 1
        name = t.name
        cur = m.teachers.get(name)
        if cur and rd.teacher_deleted(cur):
            rep.problem(f"  учитель {name!r}: профиль {rd.teacher_by_id[cur].get('name')} (id {cur}) удалён в МЭШ — "
                        "подбираем заново")
        elif cur and cur in rd.teacher_by_id:
            continue
        last, a, b = split_person(name)
        cands = by_last.get(last, [])
        if a:
            cands2 = [c for c in cands if split_person(c["name"])[1] in ("", a)]
            if b:
                cands3 = [c for c in cands2 if split_person(c["name"])[2] in ("", b)]
                if cands3:
                    cands2 = cands3
            if cands2:
                cands = cands2
        if len(cands) > 1:      # дубли профилей: берём тот, что числится в группах
            active = [c for c in cands if c["id"] in in_groups]
            if len(active) == 1:
                cands = active
        if len(cands) == 1:
            m.teachers[name] = cands[0]["id"]
            continue
        m.teachers.setdefault(name, None)
        if not cands:
            close = difflib.get_close_matches(last, list(by_last), n=3, cutoff=0.75)
            sugg = [f"{c['name']} (id {c['id']})" for k in close for c in by_last[k]]
        else:
            sugg = [f"{c['name']} (id {c['id']})" for c in cands]
        rep.problem(f"  учитель {name!r}: {'не найден' if not cands else 'несколько кандидатов'}; варианты: {', '.join(sugg)}")
    ok = sum(1 for t in tt.teachers if t.index in used_teachers and m.teachers.get(t.name))
    rep.add(f"  сопоставлено {ok} из {total}")


def _match_rooms(lessons, rd, m, rep):
    rep.add("== Кабинеты ==")
    used: Counter = Counter()
    for l in lessons:
        if l.cabinet:
            used[l.cabinet] += 1
    if not used:
        rep.add("  кабинеты в расписании не назначены")
        return
    rooms = [r for r in rd.rooms if not r.get("deleted_at")]
    by_building_number: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for r in rooms:
        num = room_number_of(r)
        if num:
            by_building_number[(r["building_id"], num)].append(r)
    buildings = sorted({r["building_id"] for r in rooms})
    # подбор здания для каждого префикса по числу совпавших номеров. Номера в зданиях пересекаются,
    # поэтому здания разбираются по очереди, от самого уверенного совпадения, и здание, уже занятое другим
    # префиксом, при сравнении с ближайшим конкурентом не учитывается. Но если лучше всего номера префикса
    # совпадают именно с занятым зданием (префикс может обозначать крыло того же здания), решение не принимается.
    parsed = {name: parse_room(name) for name in used}
    numbers: dict[str, set[str]] = defaultdict(set)
    for rn in parsed.values():
        if rn.number:
            numbers[rn.prefix].add(rn.number)
    taken = {b for b in m.room_prefixes.values() if b}
    pending = {pfx for pfx in numbers if not m.room_prefixes.get(pfx)}

    def scores(pfx: str) -> Counter:
        return Counter(b for num in numbers[pfx] for b in buildings if (b, num) in by_building_number)

    def candidate(pfx: str) -> tuple[int, int, int, str] | None:
        """(совпало, -у ближайшего свободного, здание, префикс), если выбор однозначен, иначе None."""
        sc = scores(pfx)
        free = [(n, b) for b, n in sc.most_common() if b not in taken]
        if not free or free[0][0] < max(sc.values()):
            return None
        n, b = free[0]
        second = free[1][0] if len(free) > 1 else 0
        return (n, -second, b, pfx) if n >= 3 and n > second * 1.5 else None

    while pending:
        best = max(filter(None, map(candidate, pending)), default=None)
        if best is None:
            break
        n, neg_second, b, pfx = best
        m.room_prefixes[pfx] = b
        taken.add(b)
        pending.discard(pfx)
        rep.add(f"  префикс {pfx!r} → здание {b} (совпало номеров: {n}, у ближайшего другого: {-neg_second})")
    for pfx in sorted(pending):
        m.room_prefixes.setdefault(pfx, None)
        sc = scores(pfx)
        if sc:
            rep.problem(f"  префикс {pfx!r}: здание не определено однозначно, совпадения по зданиям: {', '.join(f'{b}: {n}' for b, n in sc.items())}")
        else:
            rep.problem(f"  префикс {pfx!r}: ни один номер не совпал с кабинетами МЭШ")
    for name, cnt in sorted(used.items(), key=lambda x: -x[1]):
        cur = m.rooms.get(name)
        if cur and cur in rd.room_by_id:
            continue
        rn = parsed[name]
        b = m.room_prefixes.get(rn.prefix)
        cands = by_building_number.get((b, rn.number), []) if (b and rn.number) else []
        if not cands:   # точное совпадение названия/номера (именованные кабинеты: «МШ1», «Актовый зал»)
            cands = [r for r in rooms if rn.text in (norm_text(r.get("name")), norm_text(r.get("number")))]
        if len(cands) == 1:
            m.rooms[name] = cands[0]["id"]
            continue
        m.rooms.setdefault(name, None)
        names = {f"{r['number']} / {r['name']} (здание {r['building_id']}, id {r['id']})": norm_text(f"{r['number']} {r['name']}") for r in rooms}
        sugg = difflib.get_close_matches(rn.text, list(names.values()), n=4, cutoff=0.4)
        sugg_txt = [k for k, v in names.items() if v in sugg][:4]
        rep.problem(f"  кабинет {name!r} ({cnt} уроков): не сопоставлен; варианты: {', '.join(sugg_txt)}")
    ok = sum(1 for k in used if m.rooms.get(k))
    rep.add(f"  сопоставлено {ok} из {len(used)}")


def group_key(l: chngf.Lesson, prio: int = 1) -> str:
    """Ключ ручного сопоставления группы: «класс|предмет|номер группы», для 2-го и следующих
    приоритетов предмета — с хвостом «|2», «|3»…"""
    return f"{l.cls}|{l.subject}|{l.group}" + (f"|{prio}" if prio > 1 else "")


def split_group_key(key: str) -> tuple[str, str, str, int]:
    """(класс, предмет, номер группы, приоритет) из ключа ``groups``."""
    parts = key.split("|")
    prio = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
    return parts[0], parts[1], parts[2] if len(parts) > 2 else "", prio


def _group_ordinal(g: dict, suffix_re: str) -> int:
    m = re.search(suffix_re, g.get("name") or "") if suffix_re else None
    return int(m.group(1)) if m and m.lastindex and m.group(1).isdigit() else 1


def group_candidates(l: chngf.Lesson, rd: RefData, m: Mapping,
                     subj_names: list[str] | None = None) -> tuple[list[dict], str]:
    """Группы МЭШ класса по предмету занятия: обычные группы, а если их нет — группы внеурочной
    деятельности.  ``subj_names`` — названия предметов МЭШ, среди групп которых искать (по умолчанию
    все из сопоставления предмета).  Возвращает (кандидаты, причина отсутствия)."""
    cu_name = m.classes.get(l.cls)
    cu = rd.class_unit(cu_name) if cu_name else None
    if not cu:
        return [], "класс не сопоставлен"
    if subj_names is None:
        subj_names = m.subjects.get(l.subject) or []
    if not subj_names:
        return [], "предмет не сопоставлен"
    for pool in (rd.groups_by_class, rd.ec_groups_by_class):
        cands = [g for g in pool.get(cu["id"], []) if g.get("subject_name") in subj_names]
        if cands:
            return cands, ""
    return [], f"в классе {cu['name']} нет групп (ни обычных, ни ВД) по предметам {', '.join(subj_names)}"


def is_ec(g: dict | None) -> bool:
    return bool(g) and g.get("kind") == "ec"


def group_teacher_ids(g: dict | None) -> list[int]:
    return [t["id"] for t in (g or {}).get("teachers") or [] if t.get("id")]


def pick_teacher(g: dict | None, teacher_id: int | None, forced: int | None = None) -> tuple[int | None, str]:
    """Учитель, от имени которого занятие ставится в группу ``g``: (id, пояснение).

    Сервер принимает только учителя, привязанного к группе (удалённые профили из списка группы уже
    убраны), поэтому учитель занятия подходит не всегда.  ``forced`` — принудительная привязка из
    ``Mapping.force_teachers``: она имеет приоритет над сопоставленным учителем.  ``None`` в ответе —
    занятие в эту группу поставить нельзя; в том числе в группу без учителей: сервер отвечает «Учитель
    не привязан к переданной группе».
    """
    ids = group_teacher_ids(g)
    if forced is not None and forced in ids:
        return forced, "учитель задан принудительно"
    if teacher_id is not None and teacher_id in ids:
        return teacher_id, ""
    if teacher_id is None and len(ids) == 1:
        return ids[0], "учитель взят из группы"
    return None, ""


def _resolve(l: chngf.Lesson, rd: RefData, m: Mapping, teacher_id: int | None,
             prio: int = 1, subj_names: list[str] | None = None) -> tuple[dict | None, str, list[dict]]:
    """(группа, пояснение, оставшиеся кандидаты): кандидаты непусты только при неоднозначности —
    по ним дальше выбирает :class:`GroupResolver`."""
    key = group_key(l, prio)
    explicit = m.groups.get(key)
    if explicit:
        g = rd.any_group(explicit)
        return (g, "по явному сопоставлению", []) if g else (None, f"группа id {explicit} из сопоставления не найдена в кэше", [])
    cands, why = group_candidates(l, rd, m, subj_names)
    if not cands:
        return None, why, []
    if len(cands) == 1:
        return cands[0], "единственная группа" + (" ВД" if is_ec(cands[0]) else ""), []
    # поток из нескольких классов ↔ метагруппа с тем же набором классов; иначе — обычные группы класса
    if l.potok:
        want = {norm_class(m.classes.get(c) or c) for c in l.potok}
        meta = [g for g in cands if g.get("is_metagroup") and
                {norm_class(rd.class_unit_by_id[i]["name"]) for i in g.get("class_unit_ids") or [] if i in rd.class_unit_by_id} == want]
        if len(meta) == 1:
            return meta[0], "метагруппа потока", []
        if meta:
            cands = meta
    else:
        plain = [g for g in cands if not g.get("is_metagroup")]
        if plain:
            cands = plain
        if len(cands) == 1:
            return cands[0], "единственная группа класса", []
    if teacher_id is not None:
        by_teacher = [g for g in cands if any(t.get("id") == teacher_id for t in g.get("teachers") or [])]
        if len(by_teacher) == 1:
            return by_teacher[0], "по учителю", []
        if len(by_teacher) > 1:
            cands = by_teacher
    return (None, "несколько групп: " + "; ".join(f"{g['name']} (id {g['id']}, учителя: {', '.join(t.get('full_name') or '?' for t in g.get('teachers') or []) or '—'})" for g in cands),
            cands)


def resolve_group(l: chngf.Lesson, rd: RefData, m: Mapping, teacher_id: int | None) -> tuple[dict | None, str]:
    """Находит группу МЭШ для занятия; возвращает (группа, пояснение)."""
    g, why, _ = _resolve(l, rd, m, teacher_id)
    return g, why


@dataclass
class Link:
    """Звено цепочки приоритетов: название предмета МЭШ и подобранная по нему группа (или причина,
    почему не подобрана)."""
    prio: int
    name: str
    group: dict | None
    why: str


class GroupResolver:
    """Выбор группы МЭШ для каждой классогруппы с учётом идентичных групп.

    Сначала разрешаются однозначные случаи (по классу+предмету, учителю, потоку).
    Для оставшихся неоднозначностей (подгруппы одного учителя) используется
    информация ХроноГрафа об идентичных группах: если подгруппа «Технология/2»
    состоит из тех же учеников, что «Английский/1», а та уже сопоставлена с
    группой МЭШ, берётся кандидат с наибольшим пересечением по ученикам.
    Последний довод — номер подгруппы в названии группы МЭШ.

    Предмету ХроноГрафа может соответствовать несколько названий МЭШ по приоритету
    (``Mapping.subjects``): :meth:`resolve_chain` подбирает группу по каждому названию отдельно,
    и план недели заполняет группу 1-го приоритета до её часов, затем 2-го и т. д.
    """

    def __init__(self, tt: chngf.Timetable, rd: RefData, m: Mapping):
        self.tt, self.rd, self.m = tt, rd, m
        by_key = {g.key: g.index for g in tt.clsgrps}
        self.identical: dict[int, set[int]] = {}
        for grp in tt.ident_base.ident_groups:
            idx = {by_key[k] for k in grp if k in by_key}
            for i in idx:
                self.identical[i] = idx - {i}
        self._cache: dict[int, list[Link]] = {}

    def teacher_id(self, l: chngf.Lesson) -> int | None:
        return self.m.teachers.get(l.teacher) if l.teacher else None

    def forced_teacher(self, l: chngf.Lesson, prio: int = 1) -> int | None:
        """Учитель, принудительно заданный для этой классогруппы (``Mapping.force_teachers``)."""
        return self.m.force_teachers.get(group_key(l, prio))

    def any_forced(self, l: chngf.Lesson) -> bool:
        """Задана ли принудительная привязка учителя хотя бы для одного звена классогруппы."""
        return any(self.forced_teacher(l, lk.prio) for lk in self.resolve_chain(l))

    def teacher_for(self, l: chngf.Lesson, link: Link) -> tuple[int | None, str]:
        """Учитель для занятия в подобранной группе звена: (id, пояснение); ``None`` — группа не
        примет это занятие (учитель не привязан к ней)."""
        return pick_teacher(link.group, self.teacher_id(l), self.forced_teacher(l, link.prio))

    def resolve(self, l: chngf.Lesson) -> tuple[dict | None, str]:
        """Группа 1-го приоритета (и пояснение) — как в отчёте и на вкладке «Группы»."""
        first = self.resolve_chain(l)[0]
        return first.group, first.why

    def resolve_chain(self, l: chngf.Lesson) -> list[Link]:
        """Звенья по названиям предметов МЭШ из сопоставления, в порядке приоритета.  Названия, по
        которым в классе нет ни одной группы, пропускаются (если только не задана группа вручную);
        первое звено есть всегда — при полной неудаче оно несёт причину."""
        if l.clsgrp_index in self._cache:
            return self._cache[l.clsgrp_index]
        names = self.m.subjects.get(l.subject) or []
        chain: list[Link] = []
        for prio, name in enumerate(names, 1):
            if not self.m.groups.get(group_key(l, prio)) and not group_candidates(l, self.rd, self.m, [name])[0]:
                continue
            g, why = self._resolve_one(l, prio, [name])
            chain.append(Link(prio, name, g, why))
        if not chain:
            g, why = self._resolve_one(l, 1, None)
            chain.append(Link(1, names[0] if names else "", g, why))
        self._cache[l.clsgrp_index] = chain
        return chain

    def _resolve_one(self, l: chngf.Lesson, prio: int, subj_names: list[str] | None) -> tuple[dict | None, str]:
        g, why, cands = _resolve(l, self.rd, self.m, self.teacher_id(l), prio, subj_names)
        if g is None and cands and l.clsgrp_index in self.identical:
            g2, why2 = self._by_students(l, cands)
            if g2 is not None:
                g, why = g2, why2
        if g is None and cands and l.group > 0:
            g, why = self._by_ordinal(l, cands, why)
        return g, why

    def _by_students(self, l: chngf.Lesson, cands: list[dict]) -> tuple[dict | None, str]:
        # уже однозначно сопоставленные идентичные подгруппы того же класса
        ref_students: set[int] = set()
        ref_names = []
        for i in self.identical.get(l.clsgrp_index, ()):
            other = self._lesson_for_clsgrp(i)
            if other is None:
                continue
            g, why = resolve_group(other, self.rd, self.m, self.teacher_id(other))
            if g is not None and "проверьте" not in why and g.get("student_ids"):
                ref_students |= set(g["student_ids"])
                ref_names.append(g["name"])
        if not ref_students:
            return None, ""
        scored = []
        for g in cands:
            st = set(g.get("student_ids") or [])
            if st:
                scored.append((len(st & ref_students) / len(st | ref_students), g))
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][0] >= 0.5 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            return scored[0][1], f"по составу учеников (как {ref_names[0]}, совпадение {scored[0][0]:.0%})"
        return None, ""

    def _by_ordinal(self, l: chngf.Lesson, cands: list[dict], why: str) -> tuple[dict | None, str]:
        by_ord = [g for g in cands if _group_ordinal(g, self.m.group_ordinal_suffix) == l.group]
        if len(by_ord) == 1:
            return by_ord[0], "по номеру группы (проверьте!)"
        return None, why

    def _lesson_for_clsgrp(self, index: int) -> chngf.Lesson | None:
        if not hasattr(self, "_first_lesson"):
            self._first_lesson: dict[int, chngf.Lesson] = {}
            for x in self.tt.iter_lessons():
                self._first_lesson.setdefault(x.clsgrp_index, x)
        return self._first_lesson.get(index)


def _match_groups(tt, lessons, rd, m, rep):
    rep.add("== Группы (класс|предмет|группа → группа МЭШ) ==")
    seen: dict[str, chngf.Lesson] = {}
    per_week: Counter = Counter()          # (ключ, неделя цикла) → уроков
    for l in lessons:
        seen.setdefault(group_key(l), l)
        per_week[(group_key(l), l.week)] += 1
    n_ok = n_weak = n_ec = n_spill = n_over = n_noteacher = 0
    resolver = GroupResolver(tt, rd, m)
    for key, l in seen.items():
        chain = resolver.resolve_chain(l)
        g, why = chain[0].group, chain[0].why
        tid = None
        if g:
            n_ok += 1
            n_ec += is_ec(g)
            tid, note = resolver.teacher_for(l, chain[0])
            if tid is None:
                # сервер примет занятие только от учителя группы: нужна принудительная привязка
                n_noteacher += 1
                names = ", ".join(t.get("full_name") or "?" for t in g.get("teachers") or []) or "—"
                rep.problem(f"  {key} → {g['name']} (id {g['id']}): учитель {l.teacher!r} не привязан к группе МЭШ "
                            f"(в группе: {names}) — занятия будут пропущены; чтобы поставить их от учителя группы, "
                            f'задайте force_teachers[{key!r}] = id учителя (в окне — двойной щелчок по столбцу «Учитель»)')
            elif note:
                rep.add(f"  {key} → {g['name']} (id {g['id']}) — {note}")
            if "проверьте" in why:
                n_weak += 1
                rep.add(f"  {key} → {'ВД ' if is_ec(g) else ''}{g['name']} (id {g['id']}) — {why}")
        elif why not in ("класс не сопоставлен", "предмет не сопоставлен"):      # иначе уже отражено выше
            m.groups.setdefault(key, None)
            rep.problem(f"  {key} (учитель {l.teacher!r}): {why}")
        for link in chain[1:]:
            if link.group is None:
                m.groups.setdefault(group_key(l, link.prio), None)
                rep.problem(f"  {key}, приоритет {link.prio} «{link.name}» (учитель {l.teacher!r}): {link.why}")
        # хватит ли часов группе 1-го приоритета: иначе лишние уроки уйдут по следующим приоритетам
        # или встанут сверх часов
        if g is None:
            continue
        n_les = max(per_week[(key, w)] for w in range(max(1, tt.week_count)))
        # нули (каникулы, недели без нагрузки) не показательны — сравниваем с наименьшей ненулевой нагрузкой
        hours = [h for wk in rd.schedule_by_week if (h := rd.group_hours(g, wk, tid))]
        if not hours or n_les <= min(hours):
            continue
        rest = [lk for lk in chain[1:] if lk.group]
        if rest:
            n_spill += 1
            rep.add(f"  {key}: уроков в неделю {n_les}, часов у «{g['name']}» {min(hours)} — "
                    f"остальные пойдут в {', '.join(f'«{lk.group['name']}»' for lk in rest)}")
        else:
            n_over += 1
            rep.add(f"  {key}: уроков в неделю {n_les}, часов у «{g['name']}» {min(hours)} — лишние встанут сверх часов "
                    "(при необходимости добавьте предмету второе название МЭШ)")
    rep.add(f"  сопоставлено {n_ok} из {len(seen)} (из них группы ВД: {n_ec}; по номеру группы, требуют проверки: {n_weak}"
            + (f"; учитель не привязан к группе: {n_noteacher}" if n_noteacher else "")
            + (f"; уроков больше часов группы: с переносом в следующий приоритет {n_spill}, сверх часов {n_over}"
               if n_spill or n_over else "") + ")")
