"""Выгрузка плана и итогов загрузки в CSV и текст."""

import csv

from test_mapping import _rd
from test_uploader import FakeClient, _plan

from timetable_upload import export, uploader
from timetable_upload.mesh_api import MeshError
from timetable_upload.uploader import Journal


def _compared_plan(rd):
    plan = _plan(rd)
    stale = {"id": 77, "group_id": 10, "date": [2025, 12, 16], "bell_id": 7001, "teacher_id": 100, "room_id": 500,
             "class_unit_id": 1, "class_unit_name": "11-В", "lesson_number": 3, "group_name": "Англ 11-В", "teacher_name": "Иванов"}
    uploader.diff_plan(plan, [stale], replace=True, rd=rd)
    return plan


def test_plan_rows_offline_and_online():
    rd = _rd()
    plan = _plan(rd)
    rows = export.plan_rows([plan])
    assert {r[2] for r in rows} == {"к переносу"} and len(rows) == len(plan.items) + len(plan.skipped)
    row = dict(zip(export.PLAN_COLUMNS, rows[0]))
    assert (row["Неделя"], row["Класс"], row["Предмет"], row["Группа"], row["Вид"]) == (16, "11в", "Английский язык", 1, "урок")
    assert row["Дата"] == "2025-12-15" and row["id учителя"] == 100 and row["id кабинета"] == 500 and row["Звонок / время"]
    ec = [dict(zip(export.PLAN_COLUMNS, r)) for r in rows if r[10] == "ВД"][0]
    assert "–" in ec["Звонок / время"] and ec["Урок"] == 2

    plan = _compared_plan(rd)
    rows = export.plan_rows([plan])
    actions = [r[2] for r in rows]
    assert actions.count("создать") == 2 and actions.count("удалить") == 1 and "к переносу" not in actions
    gone = dict(zip(export.PLAN_COLUMNS, [r for r in rows if r[2] == "удалить"][0]))
    assert (gone["Дата"], gone["Класс"], gone["Урок"], gone["Группа МЭШ"], gone["id группы"]) == ("2025-12-16", "11-В", 3, "Англ 11-В", 10)
    assert "Иванов" in gone["Примечание"] and "id 77" in gone["Примечание"]
    assert all(len(r) == len(export.PLAN_COLUMNS) for r in rows)


def test_save_plans_csv_and_text(tmp_path):
    rd = _rd()
    plan = _compared_plan(rd)
    p = tmp_path / "plan.csv"
    export.save_plans(str(p), [plan])
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                      # BOM — чтобы Excel понял UTF-8
    with open(p, encoding="utf-8-sig", newline="") as f:
        data = list(csv.reader(f, delimiter=";"))
    assert data[0] == export.PLAN_COLUMNS and len(data) == 1 + len(export.plan_rows([plan]))
    t = tmp_path / "plan.txt"
    export.save_plans(str(t), [plan], header="Недели 16")
    text = t.read_text(encoding="utf-8")
    assert text.startswith("Недели 16\n\nнеделя 16") and "  - удалить:" in text and "  + создать:" in text
    assert text == "Недели 16\n\n" + export.plan_text(plan) + "\n"


def test_save_results(tmp_path):
    rd = _rd()
    plan = _compared_plan(rd)
    client = FakeClient(create_responses=[MeshError(500, "нет"), MeshError(422, "плохо")])
    res = uploader.apply_plan(client, plan, Journal(tmp_path / "j.jsonl"))
    results = [(plan, res)]
    assert len(res.errors) == 2
    p = tmp_path / "errors.csv"
    export.save_results(str(p), results)
    with open(p, encoding="utf-8-sig", newline="") as f:
        data = list(csv.reader(f, delimiter=";"))
    assert data[0] == export.ERROR_COLUMNS and len(data) == 3 and data[1][0] == "16" and data[1][1] == res.errors[0][0]
    t = tmp_path / "errors.txt"
    export.save_results(str(t), results)
    text = t.read_text(encoding="utf-8")
    assert text.startswith("неделя 16: создано 0, удалено 1, ошибок 2") and text.count("ОШИБКА") == 2
