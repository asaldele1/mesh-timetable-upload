"""Выполнение плана, журнал и откат — на заглушке клиента МЭШ."""

import datetime

import pytest
from test_mapping import _lesson, _rd, _timetable

from timetable_upload import uploader
from timetable_upload.mapping import Mapping
from timetable_upload.mesh_api import MeshError
from timetable_upload.uploader import Journal, journal_key


class FakeClient:
    """Считает вызовы; ответы на создание задаются списком (dict — обычный ответ, str — тело не JSON,
    MeshError — ошибка сервера)."""

    def __init__(self, create_responses=(), ec_schedule=None, existing=(), ec_schedules=None):
        self.calls: list[tuple] = []
        self.create_responses = list(create_responses)
        self.ec_schedule = ec_schedule
        self.existing = list(existing)
        # что сервер отдаёт списком расписаний ВД (по умолчанию — как в _rd(): неделя 16 → 950)
        self.ec_schedules_list = [{"id": 950, "week_number": 16, "academic_year_id": 14}] if ec_schedules is None else ec_schedules
        self.ay = 14

    def ec_schedules(self):
        return self.ec_schedules_list

    def _next(self, what, *args):
        self.calls.append((what,) + args)
        resp = self.create_responses.pop(0) if self.create_responses else {"id": 1000 + len(self.calls)}
        if isinstance(resp, MeshError):
            raise resp
        return resp

    def create_item(self, schedule_id, group_id, bell_id, date, teacher_id, room_id, building_id):
        return self._next("create_item", schedule_id, group_id, bell_id)

    def create_ec_item(self, ec_schedule_id, group_id, date, begin, end, duration, teacher_id, room_id, building_id):
        return self._next("create_ec_item", ec_schedule_id, group_id)

    def create_ec_schedule(self, week):
        self.calls.append(("create_ec_schedule", week))
        if isinstance(self.ec_schedule, MeshError):
            raise self.ec_schedule
        return self.ec_schedule

    def delete_item(self, schedule_id, item_id):
        self.calls.append(("delete_item", schedule_id, item_id))

    def delete_ec_item(self, item_id):
        self.calls.append(("delete_ec_item", item_id))

    def schedule_items(self, week, class_unit_ids=None):
        return [it for it in self.existing if "ec_group_id" not in it]

    def ec_schedule_items(self, ec_schedule_id, class_unit_ids=None):
        return [it for it in self.existing if "ec_group_id" in it]


def _mapping():
    return Mapping(classes={"11в": "11-В", "11Б": "11-Б"},
                   subjects={"Английский язык": ["Иностранный (английский) язык"], "Разговоры о важном": ["Разговоры о важном"]},
                   teachers={"Лебедева А.С.": 100}, rooms={"к12/2": 500})


def _plan(rd, with_ec=True):
    for day in rd.bells_timetables[0]["bells_day_timetables"]:
        for b in day["bells"]:
            b["begin_time"], b["end_time"] = f"{8 + b['ordinal']:02d}:00", f"{8 + b['ordinal']:02d}:45"
    lessons = [_lesson("11в", "Английский язык", 1, "Лебедева А.С.", day=0, lesson=0, cabinet="к12/2")]
    if with_ec:
        lessons.append(_lesson("11в", "Разговоры о важном", 0, "Лебедева А.С.", day=0, lesson=1))
    return uploader.build_plan(_timetable(lessons), rd, _mapping(), 16, datetime.date(2025, 9, 1))


def test_apply_creates_ec_schedule_before_deleting(tmp_path):
    rd = _rd()
    rd.ec_schedules = []          # расписания ВД на неделю ещё нет
    rd._index()
    plan = _plan(rd)
    assert plan.ec_schedule_id is None and len(plan.items) == 2
    stale = {"id": 77, "group_id": 10, "date": [2025, 12, 16], "bell_id": 7001, "teacher_id": 100, "room_id": 500, "class_unit_id": 1}
    uploader.diff_plan(plan, [stale], replace=True, rd=rd)
    assert [d["id"] for d in plan.to_delete] == [77] and len(plan.to_create) == 2
    client = FakeClient(ec_schedule={"id": 951}, ec_schedules=[])
    journal = Journal(tmp_path / "j.jsonl")
    res = uploader.apply_plan(client, plan, journal)
    assert (res.created, res.deleted, res.errors) == (2, 1, [])
    assert [c[0] for c in client.calls] == ["create_ec_schedule", "delete_item", "create_item", "create_ec_item"]
    assert plan.ec_schedule_id == 951 and client.calls[3][1] == 951
    recs = journal.read()
    assert [(r["kind"], r["schedule_id"]) for r in recs] == [("oo", 900), ("ec", 951)]


def test_apply_ec_schedule_failure_does_not_delete_blindly(tmp_path):
    """Не удалось создать расписание ВД — занятия ВД в ошибки, остальное выполняется; со stop_on_error
    ничего не удаляется."""
    rd = _rd()
    rd.ec_schedules = []
    rd._index()
    plan = _plan(rd)
    uploader.diff_plan(plan, [{"id": 77, "group_id": 10, "date": [2025, 12, 16], "bell_id": 7001, "teacher_id": 100,
                               "room_id": 500, "class_unit_id": 1}], replace=True, rd=rd)
    client = FakeClient(ec_schedule=MeshError(500, "нет"), ec_schedules=[])
    res = uploader.apply_plan(client, plan, Journal(tmp_path / "j.jsonl"), stop_on_error=True)
    assert res.deleted == 0 and res.created == 0 and [c[0] for c in client.calls] == ["create_ec_schedule"]
    assert "расписания ВД" in res.errors[0][0]
    client = FakeClient(ec_schedule=MeshError(500, "нет"), ec_schedules=[])
    res = uploader.apply_plan(client, plan, Journal(tmp_path / "j.jsonl"))
    assert res.deleted == 1 and res.created == 1 and len(res.errors) == 2
    assert "не создано" in res.errors[1][1] and [c[0] for c in client.calls] == ["create_ec_schedule", "delete_item", "create_item"]


def test_apply_recovers_item_from_non_json_response(tmp_path):
    """Ответ без id (текст) — занятие перепроверяется по серверу и попадает в журнал с настоящим id."""
    rd = _rd()
    plan = _plan(rd, with_ec=False)
    uploader.diff_plan(plan, [], replace=False, rd=rd)
    p = plan.to_create[0]
    on_server = {"id": 4242, "group_id": p.group_id, "date": p.date.isoformat(), "bell_id": p.bell_id,
                 "teacher_id": 100, "room_id": 500, "class_unit_id": 1}
    client = FakeClient(create_responses=["OK"], existing=[on_server])
    journal = Journal(tmp_path / "j.jsonl")
    res = uploader.apply_plan(client, plan, journal)
    assert (res.created, res.errors) == (1, []) and [r["item_id"] for r in journal.read()] == [4242]
    # а если на сервере занятия нет — это ошибка с текстом ответа
    client = FakeClient(create_responses=["OK"])
    res = uploader.apply_plan(client, plan, Journal(tmp_path / "j2.jsonl"))
    assert res.created == 0 and "не вернул id" in res.errors[0][1]


def test_journal_keys_distinguish_kinds(tmp_path):
    """Урок и занятие ВД с одинаковым id — разные записи: удаляется только нужная."""
    rd = _rd()
    plan = _plan(rd)
    journal = Journal(tmp_path / "j.jsonl")
    oo, ec = plan.items
    journal.record(plan, oo, 5)
    journal.record(plan, ec, 5)
    assert [journal_key(r) for r in journal.read()] == [("oo", 5), ("ec", 5)]
    journal.remove({("oo", 5)})
    assert [journal_key(r) for r in journal.read()] == [("ec", 5)]
    journal.record(plan, oo, 5)
    client = FakeClient()
    res = uploader.undo(client, journal, items={("ec", 5)})
    assert res.deleted == 1 and client.calls == [("delete_ec_item", 5)]
    assert [journal_key(r) for r in journal.read()] == [("oo", 5)]


def test_check_academic_year():
    rd = _rd()
    rd.academic_year = {"id": 13, "name": "2025/26"}

    class C:
        ay = 14
        session_info = type("S", (), {"academic_year": {"name": "2026/27"}})()

    with pytest.raises(ValueError, match="2025/26"):
        rd.check_academic_year(C())
    C.ay = 13
    rd.check_academic_year(C())
    rd.academic_year = {}
    rd.check_academic_year(C())       # старый кэш без года — не проверяем


def test_stop_keeps_done_work_and_journal(tmp_path):
    """Остановка: сделанное до неё остаётся в журнале, результат помечен stopped; удаления — тоже."""
    rd = _rd()
    plan = _plan(rd)
    uploader.diff_plan(plan, [], replace=False, rd=rd)
    client = FakeClient()
    journal = Journal(tmp_path / "j.jsonl")
    calls = iter([False, True])
    res = uploader.apply_plan(client, plan, journal, should_stop=lambda: next(calls))
    assert res.stopped and res.created == 1 and [c[0] for c in client.calls] == ["create_item"]
    assert [r["kind"] for r in journal.read()] == ["oo"]
    # стоп во время удалений: создания не начинаются
    uploader.diff_plan(plan, [{"id": 77, "group_id": 10, "date": [2025, 12, 16], "bell_id": 7001, "teacher_id": 100,
                               "room_id": 500, "class_unit_id": 1}], replace=True, rd=rd)
    client = FakeClient()
    res = uploader.apply_plan(client, plan, Journal(tmp_path / "j2.jsonl"), should_stop=lambda: True)
    assert res.stopped and (res.created, res.deleted) == (0, 0) and client.calls == []


def test_undo_stop_keeps_remaining_records(tmp_path):
    rd = _rd()
    plan = _plan(rd)
    journal = Journal(tmp_path / "j.jsonl")
    for i, item in enumerate(plan.items):
        journal.record(plan, item, 10 + i)
    calls = iter([False, True])
    client = FakeClient()
    res = uploader.undo(client, journal, should_stop=lambda: next(calls))
    assert res.stopped and res.deleted == 1 and client.calls == [("delete_item", 900, 10)]
    assert [journal_key(r) for r in journal.read()] == [("ec", 11)]


def _many_lessons(n):
    return [_lesson("11в", "Английский язык", 1, "Лебедева А.С.", day=i % 5, lesson=i // 5, cabinet="к12/2") for i in range(n)]


def test_parallel_creates_all_and_journals(tmp_path):
    """workers=4: запросы идут одновременно, всё создано и записано в журнал, ошибки собраны."""
    import threading
    import time

    rd = _rd()
    for day in rd.bells_timetables[0]["bells_day_timetables"]:
        for b in day["bells"]:
            b["begin_time"], b["end_time"] = f"{8 + b['ordinal']:02d}:00", f"{8 + b['ordinal']:02d}:45"
    plan = uploader.build_plan(_timetable(_many_lessons(20)), rd, _mapping(), 16, datetime.date(2025, 9, 1))
    assert len(plan.items) == 20, plan.skipped
    uploader.diff_plan(plan, [], replace=False, rd=rd)

    class SlowClient(FakeClient):
        active = peak = 0
        lock = threading.Lock()

        def create_item(self, *a):
            with self.lock:
                self.active += 1
                SlowClient.peak = max(SlowClient.peak, self.active)
            time.sleep(0.02)
            try:
                return super().create_item(*a)
            finally:
                with self.lock:
                    self.active -= 1

    client = SlowClient(create_responses=[{"id": 1}, MeshError(400, "занято"), {"id": 3}])
    journal = Journal(tmp_path / "j.jsonl")
    seen = []
    res = uploader.apply_plan(client, plan, journal, workers=4, progress=lambda d, t: seen.append((d, t)))
    assert (res.created, len(res.errors), res.stopped) == (19, 1, False)
    assert res.errors[0][1] == "занято" and SlowClient.peak > 1
    assert len(journal.read()) == 19 and seen[-1] == (20, 20) and len(seen) == 20


def test_parallel_stop_finishes_started_requests(tmp_path):
    """Остановка при workers=3: новые запросы не начинаются, начатые доходят до конца и попадают в журнал."""
    rd = _rd()
    for day in rd.bells_timetables[0]["bells_day_timetables"]:
        for b in day["bells"]:
            b["begin_time"], b["end_time"] = f"{8 + b['ordinal']:02d}:00", f"{8 + b['ordinal']:02d}:45"
    plan = uploader.build_plan(_timetable(_many_lessons(20)), rd, _mapping(), 16, datetime.date(2025, 9, 1))
    uploader.diff_plan(plan, [], replace=False, rd=rd)
    client = FakeClient()
    journal = Journal(tmp_path / "j.jsonl")
    n = 0

    def should_stop():
        nonlocal n
        n += 1
        return n > 3            # первый опрос — до старта, затем после каждого завершения

    res = uploader.apply_plan(client, plan, journal, workers=3, should_stop=should_stop)
    assert res.stopped and 3 <= res.created <= 5     # 2 завершённых до «стоп» + до 3 начатых
    assert len(journal.read()) == res.created == len(client.calls)
    # stop_on_error: после первой ошибки новые не начинаются, результат не «остановлен»
    client = FakeClient(create_responses=[MeshError(500, "x")])
    res = uploader.apply_plan(client, plan, Journal(tmp_path / "j2.jsonl"), workers=2, stop_on_error=True)
    assert not res.stopped and len(res.errors) == 1 and res.created + 1 == len(client.calls) <= 3


def test_undo_parallel(tmp_path):
    rd = _rd()
    plan = _plan(rd)
    journal = Journal(tmp_path / "j.jsonl")
    for i in range(6):
        journal.record(plan, plan.items[0], 100 + i)
    client = FakeClient()
    res = uploader.undo(client, journal, workers=4)
    assert (res.deleted, res.errors, res.stopped) == (6, [], False) and journal.read() == []
    assert sorted(c[2] for c in client.calls) == list(range(100, 106))


def test_ec_schedule_id_taken_from_server_not_cache(tmp_path):
    """Сайт пересоздаёт расписания недель ВД: в кэше id 950, на сервере уже 977. Занятие ВД должно
    уйти в 977 (иначе оно «повисает» в старом расписании), а выборка занятий — по 977."""
    rd = _rd()
    plan = _plan(rd)
    assert plan.ec_schedule_id == 950
    client = FakeClient(ec_schedules=[{"id": 977, "week_number": 16, "academic_year_id": 14},
                                      {"id": 960, "week_number": 17, "academic_year_id": 14}])
    uploader.fetch_existing(client, plan)
    assert plan.ec_schedule_id == 977
    plan.ec_schedule_id = 950                       # как если бы план строился без сервера
    uploader.diff_plan(plan, [], replace=False, rd=rd)
    res = uploader.apply_plan(client, plan, uploader.Journal(tmp_path / "j.jsonl"))
    assert res.errors == [] and plan.ec_schedule_id == 977
    assert [c for c in client.calls if c[0] == "create_ec_item"] == [("create_ec_item", 977, 600)]
    assert [c[0] for c in client.calls if c[0] == "create_ec_schedule"] == []
    # расписания на сервере нет вовсе — создаём, как раньше
    client = FakeClient(ec_schedule={"id": 978}, ec_schedules=[])
    plan.ec_schedule_id = 950
    uploader.diff_plan(plan, [], replace=False, rd=rd)
    res = uploader.apply_plan(client, plan)
    assert res.errors == [] and plan.ec_schedule_id == 978 and ("create_ec_schedule", 16) in client.calls


BUSY = "На данную дату и время у преподавателя, учеников группы ВД или в кабинете уже существуют уроки в расписании"


class StudentsClient(FakeClient):
    """Заглушка с учениками групп ВД: снятие и зачисление пишутся в calls."""

    def __init__(self, students, add_error=None, **kw):
        super().__init__(**kw)
        self.students = list(students)
        self.add_error = add_error

    def ec_group_students(self, group_ids):
        return [r for r in self.students if r["ec_group_id"] in group_ids]

    def delete_ec_group_student(self, record_id):
        self.calls.append(("delete_student", record_id))
        self.students = [r for r in self.students if r["id"] != record_id]

    def add_ec_group_student(self, group_id, student_id, begin, end):
        self.calls.append(("add_student", group_id, student_id, begin, end))
        if self.add_error:
            raise self.add_error


def _ec_only_plan(rd):
    plan = _plan(rd)
    uploader.diff_plan(plan, [], replace=False, rd=rd)
    plan.to_create = [p for p in plan.to_create if p.kind == "ec"]
    return plan, plan.to_create[0].group_id


def _students(gid):
    return [{"id": 1, "student_id": 501, "ec_group_id": gid, "begin_date": "2026-09-01", "end_date": "2027-08-31",
             "deleted_at": None},
            {"id": 2, "student_id": 502, "ec_group_id": gid, "begin_date": "2026-09-01", "end_date": "2027-08-31",
             "deleted_at": None}]


def test_ec_busy_retries_without_students(tmp_path):
    """«Уже существуют уроки» — ученики группы снимаются, занятие ставится повторно, ученики возвращаются."""
    plan, gid = _ec_only_plan(_rd())
    client = StudentsClient(_students(gid), create_responses=[MeshError(400, BUSY), {"id": 7}])
    journal = Journal(tmp_path / "j.jsonl")
    res = uploader.apply_plan(client, plan, journal)
    assert (res.created, res.errors) == (1, [])
    assert [c[0] for c in client.calls] == ["create_ec_item", "delete_student", "delete_student", "create_ec_item",
                                            "add_student", "add_student"]
    assert client.calls[-1] == ("add_student", gid, 502, "2026-09-01", "2027-08-31")
    assert [r["item_id"] for r in journal.read()] == [7]


def test_ec_busy_restores_students_after_failed_retry(tmp_path):
    plan, gid = _ec_only_plan(_rd())
    client = StudentsClient(_students(gid), create_responses=[MeshError(400, BUSY), MeshError(400, BUSY)])
    res = uploader.apply_plan(client, plan, Journal(tmp_path / "j.jsonl"))
    assert res.created == 0 and "без учеников группы" in res.errors[0][1]
    assert [c[0] for c in client.calls][-2:] == ["add_student", "add_student"]
    # учеников нет — повторять нечего
    client = StudentsClient([], create_responses=[MeshError(400, BUSY)])
    res = uploader.apply_plan(client, plan, Journal(tmp_path / "j2.jsonl"))
    assert "учеников в группе нет" in res.errors[0][1] and [c[0] for c in client.calls] == ["create_ec_item"]
    # ученика вернуть не удалось — отдельная ошибка
    client = StudentsClient(_students(gid), add_error=MeshError(500, "сбой"),
                            create_responses=[MeshError(400, BUSY), {"id": 7}])
    res = uploader.apply_plan(client, plan, Journal(tmp_path / "j3.jsonl"))
    assert res.created == 1 and [e[0].startswith("возврат ученика") for e in res.errors] == [True, True]
