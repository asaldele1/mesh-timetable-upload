import datetime
import os

import pytest

from timetable_upload import chngf, paths

HERE = os.path.dirname(__file__)
# Реальный файл школы (лежит в папке данных, не в репозитории): тесты на нём пропускаются, если его нет.
SAMPLE = os.path.join(paths.data_dir(), "school.chngf")
# Демонстрационный файл из дистрибутива ХроноГрафа.
DEMO = os.path.join(HERE, "data", "demo.chngf")


@pytest.fixture(scope="module")
def tt():
    if not os.path.exists(SAMPLE):
        pytest.skip("нет файла школы school.chngf в папке данных")
    return chngf.load(SAMPLE)


@pytest.fixture(scope="module")
def demo():
    if not os.path.exists(DEMO):
        pytest.skip("нет файла-образца demo.chngf")
    return chngf.load(DEMO)


def test_header(tt):
    assert (tt.day_count, tt.lesson_count, tt.week_count) == (6, 9, 1)
    assert tt.teach_year == "2025/26"
    assert tt.period_begin == datetime.date(2025, 9, 1)
    assert tt.school_name == "Школа № 1"
    assert tt.lesson_begin[0].strftime("%H:%M") == "09:00" and tt.lesson_end[0].strftime("%H:%M") == "09:45"


def test_counts(tt):
    assert len(tt.classes) == 70
    assert len(tt.teachers) == 204
    assert len(tt.subjects) == 83
    assert len(tt.cabinets) == 101
    assert len(tt.clsgrps) == 1723
    assert sum(1 for _ in tt.iter_lessons()) == 3245


def test_references_consistent(tt):
    # каждая классогруппа принадлежит ровно одному классу и предмету, число уроков = нагрузке (кроме 4 незакрытых)
    assert all(g.cls >= 0 and g.subject >= 0 for g in tt.clsgrps)
    mism = sum(1 for g in tt.clsgrps if sum(g.lessons) != g.nagr)
    assert mism == 4
    # учитель на слоте занятия классогруппы: если у него отмечен кабинет, он совпадает по слоту
    for g in tt.clsgrps:
        if g.teacher < 0:
            continue
        t = tt.teachers[g.teacher]
        for slot in range(len(g.lessons)):
            if t.slots[slot].has_lesson:
                assert t.slots[slot].cabinet >= 0


def test_known_lessons(tt):
    """Проверено по отчёту «Расписание классов» самой программы."""
    by_cls = tt.lessons_by_class()
    l = [x for x in by_cls["10А"] if x.day == 0 and x.lesson == 0]
    assert [(x.subject, x.teacher, x.cabinet) for x in l] == [("Разговоры о важном", "Громова С.В.", "38")]
    l = sorted((x.subject, x.group, x.teacher, x.cabinet) for x in by_cls["10А"] if x.day == 2 and x.lesson == 1)
    assert l == [("Английский язык", 1, "Зуева Е.М.", "п4"), ("Английский язык", 2, "Тихонова Ольга Владимировна", "21")]
    # 5А, понедельник: 7 уроков, 2-й — математический практикум по трём группам
    mon = [x for x in by_cls["5А"] if x.day == 0]
    assert max(x.lesson for x in mon) == 6
    assert sorted(x.group for x in mon if x.lesson == 1) == [1, 2, 3]


def test_potok(tt):
    p = tt.potok_classes()
    assert ("11г", "11д") in set(p.values())
    l = [x for x in tt.iter_lessons() if x.cls == "11д" and x.subject == "Физическая культура"]
    assert l and all(x.potok == ("11г", "11д") for x in l)


def test_class_busy_grid(tt):
    c = next(c for c in tt.classes if c.name == "2А")
    # пятидневка, уроки 7-9 закрыты, суббота закрыта целиком
    for day in range(5):
        assert [c.busy[tt.slot(day, l)] for l in range(9)] == [0] * 6 + [2] * 3
    assert all(c.busy[tt.slot(5, l)] == 2 for l in range(9))


def test_demo(demo):
    assert (demo.day_count, demo.lesson_count, demo.week_count) == (6, 12, 1)
    assert len(demo.classes) == 26 and len(demo.langs) == 2
    assert [l.name for l in demo.langs] == ["Английский", "Французский"]
    assert sum(1 for _ in demo.iter_lessons()) == 1017
    # классы 10-11 в «колледж»-формате со списком учеников
    c10 = next(c for c in demo.classes if c.name == "10а")
    assert len(c10.students) == 27 and c10.students[0].last_name == "Авилова"
    assert (c10.man, c10.woman) == (7, 20)
    # язык подставляется вместо предмета «Иностранный Язык»
    l = next(x for x in demo.iter_lessons() if x.lang_used)
    assert l.base_subject.startswith("Иностранный") and l.subject in ("Английский", "Французский")


def test_teacher_names(tt):
    t = next(t for t in tt.teachers if t.last_name == "Белова М.А.")
    assert t.name == "Белова М.А." and t.short_name == "Белова М.А."
    t = next(t for t in tt.teachers if t.last_name == "Морозов")
    assert t.name == "Морозов Николай Борисович" and t.short_name == "Морозов Н.Б."


def test_bad_file(tmp_path):
    p = tmp_path / "x.chngf"
    p.write_bytes(b"\x00" * 100)
    with pytest.raises(chngf.ChngfError):
        chngf.load(p)


def test_truncated_file(tmp_path):
    """Обрезанный файл — ChngfError с путём, а не struct.error из глубины разбора."""
    data = open(DEMO, "rb").read()
    for n in (3, 200, len(data) // 2):
        p = tmp_path / f"t{n}.chngf"
        p.write_bytes(data[:n])
        with pytest.raises(chngf.ChngfError, match="повреждён"):
            chngf.load(p)
