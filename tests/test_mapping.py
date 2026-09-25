import datetime

import pytest

from timetable_upload import chngf, mapping, refdata, uploader
from timetable_upload.mapping import Mapping, parse_room, person_key, resolve_group, split_person


def test_split_person():
    assert split_person("Иванова М.А.") == ("иванова", "м", "а")
    assert split_person("Иванов Иван Петрович") == ("иванов", "и", "п")
    assert split_person("Лазар") == ("лазар", "", "")
    assert person_key("Алёшина О.П.") == person_key("Алешина Ольга Павловна")


def test_parse_room():
    r = parse_room("к12/1")
    assert (r.prefix, r.number) == ("к", "12-1")
    assert parse_room("05").number == "5" and parse_room("05").prefix == ""
    assert parse_room("_10а").number == "10а" and parse_room("_10а").prefix == "_"
    assert parse_room("Б.сп.зал").number == ""
    assert mapping.room_number_of({"number": "12-2", "name": "12-2 Иностранный язык"}) == "12-2"
    assert mapping.room_number_of({"number": "3", "name": "03 Информатика"}) == "3"


def _rd():
    rd = refdata.RefData()
    rd.class_units = [{"id": 1, "name": "11-В", "class_level_id": 11}, {"id": 2, "name": "11-Б", "class_level_id": 11}]
    rd.groups = [
        {"id": 10, "name": "Иностранный (английский) язык 11-В 11в", "subject_name": "Иностранный (английский) язык",
         "class_unit_id": 1, "class_unit_ids": [1], "is_metagroup": False, "teachers": [{"id": 100, "full_name": "Лебедева Анна Сергеевна"}]},
        {"id": 11, "name": "Иностранный (английский) язык 11-В 1 2гр", "subject_name": "Иностранный (английский) язык",
         "class_unit_id": 1, "class_unit_ids": [1], "is_metagroup": False, "teachers": [{"id": 101, "full_name": "Зайцева Мария Игоревна"}]},
        {"id": 12, "name": "Физическая культура 11пт", "subject_name": "Физическая культура",
         "class_unit_id": None, "class_unit_ids": [1, 2], "is_metagroup": True, "teachers": [{"id": 102, "full_name": "Смирнов"}]},
        {"id": 13, "name": "Физическая культура 11-В 11в", "subject_name": "Физическая культура",
         "class_unit_id": 1, "class_unit_ids": [1], "is_metagroup": False, "teachers": [{"id": 102, "full_name": "Смирнов"}]},
    ]
    rd.rooms = [{"id": 500, "number": "12-2", "name": "12-2 Иностранный язык", "building_id": 7}]
    rd.teacher_profiles = [{"id": 100, "name": "Лебедева Анна Сергеевна"}, {"id": 101, "name": "Зайцева Мария Игоревна"},
                           {"id": 102, "name": "Смирнов Павел Олегович"}]
    rd.schedules = [{"id": 900, "week_number": 16}]
    bells = [{"id": 7000 + n, "is_lesson": True, "ordinal": n, "study_ordinal": n} for n in range(1, 8)]
    rd.bells_timetables = [{"id": 300, "name": "РП", "building_id": 7,
                            "bells_day_timetables": [{"week_day_id": d, "bells": bells} for d in range(1, 7)]}]
    rd.bells_timetable_assignments = [{"class_unit_id": 1, "bells_timetable_id": 300, "begin_date": "01.09.2025", "end_date": "31.08.2026"},
                                      {"class_unit_id": 2, "bells_timetable_id": 300, "begin_date": "01.09.2025", "end_date": "31.08.2026"}]
    # внеурочная деятельность: курс «Разговоры о важном 10-11» с группами по классам, «Физическая культура» тоже есть в ВД
    rd.ec_activities = [{"id": 50, "name": "Разговоры о важном 10-11", "teacher_ids": [100]},
                        {"id": 51, "name": "Физическая культура 11", "teacher_ids": [102]}]
    rd.ec_groups = [{"id": 600, "name": "Группа Разговоры о важном 11в ", "ec_activity_id": 50, "class_unit_ids": [1], "teacher_ids": [100], "student_ids": [1, 2]},
                    {"id": 601, "name": "Группа Разговоры о важном 11б", "ec_activity_id": 50, "class_unit_ids": [2], "teacher_ids": [101], "student_ids": [3]},
                    {"id": 602, "name": "Группа Физическая культура 11в", "ec_activity_id": 51, "class_unit_ids": [1], "teacher_ids": [102], "student_ids": [1]}]
    rd.ec_schedules = [{"id": 950, "week_number": 16}]
    rd._index()
    return rd


def test_match_rooms_prefixes_to_buildings():
    """Три здания с пересекающейся нумерацией: префиксы разбираются по очереди, занятое здание не мешает
    соседнему; префикс, чьи номера лучше всего совпадают с уже занятым зданием, остаётся нерешённым."""
    rd = refdata.RefData()
    plan = {1: ["1", "2", "3", "5", "6", "8", "10"] + [str(n) for n in range(14, 44)],   # основное
            2: ["4", "5"] + [str(n) for n in range(21, 58)],                              # «к»
            3: ["1", "2", "3", "7"] + [str(n) for n in range(11, 35)]}                    # «_»
    rd.rooms = [{"id": b * 1000 + i, "number": num, "name": num, "building_id": b}
                for b, nums in plan.items() for i, num in enumerate(nums)]
    rd._index()
    names = (["05", "06", "08", "10"] + [str(n) for n in range(14, 44)]
             + ["к4", "к5"] + [f"к{n}" for n in range(21, 58)]
             + ["_7"] + [f"_{n}" for n in range(11, 35)]
             + ["п1", "п2", "п3"])
    lessons = [_lesson("11в", "Физика", 0, "Смирнов", cabinet=n) for n in names]
    m, rep = Mapping(), mapping.MatchReport()
    mapping._match_rooms(lessons, rd, m, rep)
    assert m.room_prefixes == {"": 1, "к": 2, "_": 3, "п": None}
    assert rd.room_by_id[m.rooms["21"]]["building_id"] == 1
    assert rd.room_by_id[m.rooms["к21"]]["building_id"] == 2
    assert rd.room_by_id[m.rooms["_21"]]["building_id"] == 3
    assert m.rooms["п1"] is None and all(m.rooms[n] for n in names if not n.startswith("п"))
    # заданный вручную префикс не пересматривается
    m2 = Mapping(room_prefixes={"_": 1})
    mapping._match_rooms(lessons, rd, m2, mapping.MatchReport())
    assert m2.room_prefixes["_"] == 1 and rd.room_by_id[m2.rooms["_21"]]["building_id"] == 1


def test_ec_course_name_and_index():
    suffix = refdata.load_defaults()["ec_course_suffix"]
    assert refdata.ec_course_name("Разговоры о важном 2-4", suffix) == "Разговоры о важном"
    assert refdata.ec_course_name("Методы решения физических задач 9абм", suffix) == "Методы решения физических задач"
    assert refdata.ec_course_name("Робототехника", suffix) == "Робототехника"
    assert refdata.ec_course_name("Робототехника 5", "") == "Робототехника 5"
    rd = _rd()
    assert rd.ec_course_names == ["Разговоры о важном", "Физическая культура"]
    g = rd.ec_group_by_id[600]
    assert g["kind"] == "ec" and g["subject_name"] == "Разговоры о важном" and g["name"] == "Группа Разговоры о важном 11в"
    assert g["teachers"] == [{"id": 100, "full_name": "Лебедева Анна Сергеевна"}] and not g["is_metagroup"]
    assert [x["id"] for x in rd.ec_groups_by_class[1]] == [600, 602]
    assert rd.any_group(600) is g and rd.any_group(10)["id"] == 10


class _FakeTimetable:
    """Расписание из готового списка занятий (build_plan пользуется только iter_lessons и week_count)."""

    def __init__(self, lessons, week_count=1):
        self._lessons = lessons
        self.week_count = week_count
        self.clsgrps = []
        self.ident_base = chngf.KeyBase()

    def iter_lessons(self):
        return iter(self._lessons)


def _timetable(lessons, week_count=1):
    return _FakeTimetable(lessons, week_count)


_counter = iter(range(1, 10_000))


def _lesson(cls, subject, group, teacher, potok=(), day=0, lesson=0, cabinet="", week=0):
    idx = next(_counter)
    return chngf.Lesson(day=day, lesson=lesson, week=week, cls=cls, subject=subject, subject_short=subject,
                        base_subject=subject, group=group, teacher=teacher, teacher_index=0, cabinet=cabinet,
                        clsgrp_index=idx, clsgrp_key=idx, potok=potok)


def test_resolve_group_by_teacher_and_potok():
    rd = _rd()
    m = Mapping(classes={"11в": "11-В", "11Б": "11-Б"},
                subjects={"Английский язык": ["Иностранный (английский) язык"], "Физическая культура": ["Физическая культура"]},
                teachers={"Лебедева А.С.": 100, "Зайцева Мария Игоревна": 101, "Смирнов Павел Олегович": 102})
    g, why = resolve_group(_lesson("11в", "Английский язык", 2, "Зайцева Мария Игоревна"), rd, m, 101)
    assert g["id"] == 11 and why == "по учителю"
    g, why = resolve_group(_lesson("11в", "Физическая культура", 0, "Смирнов Павел Олегович", potok=("11Б", "11в")), rd, m, 102)
    assert g["id"] == 12 and "метагруппа" in why
    g, why = resolve_group(_lesson("11в", "Физическая культура", 0, "Смирнов Павел Олегович"), rd, m, 102)
    assert g["id"] == 13
    g, why = resolve_group(_lesson("11в", "Химия", 0, "Кто-то"), rd, m, None)
    assert g is None and "предмет" in why


def test_resolver_ordinal_ignores_metagroups():
    """Учитель не сопоставлен, подгруппа 2: метагруппа «…11пт 2гр» не мешает выбрать обычную группу класса
    по номеру (как делал resolve_group до выделения GroupResolver)."""
    rd = _rd()
    rd.groups.append({"id": 14, "name": "Иностранный (английский) язык 11пт 2гр", "subject_name": "Иностранный (английский) язык",
                      "class_unit_id": None, "class_unit_ids": [1, 2], "is_metagroup": True, "teachers": [{"id": 103, "full_name": "Кто-то"}]})
    rd._index()
    m = Mapping(classes={"11в": "11-В", "11Б": "11-Б"}, subjects={"Английский язык": ["Иностранный (английский) язык"]})
    l = _lesson("11в", "Английский язык", 2, "Неизвестный")
    g, why = mapping.GroupResolver(_timetable([l]), rd, m).resolve(l)
    assert g["id"] == 11 and "по номеру группы" in why
    # без номера подгруппы остаётся неоднозначность — с перечнем кандидатов
    l0 = _lesson("11в", "Английский язык", 0, "Неизвестный")
    g, why = mapping.GroupResolver(_timetable([l0]), rd, m).resolve(l0)
    assert g is None and why.startswith("несколько групп") and "11пт" not in why


def test_group_ordinal_tolerates_bad_regex():
    assert mapping._group_ordinal({"name": "Технология 5-А 2гр"}, r"(\d+)гр$") == 2
    assert mapping._group_ordinal({"name": "Технология 5-А 2гр"}, r"\d+гр$") == 1      # нет группы захвата
    assert mapping._group_ordinal({"name": "Технология 5-А 2гр"}, r"(гр)$") == 1       # не число
    with pytest.raises(ValueError, match="group_ordinal_suffix"):
        Mapping(group_ordinal_suffix=r"\d+гр$").check_settings()
    with pytest.raises(ValueError, match="ec_course_suffix"):
        Mapping(ec_course_suffix="(").check_settings()


def test_match_subjects_keeps_curated_list():
    """Точное совпадение вне заданного пользователем списка только подсказывается, список не расширяется."""
    rd = refdata.RefData()
    rd.groups = [{"id": 1, "subject_name": "История", "class_unit_id": 1},
                 {"id": 2, "subject_name": "История России", "class_unit_id": 1}]
    rd._index()
    m = Mapping(subjects={"История": ["История России"]})
    rep = mapping.MatchReport()
    mapping._match_subjects([_lesson("5а", "История", 0, "Кто-то")], rd, m, rep)
    assert m.subjects["История"] == ["История России"]
    assert any("точное совпадение 'История'" in line for line in rep.lines)


def test_resolve_group_falls_back_to_ec():
    rd = _rd()
    m = Mapping(classes={"11в": "11-В", "11Б": "11-Б"},
                subjects={"Разговоры о важном": ["Разговоры о важном"], "Физическая культура": ["Физическая культура"]},
                teachers={"Лебедева А.С.": 100, "Смирнов Павел Олегович": 102})
    # обычных групп по предмету нет → группа ВД класса
    g, why = resolve_group(_lesson("11в", "Разговоры о важном", 0, "Лебедева А.С."), rd, m, 100)
    assert g["id"] == 600 and mapping.is_ec(g) and "ВД" in why
    # обычные группы есть → ВД не рассматривается, даже если там тот же предмет
    g, why = resolve_group(_lesson("11в", "Физическая культура", 0, "Смирнов Павел Олегович"), rd, m, 102)
    assert g["id"] == 13 and not mapping.is_ec(g)
    # явное сопоставление может указывать на группу ВД
    m.groups["11в|Физическая культура|0"] = 602
    g, why = resolve_group(_lesson("11в", "Физическая культура", 0, "Смирнов Павел Олегович"), rd, m, 102)
    assert g["id"] == 602 and mapping.is_ec(g)


def test_build_plan_ec_item():
    rd = _rd()
    for day in rd.bells_timetables[0]["bells_day_timetables"]:
        for b in day["bells"]:
            b["begin_time"], b["end_time"] = f"{8 + b['ordinal']:02d}:00", f"{8 + b['ordinal']:02d}:45"
    m = Mapping(classes={"11в": "11-В", "11Б": "11-Б"}, subjects={"Разговоры о важном": ["Разговоры о важном"]},
                teachers={"Лебедева А.С.": 100})
    tt = _timetable([_lesson("11в", "Разговоры о важном", 0, "Лебедева А.С.", day=0, lesson=0)])
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert plan.ec_schedule_id == 950 and len(plan.items) == 1
    it = plan.items[0]
    assert (it.kind, it.group_id, it.begin_time, it.end_time, it.duration) == ("ec", 600, "09:00", "09:45", "00:45")
    assert it.room_id is None and it.building_id is None      # кабинет для ВД не подставляется
    assert it.key == ("ec", 600, "2025-12-15", "09:00")
    existing = [{"id": 5, "ec_group_id": 600, "date": "2025-12-15", "begin_time": "09:00", "end_time": "09:45",
                 "teacher_id": 100, "room_id": None, "class_unit_ids": [1], "ec_schedule_id": 950}]
    uploader.diff_plan(plan, existing, replace=False, rd=rd)
    assert plan.unchanged == 1 and plan.to_create == []
    # чужое занятие ВД того же класса удаляется только с replace
    other = {"id": 6, "ec_group_id": 602, "date": "2025-12-16", "begin_time": "09:00", "teacher_id": 102, "class_unit_ids": [1]}
    uploader.diff_plan(plan, existing + [other], replace=True, rd=rd)
    assert [d["id"] for d in plan.to_delete] == [6]
    assert uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), uploader.Selection(only="oo")).items == []
    # длительность звонка вне 30/35/40/45 минут — пропуск
    rd.bells_timetables[0]["bells_day_timetables"][0]["bells"][0]["end_time"] = "09:20"
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert plan.items == [] and "длительность" in plan.skipped[0][1]


def test_build_plan_and_diff():
    rd = _rd()
    m = Mapping(classes={"11в": "11-В", "11Б": "11-Б"},
                subjects={"Английский язык": ["Иностранный (английский) язык"], "Физическая культура": ["Физическая культура"]},
                teachers={"Лебедева А.С.": 100, "Смирнов Павел Олегович": 102}, rooms={"к12/2": 500})
    lessons = [
        _lesson("11в", "Английский язык", 1, "Лебедева А.С.", day=0, lesson=0, cabinet="к12/2"),
        _lesson("11в", "Физическая культура", 0, "Смирнов Павел Олегович", potok=("11Б", "11в"), day=1, lesson=2),
        _lesson("11Б", "Физическая культура", 0, "Смирнов Павел Олегович", potok=("11Б", "11в"), day=1, lesson=2),
        _lesson("11в", "Английский язык", 0, "Лебедева А.С.", day=5, lesson=7),   # нет такого звонка (8-й урок)
    ]
    tt = _timetable(lessons)
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert plan.schedule_id == 900
    assert len(plan.items) == 2            # поток из двух классов → одно занятие метагруппы
    assert len(plan.skipped) == 1 and "звонка" in plan.skipped[0][1]
    first = plan.items[0]
    assert (first.date, first.bell_id, first.room_id, first.building_id) == (datetime.date(2025, 12, 15), 7001, 500, 7)
    assert not first.room_guessed and plan.items[1].room_guessed     # у физкультуры кабинета нет — подставлен
    existing = [
        {"id": 1, "group_id": 10, "date": [2025, 12, 15], "bell_id": 7001, "teacher_id": 100, "room_id": 500, "class_unit_id": 1, "schedule_id": 900},
        {"id": 2, "group_id": 13, "date": [2025, 12, 17], "bell_id": 7002, "teacher_id": 102, "room_id": None, "class_unit_id": 1, "schedule_id": 900},
    ]
    uploader.diff_plan(plan, existing, replace=False, rd=rd)
    assert plan.unchanged == 1 and len(plan.to_create) == 1 and plan.to_delete == []
    uploader.diff_plan(plan, existing, replace=True, rd=rd)
    assert [d["id"] for d in plan.to_delete] == [2]
    # без replace отличающееся занятие не трогается, с replace — пересоздаётся
    existing[0]["teacher_id"] = 999
    uploader.diff_plan(plan, existing, replace=False, rd=rd)
    assert len(plan.differs) == 1 and plan.to_delete == [] and len(plan.to_create) == 1
    uploader.diff_plan(plan, existing, replace=True, rd=rd)
    assert sorted(d["id"] for d in plan.to_delete) == [1, 2] and len(plan.to_create) == 2


def test_guessed_room_is_not_compared():
    """Кабинет, подставленный по умолчанию, — догадка: занятие с другим кабинетом на сервере
    не считается отличающимся и при replace не пересоздаётся."""
    rd = _rd()
    m = Mapping(classes={"11в": "11-В", "11Б": "11-Б"}, subjects={"Физическая культура": ["Физическая культура"]},
                teachers={"Смирнов Павел Олегович": 102})
    tt = _timetable([_lesson("11в", "Физическая культура", 0, "Смирнов Павел Олегович", day=2, lesson=1),
                     _lesson("11в", "Физическая культура", 0, "Смирнов Павел Олегович", day=3, lesson=1, cabinet="к1")])
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert [p.room_guessed for p in plan.items] == [True, True] and all(p.room_id == 500 for p in plan.items)
    existing = [{"id": 1, "group_id": 13, "date": [2025, 12, 17], "bell_id": 7002, "teacher_id": 102, "room_id": 88784, "class_unit_id": 1},
                {"id": 2, "group_id": 13, "date": [2025, 12, 18], "bell_id": 7002, "teacher_id": 102, "room_id": 88784, "class_unit_id": 1}]
    uploader.diff_plan(plan, existing, replace=True, rd=rd)
    assert plan.unchanged == 2 and plan.to_delete == [] and plan.to_create == [] and plan.differs == []
    # сопоставленный кабинет сравнивается
    m.rooms["к1"] = 500
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    uploader.diff_plan(plan, existing, replace=False, rd=rd)
    assert plan.unchanged == 1 and [cur["id"] for cur, _ in plan.differs] == [2]


def test_build_plan_two_week_cycle():
    """Двухнедельный файл: в нечётные недели МЭШ идёт первая неделя цикла, в чётные — вторая."""
    rd = _rd()
    rd.schedules.append({"id": 901, "week_number": 17})
    rd._index()
    m = Mapping(classes={"11в": "11-В", "11Б": "11-Б"},
                subjects={"Английский язык": ["Иностранный (английский) язык"]}, teachers={"Лебедева А.С.": 100},
                rooms={"к12/2": 500})
    tt = _timetable([_lesson("11в", "Английский язык", 1, "Лебедева А.С.", day=0, lesson=0, cabinet="к12/2", week=0),
                     _lesson("11в", "Английский язык", 1, "Лебедева А.С.", day=0, lesson=1, cabinet="к12/2", week=1)],
                    week_count=2)
    p16 = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    p17 = uploader.build_plan(tt, rd, m, 17, datetime.date(2025, 9, 1))
    assert (p16.cycle_week, p17.cycle_week) == (1, 0)
    assert [p.bell_id for p in p16.items] == [7002] and [p.bell_id for p in p17.items] == [7001]
    assert "2-я неделя цикла" in p16.summary()


def test_week1_monday():
    assert uploader.week1_monday_for(None, datetime.date(2025, 9, 1)) == datetime.date(2025, 9, 1)
    # дата начала учебного года из кэша МЭШ важнее файла; без файла и кэша — ошибка
    assert uploader.week1_monday_for(None, None, {"begin_date": "01.09.2026"}) == datetime.date(2026, 8, 31)
    with pytest.raises(ValueError):
        uploader.week1_monday_for(None, None, {})
    sel = uploader.Selection(dates={datetime.date(2026, 9, 2), datetime.date(2026, 9, 9)})
    assert sel.weeks_for(None, datetime.date(2026, 8, 31)) == [1, 2]
    assert sel.weeks_for([2, 3], datetime.date(2026, 8, 31)) == [2]
    assert uploader.Selection().weeks_for([3], datetime.date(2026, 8, 31)) == [3]
    assert uploader.Selection().weeks_for(None, datetime.date(2026, 8, 31)) == []


class _Classes:
    def __init__(self, names):
        self.classes = [type("C", (), {"name": n})() for n in names]


def test_select_classes_days_dates():
    tt = _Classes(["5А", "5Б", "10М", "10Х", "11М", "1А"])
    assert uploader.select_classes("5а, 10м", tt) == ["5А", "10М"]
    assert uploader.select_classes("1", tt) == ["1А"]                  # параллель, а не 10/11
    assert uploader.select_classes("10", tt) == ["10М", "10Х"]
    assert uploader.select_classes("1?м", tt) == ["10М", "11М"]
    assert uploader.select_classes("", tt) is None
    with pytest.raises(ValueError, match="7а"):
        uploader.select_classes("7а", tt)
    assert uploader.parse_days("пн,ср") == {0, 2}
    assert uploader.parse_days("2-4") == {1, 2, 3} and uploader.parse_days("Пт-сб") == {4, 5}
    assert uploader.parse_days(None) is None
    assert uploader.parse_dates("2027-05-24, 25.05.2027") == {datetime.date(2027, 5, 24), datetime.date(2027, 5, 25)}
    assert len(uploader.parse_dates("2027-05-24..2027-05-28")) == 5
    sel = uploader.Selection(dates=uploader.parse_dates("2027-05-24,2027-06-01"))
    assert sel.weeks(datetime.date(2026, 8, 31)) == [39, 40]
    sel = uploader.Selection(days={0, 1}, dates={datetime.date(2027, 5, 25), datetime.date(2027, 5, 26)})
    assert sel.week_dates(datetime.date(2027, 5, 24)) == {datetime.date(2027, 5, 25)}


def test_build_plan_selection_limits_replace():
    rd = _rd()
    m = Mapping(classes={"11в": "11-В", "11Б": "11-Б"},
                subjects={"Английский язык": ["Иностранный (английский) язык"]}, teachers={"Лебедева А.С.": 100},
                rooms={"к12/2": 500})
    tt = _timetable([_lesson("11в", "Английский язык", 1, "Лебедева А.С.", day=0, lesson=0, cabinet="к12/2"),
                     _lesson("11в", "Английский язык", 1, "Лебедева А.С.", day=1, lesson=0, cabinet="к12/2")])
    sel = uploader.Selection(classes=["11в"], days={1})
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), sel)
    assert [p.date for p in plan.items] == [datetime.date(2025, 12, 16)] and plan.dates == {datetime.date(2025, 12, 16)}
    existing = [   # понедельник не входит в выбор — при replace не удаляется, вторник — удаляется
        {"id": 1, "group_id": 10, "date": [2025, 12, 15], "bell_id": 7002, "teacher_id": 100, "room_id": 500, "class_unit_id": 1},
        {"id": 2, "group_id": 10, "date": [2025, 12, 16], "bell_id": 7002, "teacher_id": 100, "room_id": 500, "class_unit_id": 1},
    ]
    uploader.diff_plan(plan, existing, replace=True, rd=rd)
    assert [d["id"] for d in plan.to_delete] == [2] and len(plan.to_create) == 1



def test_replace_respects_only_kind():
    """--only oo --replace не должен удалять занятия ВД тех же классов (и наоборот)."""
    rd = _rd()
    m = Mapping(classes={"11в": "11-В", "11Б": "11-Б"},
                subjects={"Английский язык": ["Иностранный (английский) язык"]}, teachers={"Лебедева А.С.": 100},
                rooms={"к12/2": 500})
    tt = _timetable([_lesson("11в", "Английский язык", 1, "Лебедева А.С.", day=1, lesson=0, cabinet="к12/2")])
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), uploader.Selection(classes=["11в"], only="oo"))
    existing = [
        {"id": 1, "group_id": 11, "date": [2025, 12, 16], "bell_id": 7002, "teacher_id": 101, "room_id": 500, "class_unit_id": 1},
        {"id": 2, "ec_group_id": 600, "date": "2025-12-16", "begin_time": "09:00", "end_time": "09:45",
         "teacher_id": 100, "class_unit_ids": [1]},
    ]
    uploader.diff_plan(plan, existing, replace=True, rd=rd)
    assert [d["id"] for d in plan.to_delete] == [1] and len(plan.existing) == 1


def test_deleted_teachers_are_filtered():
    """Удалённые профили: не в списке действующих, убираются из учителей групп (уроки и ВД), занятие с таким
    учителем пропускается с понятной причиной, а старое сопоставление на него подбирается заново."""
    rd = _rd()
    rd.teacher_profiles.append({"id": 103, "name": "Ушедший Пётр Петрович", "deleted": True, "deleted_at": "2026-09-15"})
    rd.groups[0]["teachers"].append({"id": 103, "full_name": None})
    rd.ec_groups[0]["teacher_ids"].append(103)
    rd._index()
    assert [t["id"] for t in rd.teachers] == [100, 101, 102] and rd.teacher_deleted(103) and not rd.teacher_deleted(100)
    assert not rd.teacher_deleted(999)      # неизвестный профиль (кэш по части параллелей) — не считаем удалённым
    assert [t["id"] for t in rd.group_by_id[10]["teachers"]] == [100]
    assert [t["id"] for t in rd.ec_group_by_id[600]["teachers"]] == [100]
    assert "учителей: 3 (+ удалённых 1)" in rd.summary()
    m = Mapping(classes={"11в": "11-В"}, subjects={"Английский язык": ["Иностранный (английский) язык"]},
                teachers={"Ушедший П.П.": 103})
    plan = uploader.build_plan(_timetable([_lesson("11в", "Английский язык", 1, "Ушедший П.П.")]), rd, m,
                               16, datetime.date(2025, 9, 1))
    assert plan.items == [] and plan.skipped[0][1] == "учитель удалён в МЭШ: Ушедший Пётр Петрович (id 103)"
    # автосопоставление: удалённый профиль сбрасывается и ищется заново среди действующих
    tt = _timetable([_lesson("11в", "Английский язык", 1, "Лебедева Анна Сергеевна")])
    tt.teachers = [chngf.Teacher(index=0, last_name="Лебедева", first_name="Анна", middle_name="Сергеевна", slots=[])]
    m = Mapping(teachers={"Лебедева Анна Сергеевна": 103})
    rep = mapping.MatchReport()
    mapping._match_teachers(tt, list(tt.iter_lessons()), rd, m, rep)
    assert m.teachers["Лебедева Анна Сергеевна"] == 100 and any("удалён в МЭШ" in s for s in rep.lines)


# ---------------------------------------------------------------- приоритеты предметов и часы групп
def _rd_prio(hours=1):
    """К _rd() добавлены группы 11-В по информатике, технологии (учитель Лебедева, по ``hours`` ч/нед)
    и труду (другой учитель)."""
    rd = _rd()

    def loads(h):
        return [{"week_number": w, "hours": h, "real_hours": 0} for w in (16, 17)] if h is not None else []

    rd.groups += [
        {"id": 20, "name": "Информатика 11-В 11в", "subject_name": "Информатика", "class_unit_id": 1, "class_unit_ids": [1],
         "is_metagroup": False, "teachers": [{"id": 100, "full_name": "Лебедева", "teacher_loads": loads(hours)}]},
        {"id": 21, "name": "Технология 11-В 11в", "subject_name": "Технология", "class_unit_id": 1, "class_unit_ids": [1],
         "is_metagroup": False, "teachers": [{"id": 100, "full_name": "Лебедева", "teacher_loads": loads(hours)}]},
        {"id": 22, "name": "Труд 11-В 11в", "subject_name": "Труд", "class_unit_id": 1, "class_unit_ids": [1],
         "is_metagroup": False, "teachers": [{"id": 101, "full_name": "Зайцева", "teacher_loads": loads(hours)}]},
    ]
    rd.schedules.append({"id": 901, "week_number": 17})
    rd._index()
    return rd


def _m_prio(names=("Информатика", "Технология", "Труд", "Химия"), **groups):
    return Mapping(classes={"11в": "11-В"}, subjects={"Информатика": list(names)},
                   teachers={"Лебедева А.С.": 100, "Зайцева Мария Игоревна": 101}, groups=dict(groups))


def _inf(day, lesson=0, teacher="Лебедева А.С."):
    return _lesson("11в", "Информатика", 0, teacher, day=day, lesson=lesson)


def test_group_key_priority():
    l = _inf(0)
    assert mapping.group_key(l) == "11в|Информатика|0" and mapping.group_key(l, 2) == "11в|Информатика|0|2"
    assert mapping.split_group_key("11в|Информатика|0") == ("11в", "Информатика", "0", 1)
    assert mapping.split_group_key("11в|Информатика|1|3") == ("11в", "Информатика", "1", 3)


def test_resolve_chain_by_priority():
    rd = _rd_prio()
    chain = mapping.GroupResolver(_timetable([]), rd, _m_prio()).resolve_chain(_inf(0))
    # «Химия» пропущена — в классе нет групп; остальные — по порядку списка
    assert [(lk.prio, lk.name, lk.group["id"]) for lk in chain] == [(1, "Информатика", 20), (2, "Технология", 21), (3, "Труд", 22)]
    assert rd.group_hours(rd.group_by_id[20], 16, 100) == 1 and rd.group_hours(rd.group_by_id[20], 16, 999) == 1
    assert rd.group_hours(rd.group_by_id[20], 40) is None and rd.group_hours(rd.ec_group_by_id[600], 16) is None
    assert rd.group_hours_span(rd.group_by_id[20]) == "1" and rd.group_hours_span(rd.ec_group_by_id[600]) == ""
    # ручной выбор по приоритету: «|2» — второе звено; первое звено — без суффикса, как раньше
    m = _m_prio(**{"11в|Информатика|0|2": 22, "11в|Информатика|0": 21})
    chain = mapping.GroupResolver(_timetable([]), rd, m).resolve_chain(_inf(0))
    assert [(lk.prio, lk.group["id"], lk.why) for lk in chain[:2]] == [(1, 21, "по явному сопоставлению"), (2, 22, "по явному сопоставлению")]
    # ничего не подобрано — единственное звено с причиной
    chain = mapping.GroupResolver(_timetable([]), rd, _m_prio(names=("Химия",))).resolve_chain(_inf(0))
    assert len(chain) == 1 and chain[0].group is None and "нет групп" in chain[0].why


def test_build_plan_spills_by_hours():
    rd = _rd_prio()
    lessons = [_inf(0), _inf(1), _inf(2)]
    plan = uploader.build_plan(_timetable(lessons), rd, _m_prio(), 16, datetime.date(2025, 9, 1))
    got = [(p.group_id, p.priority, p.over_hours) for p in plan.items]
    # пн — информатика (1 ч), вт — технология (2-й приоритет), ср — труд не подходит по учителю → информатика сверх часов
    assert got == [(20, 1, False), (21, 2, False), (20, 1, True)] and plan.skipped == []
    assert "приоритет 2 «Технология»" in plan.items[1].note and "исчерпаны «Информатика 11-В 11в» (1 ч)" in plan.items[1].note
    assert "сверх часов" in plan.items[2].note and "[сверх часов]" in plan.items[2].describe()
    assert "по 2-му и далее приоритету предмета 1" in plan.summary() and "сверх часов группы 1" in plan.summary()
    # часов нет в справочнике — лимита нет, всё в первый приоритет
    plan = uploader.build_plan(_timetable(lessons), _rd_prio(hours=None), _m_prio(), 16, datetime.date(2025, 9, 1))
    assert [(p.group_id, p.priority) for p in plan.items] == [(20, 1)] * 3
    # часы считаются по всей неделе, даже если план ограничен одной датой
    sel = uploader.Selection(dates={datetime.date(2025, 12, 17)})
    plan = uploader.build_plan(_timetable(lessons), rd, _m_prio(), 16, datetime.date(2025, 9, 1), sel)
    assert [(p.group_id, p.over_hours) for p in plan.items] == [(20, True)] and plan.skipped == []
    # учитель не привязан ни к одной группе цепочки — пропуск с перечнем групп
    plan = uploader.build_plan(_timetable([_inf(0, teacher="Зайцева Мария Игоревна")]), rd,
                               _m_prio(names=("Информатика", "Технология")), 16, datetime.date(2025, 9, 1))
    assert plan.items == [] and "не привязан" in plan.skipped[0][1] and "«Технология 11-В 11в»" in plan.skipped[0][1]


def test_force_teacher_binds_group_with_other_teacher():
    """Принудительная привязка: занятие ставится в группу, где учителя занятия нет, от имени учителя группы."""
    rd = _rd_prio()
    lesson = _inf(0, teacher="Зайцева Мария Игоревна")      # id 101, в группе 20 — только Лебедева (100)
    tt = _timetable([lesson])
    m = _m_prio(names=("Информатика",), **{"11в|Информатика|0": 20})
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert plan.items == [] and "не привязан" in plan.skipped[0][1]
    m.force_teachers["11в|Информатика|0"] = 100
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert [(p.group_id, p.teacher_id, p.over_hours) for p in plan.items] == [(20, 100, False)]
    assert "учитель задан принудительно" in plan.items[0].note
    # учитель не из группы принудительной привязкой не становится её учителем
    m.force_teachers["11в|Информатика|0"] = 999
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert plan.items == [] and "не привязан" in plan.skipped[0][1]


def test_force_teacher_overrides_deleted_profile():
    """Учитель занятия удалён в МЭШ: с принудительной привязкой занятие всё равно ставится."""
    rd = _rd_prio()
    rd.teacher_profiles.append({"id": 104, "name": "Уволенный У.У.", "deleted": True})
    rd._index()
    tt = _timetable([_inf(0)])
    m = _m_prio(names=("Информатика",), **{"11в|Информатика|0": 20})
    m.teachers["Лебедева А.С."] = 104
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert plan.items == [] and "удалён в МЭШ" in plan.skipped[0][1]
    m.force_teachers["11в|Информатика|0"] = 100
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert [(p.group_id, p.teacher_id) for p in plan.items] == [(20, 100)]


def test_match_groups_reports_unattached_teacher():
    rd = _rd_prio()
    lessons = [_inf(0, teacher="Зайцева Мария Игоревна")]
    m = _m_prio(names=("Информатика",), **{"11в|Информатика|0": 20})
    rep = mapping.MatchReport()
    mapping._match_groups(_timetable(lessons), lessons, rd, m, rep)
    assert any("не привязан к группе МЭШ" in x and "force_teachers" in x for x in rep.lines)
    m.force_teachers["11в|Информатика|0"] = 100
    rep = mapping.MatchReport()
    mapping._match_groups(_timetable(lessons), lessons, rd, m, rep)
    assert not any("не привязан к группе МЭШ" in x for x in rep.lines)
    assert any("учитель задан принудительно" in x for x in rep.lines)


def test_build_plan_counts_existing_lessons():
    rd = _rd_prio()
    lessons = [_inf(0), _inf(1), _inf(2)]
    tt = _timetable(lessons)
    m = _m_prio(names=("Информатика", "Технология"))
    thu = {"id": 5, "group_id": 20, "date": [2025, 12, 18], "bell_id": 7003, "teacher_id": 100, "room_id": 500, "class_unit_id": 1}
    # без replace чужой урок информатики в четверг остаётся и съедает единственный час
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), existing=[thu], replace=False)
    assert [(p.group_id, p.over_hours) for p in plan.items] == [(21, False), (20, True), (20, True)]
    # с replace он будет удалён — часы свободны
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), existing=[thu], replace=True)
    assert [(p.group_id, p.over_hours) for p in plan.items] == [(20, False), (21, False), (20, True)]
    # а если план только на среду, четверг вне дат плана и остаётся даже с replace
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), uploader.Selection(dates={datetime.date(2025, 12, 17)}),
                               existing=[thu], replace=True)
    assert [(p.group_id, p.over_hours) for p in plan.items] == [(20, True)]
    # занятия сервера в тех же слотах, что и план, не считаются дважды: план их подтверждает как есть
    same = [{"id": 6, "group_id": 20, "date": [2025, 12, 15], "bell_id": 7001, "teacher_id": 100, "room_id": 500, "class_unit_id": 1},
            {"id": 7, "group_id": 21, "date": [2025, 12, 16], "bell_id": 7001, "teacher_id": 100, "room_id": 500, "class_unit_id": 1}]
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), existing=same, replace=False)
    assert [(p.group_id, p.priority, p.over_hours) for p in plan.items] == [(20, 1, False), (21, 2, False), (20, 1, True)]
    uploader.diff_plan(plan, same, replace=False, rd=rd)
    assert plan.unchanged == 2 and len(plan.to_create) == 1


def test_build_plan_loaded_day_counted_by_server():
    """День вне плана уже загружен на сервер по старой версии (урок в другом слоте): его часы берутся с
    сервера, урок файла в этот день второй раз не считается."""
    rd = _rd_prio(hours=2)
    tt = _timetable([_inf(0, lesson=2), _inf(5)])           # в файле: пн 3-й урок, сб 1-й
    m = _m_prio(names=("Информатика", "Технология"))
    old_mon = {"id": 5, "group_id": 20, "date": [2025, 12, 15], "bell_id": 7005, "teacher_id": 100, "room_id": 500,
               "class_unit_id": 1}                        # на сервере: пн 5-й урок
    sat = uploader.Selection(dates={datetime.date(2025, 12, 20)})
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), sat, existing=[old_mon], replace=True)
    assert [(p.group_id, p.priority, p.over_hours) for p in plan.items] == [(20, 1, False)]
    # на сервере в понедельник у класса ничего нет — день не загружен, часы по файлу (пн + сб = 2)
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), sat, existing=[], replace=True)
    assert [(p.group_id, p.over_hours) for p in plan.items] == [(20, False)]
    # в загруженном понедельнике два урока информатики — часы исчерпаны, суббота во 2-й приоритет
    two = [old_mon, dict(old_mon, id=6, bell_id=7006)]
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1), sat, existing=two, replace=True)
    assert [(p.group_id, p.priority) for p in plan.items] == [(21, 2)]


def test_group_without_teachers_is_not_attached():
    """В группу без учителей сервер занятия не принимает — звено пропускается, даже с force_teachers."""
    rd = _rd_prio()
    rd.group_by_id[20]["teachers"] = []
    tt = _timetable([_inf(0)])
    plan = uploader.build_plan(tt, rd, _m_prio(names=("Информатика", "Технология")), 16, datetime.date(2025, 9, 1))
    assert [(p.group_id, p.priority) for p in plan.items] == [(21, 2)]
    m = _m_prio(names=("Информатика",))
    m.force_teachers["11в|Информатика|0"] = 100
    plan = uploader.build_plan(tt, rd, m, 16, datetime.date(2025, 9, 1))
    assert plan.items == [] and "«Информатика 11-В 11в» — нет учителей" in plan.skipped[0][1]


def test_match_groups_reports_hours():
    rd = _rd_prio()
    lessons = [_inf(0), _inf(1)]
    m = _m_prio(names=("Информатика", "Технология"))
    rep = mapping.MatchReport()
    mapping._match_groups(_timetable(lessons), lessons, rd, m, rep)
    assert any("уроков в неделю 2, часов у «Информатика 11-В 11в» 1 — остальные пойдут в «Технология 11-В 11в»" in x for x in rep.lines)
    m = _m_prio(names=("Информатика",))
    rep = mapping.MatchReport()
    mapping._match_groups(_timetable(lessons), lessons, rd, m, rep)
    assert any("сверх часов" in x for x in rep.lines)
    # неразрешённое звено второго приоритета — проблема с ключом «|2»
    m = _m_prio(names=("Информатика", "Труд"))
    m.groups["11в|Информатика|0|2"] = 999999
    rep = mapping.MatchReport()
    mapping._match_groups(_timetable(lessons), lessons, rd, m, rep)
    assert any("приоритет 2 «Труд»" in x and "не найдена в кэше" in x for x in rep.lines)


def test_build_plan_flow_keeps_one_group_when_hours_exhausted():
    """Поток двух классов на метагруппу с 1 ч/нед: второй класс видит слот уже занятым потоком и не уходит
    во 2-й приоритет — одно занятие, а не два в разных группах."""
    rd = _rd_prio()
    for g in rd.groups:
        if g["id"] in (20, 21):
            g["is_metagroup"], g["class_unit_ids"] = True, [1, 2]
    rd._index()
    m = _m_prio(names=("Информатика", "Технология"))
    m.classes["11Б"] = "11-Б"
    lessons = [_lesson("11в", "Информатика", 0, "Лебедева А.С.", potok=("11Б", "11в"), day=0, lesson=0),
               _lesson("11Б", "Информатика", 0, "Лебедева А.С.", potok=("11Б", "11в"), day=0, lesson=0)]
    plan = uploader.build_plan(_timetable(lessons), rd, m, 16, datetime.date(2025, 9, 1))
    assert [(p.group_id, p.priority, p.over_hours) for p in plan.items] == [(20, 1, False)] and plan.skipped == []
