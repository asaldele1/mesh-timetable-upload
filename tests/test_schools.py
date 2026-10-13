"""Несколько школ у пользователя: выбор профиля при входе, папки школ, перенос данных прежней версии."""

import json
import os

import pytest

from timetable_upload import cli, paths, schools
from timetable_upload.mapping import Mapping
from timetable_upload.mesh_api import MeshClient, ProfileChoiceRequired, school_label, teacher_profiles_of

P1234 = {"id": 11, "type": "teacher", "school_id": 1234, "school_shortname": "Школа № 1", "roles": ["teacher"]}
P900 = {"id": 22, "type": "teacher", "school_id": 900, "school_shortname": "", "school_name": "", "roles": ["deputy"]}
PARENT = {"id": 33, "type": "parent"}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    return tmp_path


def client_with(profiles):
    c = MeshClient("t")
    c.sessions = lambda: {"profiles": profiles}
    c.academic_years = lambda only_current=False: [{"id": 14, "name": "2026/27"}]
    return c


def test_teacher_profiles_and_label():
    assert teacher_profiles_of({"profiles": [P1234, PARENT, P900]}) == [P1234, P900]
    no_school = {"id": 5, "type": "teacher"}
    assert teacher_profiles_of({"profiles": [no_school]}) == [no_school]     # как раньше, если school_id нет ни у кого
    assert school_label(P1234) == "Школа № 1" and school_label(P900) == "Школа id 900"


def test_login_chooses_school_by_profile():
    c = client_with([P1234, P900])
    with pytest.raises(ProfileChoiceRequired) as e:
        c.login()
    assert e.value.profiles == [P1234, P900] and "Школа № 1" in str(e.value)
    s = c.login(profile_id=22)
    assert s.school_id == 900 and c.http.headers["Profile-Id"] == "22" and c.profiles == [P1234, P900]
    with pytest.raises(ProfileChoiceRequired, match="недоступен"):
        c.login(profile_id=99)
    s = client_with([P1234, PARENT]).login()          # школа одна — без выбора
    assert s.profile_id == 11 and s.school_label == "Школа № 1"


def test_current_school_roundtrip(data_dir):
    assert schools.load_current().profile_id is None
    cur = schools.remember(P900, [P1234, P900])
    assert (cur.profile_id, cur.school_id, cur.label) == (22, 900, "Школа id 900")
    again = schools.load_current()
    assert again.school_of(11) == 1234 and again.label == "Школа id 900" and "type" not in again.profiles[0]


def test_default_paths(data_dir):
    assert schools.default_path("cache") == paths.cache_dir()
    p = schools.default_path("mapping", 1234)
    assert p == os.path.join(str(data_dir), "schools", "1234", "mapping.json")
    assert schools.is_default_path("mapping", p) and schools.is_default_path("mapping", paths.mapping_file())
    assert schools.is_default_path("journal", "")
    assert not schools.is_default_path("mapping", str(data_dir / "elsewhere" / "mapping.json"))
    assert not schools.is_default_path("mapping", schools.default_path("journal", 1234))


def _legacy(data_dir, school_id=1234):
    if school_id is not None:
        os.makedirs(paths.cache_dir())
        with open(os.path.join(paths.cache_dir(), "class_units.json"), "w") as f:
            json.dump([{"id": 1, "school_id": school_id}], f)
    with open(paths.mapping_file(), "w") as f:
        f.write("{}")


def test_migrate_legacy_to_owner(data_dir):
    _legacy(data_dir, 1234)
    assert schools.legacy_owner() == 1234
    assert schools.migrate_legacy(900, 2) == []                     # чужая школа — не трогаем
    moved = schools.migrate_legacy(1234, 2)
    assert set(moved) == {schools.default_path("cache", 1234), schools.default_path("mapping", 1234)}
    assert not os.path.exists(paths.mapping_file()) and os.path.exists(schools.default_path("mapping", 1234))
    assert schools.migrate_legacy(1234, 2) == []                     # уже перенесено


def test_migrate_legacy_without_cache(data_dir):
    """Без кэша хозяин данных неизвестен: переносим, только если школа у пользователя одна."""
    _legacy(data_dir, None)
    assert schools.migrate_legacy(1234, 2) == []
    assert schools.migrate_legacy(1234, 1) == [schools.default_path("mapping", 1234)]


def test_migrate_keeps_existing_school_files(data_dir):
    _legacy(data_dir, 1234)
    os.makedirs(schools.school_dir(1234))
    with open(schools.default_path("mapping", 1234), "w") as f:
        f.write('{"x": 1}')
    assert schools.migrate_legacy(1234, 1) == [schools.default_path("cache", 1234)]
    assert os.path.exists(paths.mapping_file())


def test_cli_paths_follow_school(data_dir):
    args = cli.build_parser().parse_args(["match", "f.chngf"])
    cli.resolve_school_paths(args)
    assert args.mapping == paths.mapping_file() and args.school_id is None       # школа ещё не выбрана
    schools.remember(P1234, [P1234, P900])
    args = cli.build_parser().parse_args(["upload", "f.chngf", "--profile-id", "22", "--journal", "my.jsonl"])
    cli.resolve_school_paths(args)
    assert args.school_id == 900 and args.journal == "my.jsonl"
    assert args.cache == schools.default_path("cache", 900) and args.mapping == schools.default_path("mapping", 900)


def test_cli_import_export_mapping(data_dir, tmp_path, capsys):
    schools.remember(P1234, [P1234, P900])
    own = schools.default_path("mapping", 1234)
    src = tmp_path / "from_other_pc.json"
    src.write_text('{"classes": {"5А": "5-А"}}', encoding="utf-8")
    cli.main(["import-mapping", str(src)])
    assert Mapping.load(own).classes == {"5А": "5-А"}
    src.write_text('{"classes": {"5А": "5-Б"}}', encoding="utf-8")
    with pytest.raises(SystemExit):              # уже есть — без --force не заменяем
        cli.main(["import-mapping", str(src)])
    assert Mapping.load(own).classes == {"5А": "5-А"}
    cli.main(["import-mapping", str(src), "--force"])
    assert Mapping.load(own).classes == {"5А": "5-Б"}
    dest = tmp_path / "out" / "copy.json"
    dest.parent.mkdir()
    cli.main(["export-mapping", str(dest)])
    assert Mapping.load(dest).classes == {"5А": "5-Б"}
    junk = tmp_path / "junk.json"
    junk.write_text('{"something": 1}', encoding="utf-8")
    with pytest.raises(SystemExit):              # чужой JSON — не сопоставление
        cli.main(["import-mapping", str(junk), "--force"])
    assert Mapping.load(own).classes == {"5А": "5-Б"}
