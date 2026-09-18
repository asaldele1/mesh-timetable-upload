"""Командная строка: python -m timetable_upload <команда> ..."""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import logging
import sys
from dataclasses import asdict

from . import auth, chngf, export, paths, refdata, uploader
from .mapping import Mapping, match_all
from .mesh_api import MAX_WORKERS, MeshClient, MeshError, fmt_roles
from .uploader import DAYS, Journal

TOKEN_ENV = auth.TOKEN_ENV
CLASSES_HELP = "классы через запятую: имена (5а,5б), параллель целиком (5), шаблон (10*); по умолчанию все"
DAYS_HELP = "дни недели через запятую: пн,ср | 1-3 | пн-пт; по умолчанию все"


def parse_weeks(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return sorted(set(out))


def parse_list(spec: str | None) -> list[str] | None:
    return [c.strip() for c in spec.split(",") if c.strip()] if spec else None


def selection(args, tt: chngf.Timetable) -> uploader.Selection:
    """Выбор классов/дней/дат/вида занятий из аргументов plan/upload."""
    return uploader.Selection(classes=uploader.select_classes(args.classes, tt),
                              days=uploader.parse_days(getattr(args, "days", None)),
                              dates=uploader.parse_dates(getattr(args, "dates", None)),
                              only=getattr(args, "only", None))


def weeks_for(args, sel: uploader.Selection, w1: _dt.date) -> list[int]:
    """Недели из --weeks и/или --dates (пересечение, если заданы обе)."""
    weeks = sel.weeks_for(parse_weeks(args.weeks) if args.weeks else None, w1)
    if not weeks:
        raise ValueError("укажите --weeks и/или --dates")
    return weeks


def week1(args, tt: chngf.Timetable | None, rd: refdata.RefData | None = None) -> _dt.date:
    override = getattr(args, "week1_monday", None)
    return uploader.week1_monday_for(tt, _dt.date.fromisoformat(override) if override else None,
                                     rd.academic_year if rd else None)


def make_client(args, rd: refdata.RefData | None = None) -> MeshClient:
    """Клиент по токену из --token-file (или переменной MESH_TOKEN) с выполненным входом.
    Истекающий токен обновляется по refresh-токену (см. auth); то же при ответе 401.
    rd — кэш справочников, с которым будет работать клиент: его учебный год должен совпадать с годом входа."""
    token = auth.current_token(args.token_file)
    client = MeshClient(token, delay=getattr(args, "delay", 0),
                        refresh=lambda: auth.current_token(args.token_file, force=True))
    s = client.login(profile_id=args.profile_id, academic_year=args.academic_year)
    logging.getLogger(__name__).info("профиль %s (%s), учебный год %s (id %s)",
                                     s.profile_id, s.profile.get("name") or s.profile.get("school_name"),
                                     s.academic_year.get("name"), s.academic_year_id)
    if rd is not None:
        rd.check_academic_year(client)
    return client


# ---------------------------------------------------------------------- команды
def cmd_info(args):
    tt = chngf.load(args.file)
    print(f"Файл: {args.file}")
    print(f"Проект: {tt.title.strip()} / {tt.project_name} (версия {tt.version}, сохранён {tt.saved_at})")
    print(f"Школа: {tt.school_name}; учебный год {tt.teach_year} {tt.half_year_name}; период {tt.period_begin} — {tt.period_end}")
    bells = list(zip(tt.lesson_begin, tt.lesson_end))[:tt.lesson_count]
    print(f"Сетка: {tt.day_count} дн. × {tt.lesson_count} ур. × {tt.week_count} нед.; "
          f"звонки: {', '.join(f'{b:%H:%M}-{e:%H:%M}' for b, e in bells)}")
    lessons = list(tt.iter_lessons())
    print(f"Классов {len(tt.classes)}, учителей {len(tt.teachers)}, предметов {len(tt.subjects)}, "
          f"кабинетов {len(tt.cabinets)}, классогрупп {len(tt.clsgrps)}, занятий в расписании {len(lessons)} "
          f"(с кабинетом: {sum(1 for l in lessons if l.cabinet)})")
    print("Классы:", ", ".join(c.name for c in tt.classes))
    if tt.langs:
        print("Иностранные языки:", ", ".join(l.name for l in tt.langs))
    if args.verbose:
        print("Предметы:", "; ".join(f"{s.name} ({s.short_name})" for s in tt.subjects))
        print("Учителя:", "; ".join(t.name for t in tt.teachers))
        print("Кабинеты:", ", ".join(c.name for c in tt.cabinets))


def cmd_dump(args):
    tt = chngf.load(args.file)
    lessons = [l for lst in tt.lessons_by_class().values() for l in lst]
    if args.classes:
        wanted = set(uploader.select_classes(args.classes, tt))
        lessons = [l for l in lessons if l.cls in wanted]
    days = uploader.parse_days(args.days)
    if days is not None:
        lessons = [l for l in lessons if l.day in days]
    if args.format == "json":
        json.dump([asdict(l) for l in lessons], sys.stdout, ensure_ascii=False, indent=1)
        print()
        return
    if args.format == "csv":
        w = csv.writer(sys.stdout)
        w.writerow(["class", "week", "day", "lesson", "subject", "group", "teacher", "cabinet", "potok"])
        for l in lessons:
            w.writerow([l.cls, l.week + 1, l.day + 1, l.lesson + 1, l.subject, l.group, l.teacher, l.cabinet, "+".join(l.potok)])
        return
    cur = None
    for l in lessons:
        if l.cls != cur:
            cur = l.cls
            print(f"\n=== {cur} ===")
        grp = f"/{l.group}" if l.group else ""
        potok = f" (поток {'+'.join(l.potok)})" if l.potok else ""
        wk = f"н{l.week + 1} " if tt.week_count > 1 else ""
        print(f"{wk}{DAYS[l.day]} {l.lesson + 1}: {l.subject}{grp} — {l.teacher or '—'} [{l.cabinet or '—'}]{potok}")


def cmd_check_auth(args):
    client = make_client(args)
    print("Срок действия:", auth.describe(args.token_file))
    s = client.session_info
    print("Профиль:", json.dumps(s.profile, ensure_ascii=False))
    print("Учебный год:", json.dumps(s.academic_year, ensure_ascii=False))
    print("Все учебные годы:", ", ".join(f"{y.get('name')} (id {y.get('id')})" for y in client.academic_years()))
    p = client.teacher_profile()
    print(f"Профиль сотрудника: {p.get('name')} (id {p.get('id')}), роли {fmt_roles(p.get('roles'))}")


def cmd_fetch(args):
    client = make_client(args)
    levels = [int(x) for x in args.class_levels.split(",")] if args.class_levels else None
    rd = refdata.fetch(client, args.cache, levels)
    print("Загружено:", rd.summary())


def cmd_weeks(args):
    """Сводка по неделям: даты, число занятий на сервере, есть ли нагрузка у учителей.

    Счётчик занятий в списке недель сервер отдаёт неверно (0), а выборка занятий без
    фильтра по классам пуста — поэтому с --online занятия считаются запросом по всем классам.
    """
    rd = refdata.RefData.load(args.cache)
    tt = chngf.load(args.file) if args.file else None
    try:
        w1 = week1(args, tt, rd)
    except ValueError:           # ни файла, ни даты начала года в кэше — недели без дат
        w1 = None
    client = make_client(args, rd) if args.online else None
    cu_ids = [cu["id"] for cu in rd.class_units]
    loads: dict[int, int] = {}
    for g in rd.groups:
        for t in g.get("teachers") or []:
            for l in t.get("teacher_loads") or []:
                if l.get("hours"):
                    loads[l["week_number"]] = loads.get(l["week_number"], 0) + 1
    print(f"Учебный год {rd.academic_year.get('name')}: {rd.academic_year.get('begin_date')} — {rd.academic_year.get('end_date')}")
    ec_by_week = {x["week_number"]: x for x in client.ec_schedules()} if client else {}   # id недель ВД в кэше могли устареть
    for s in sorted(rd.schedules, key=lambda x: x["week_number"]):
        wk = s["week_number"]
        monday = (w1 + _dt.timedelta(days=7 * (wk - 1))).isoformat() if w1 else ""
        if client:
            items = client.schedule_items(wk, cu_ids)
            classes = len({it.get("class_unit_id") for it in items})
            ec_sched = ec_by_week.get(wk)
            ec_items = client.ec_schedule_items(ec_sched["id"], cu_ids) if ec_sched else []
            count = f"уроков на сервере {len(items):5} (классов {classes:2}), занятий ВД {len(ec_items):4}"
        else:
            count = "занятий на сервере: ? (нужен --online)"
        print(f"  неделя {wk:2d} {monday}: {count}, групп с нагрузкой учителя {loads.get(wk, 0)}")


def cmd_match(args):
    tt = chngf.load(args.file)
    m = Mapping.load(args.mapping)
    rd = refdata.RefData.load(args.cache, m.ec_course_suffix)
    for s in parse_list(args.ignore_subjects) or []:
        if s not in m.ignore_subjects:
            m.ignore_subjects.append(s)
    for c in uploader.select_classes(args.ignore_classes, tt) or []:
        if c not in m.ignore_classes:
            m.ignore_classes.append(c)
    if args.only_classes:
        keep = set(uploader.select_classes(args.only_classes, tt))
        for c in tt.classes:
            if c.name not in keep and c.name not in m.ignore_classes:
                m.ignore_classes.append(c.name)
    rep = match_all(tt, rd, m)
    m.save(args.mapping)
    print(rep.text())
    print(f"\nНе сопоставлено: {rep.unresolved}. Сопоставление записано в {args.mapping} — "
          f"заполните значения null вручную и запустите match ещё раз для проверки.")


def _load_all(args):
    tt = chngf.load(args.file)
    m = Mapping.load(args.mapping)
    rd = refdata.RefData.load(args.cache, m.ec_course_suffix)
    return tt, rd, m


def _print_plan(plan: uploader.Plan, verbose: bool):
    print(export.plan_text(plan, verbose))


def cmd_plan(args):
    tt, rd, m = _load_all(args)
    w1 = week1(args, tt, rd)
    client = make_client(args, rd) if args.online else None
    sel = selection(args, tt)
    plans = []
    for week in weeks_for(args, sel, w1):
        plan = uploader.plan_week(tt, rd, m, week, w1, sel, client, args.replace)
        _print_plan(plan, args.verbose)
        plans.append(plan)
    if args.export:
        export.save_plans(args.export, plans)
        print(f"План записан в {args.export}")


def cmd_upload(args):
    tt, rd, m = _load_all(args)
    w1 = week1(args, tt, rd)
    client = make_client(args, rd)
    journal = Journal(args.journal)
    total_err = 0
    sel = selection(args, tt)
    for week in weeks_for(args, sel, w1):
        plan = uploader.plan_week(tt, rd, m, week, w1, sel, client, args.replace)
        _print_plan(plan, args.verbose)
        if not args.apply:
            print("  (пробный прогон; добавьте --apply, чтобы применить)")
            continue

        def progress(done, total):
            if done % 25 == 0 or done == total:
                print(f"\r  выполнено {done}/{total}", end="", flush=True)
        res = uploader.apply_plan(client, plan, journal, stop_on_error=args.stop_on_error, progress=progress,
                                  workers=args.workers)
        print(f"\r  неделя {week}: создано {res.created}, удалено {res.deleted}, ошибок {len(res.errors)}; "
              f"журнал созданного: {args.journal}")
        for what, msg in res.errors:
            print(f"    ОШИБКА {what}\n      {msg}")
        total_err += len(res.errors)
    sys.exit(1 if total_err else 0)


def cmd_undo(args):
    journal = Journal(args.journal)
    recs = journal.read()
    weeks = parse_weeks(args.weeks) if args.weeks else None
    # классы в журнале — имена из файла ХроноГрафа; без файла принимаем список как есть
    sel = uploader.Selection(classes=parse_list(args.classes), days=uploader.parse_days(args.days),
                             dates=uploader.parse_dates(args.dates), only=args.only)
    if not weeks and not sel.dates:
        # без ограничения по времени ничего не удаляем: слишком легко снести всё, что лежит в журнале
        in_journal = sorted({r["week"] for r in recs})
        raise ValueError("укажите --weeks и/или --dates" + (f" (в журнале недели: {', '.join(map(str, in_journal))})"
                                                             if in_journal else " (журнал пуст)"))
    todo = [r for r in recs if uploader.journal_matches(r, weeks, sel)]
    print(f"В журнале {len(recs)} занятий, к удалению {len(todo)}")
    if args.verbose:
        for r in todo:
            print(f"  - {'ВД ' if r.get('kind') == 'ec' else ''}{r['class']} {r['date']} урок {r['lesson']} {r['group_name']} (id {r['item_id']})")
    if not args.apply:
        print("  (пробный прогон; добавьте --apply, чтобы удалить)")
        return
    client = make_client(args)
    res = uploader.undo(client, journal, weeks, sel, workers=args.workers)
    print(f"Удалено {res.deleted}, ошибок {len(res.errors)}")
    for what, msg in res.errors:
        print(f"    ОШИБКА {what}\n      {msg}")


def cmd_copy(args):
    tt, rd, m = _load_all(args)
    client = make_client(args, rd)
    names = uploader.select_classes(args.classes, tt) or [c.name for c in tt.classes if c.name not in m.ignore_classes]
    cu_ids = []
    for n in names:
        cu = rd.class_unit(m.classes.get(n) or n)
        if not cu:
            print(f"класс {n!r} не сопоставлен с МЭШ — пропущен")
            continue
        cu_ids.append(cu["id"])
    for line in uploader.copy_week(client, rd, args.from_week, parse_weeks(args.to_weeks), cu_ids, args.force):
        print(line)


def cmd_gui(args):
    from .gui import main as gui_main
    sys.exit(gui_main([]))


def cmd_login(args):
    try:
        from .gui.web_login import main as login_main
    except ImportError as e:
        raise ValueError(f"для входа через браузер нужен PySide6 с QtWebEngine (pip install PySide6) "
                         f"либо timetable-upload-gui --login: {e}") from e
    sys.exit(login_main([args.token_file]))


def cmd_paths(args):
    for what, path, exists in paths.describe():
        print(f"{what:24} {path}{'' if exists else '   (нет)'}")
    print(f"Другая папка: переменная {paths.HOME_ENV}")


# ---------------------------------------------------------------------- разбор аргументов
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="timetable_upload",
                                 description="Перенос расписания из ХроноГраф 3.0 Мастер (.chngf) в МЭШ")
    ap.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def auth(p):
        p.add_argument("--token-file", default=paths.token_file(),
                       help=f"файл с aupd_token (по умолчанию {paths.token_file()}; либо переменная {TOKEN_ENV})")
        p.add_argument("--profile-id", type=int, help="id профиля сотрудника, если их несколько")
        p.add_argument("--academic-year", help="учебный год: id или название («2025/26»); по умолчанию текущий")
        p.add_argument("--delay", type=float, default=0, help="пауза между запросами, с (по умолчанию нет; при 429 сервер и так ждём)")

    def workers(p):
        p.add_argument("--workers", type=int, default=8, choices=range(1, MAX_WORKERS + 1), metavar="N",
                       help=f"сколько запросов слать одновременно, 1–{MAX_WORKERS} (по умолчанию 8)")

    def cache(p):
        p.add_argument("--cache", default=paths.cache_dir(),
                       help=f"каталог кэша справочников МЭШ (по умолчанию {paths.cache_dir()})")

    def inputs(p):
        p.add_argument("file", help="файл расписания .chngf")
        cache(p)
        p.add_argument("--mapping", default=paths.mapping_file(),
                       help=f"файл сопоставления (по умолчанию {paths.mapping_file()})")

    def selection(p, classes_help=CLASSES_HELP):
        p.add_argument("--weeks", help="номера учебных недель: 16 | 16-20 | 16,18-20 (можно вместо этого --dates)")
        p.add_argument("--dates", help="конкретные даты: 2027-05-24 | 24.05.2027 | 2027-05-24..2027-05-28, через запятую")
        p.add_argument("--days", help=DAYS_HELP)
        p.add_argument("--classes", help=classes_help)
        p.add_argument("--only", choices=["oo", "ec"], help="только уроки (oo) или только внеурочную деятельность (ec)")

    p = sub.add_parser("info", help="сведения о файле .chngf")
    p.add_argument("file")
    p.set_defaults(fn=cmd_info)

    p = sub.add_parser("dump", help="вывести расписание из .chngf")
    p.add_argument("file")
    p.add_argument("--classes", help=CLASSES_HELP)
    p.add_argument("--days", help=DAYS_HELP)
    p.add_argument("--format", choices=["text", "json", "csv"], default="text")
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("check-auth", help="проверить токен: профиль, учебный год")
    auth(p)
    p.set_defaults(fn=cmd_check_auth)

    p = sub.add_parser("login", help="войти в school.mos.ru в окне встроенного браузера и сохранить токен (нужен PySide6)")
    p.add_argument("--token-file", default=paths.token_file(), help=f"куда записать aupd_token (по умолчанию {paths.token_file()})")
    p.set_defaults(fn=cmd_login)

    p = sub.add_parser("paths", help="показать папку данных (кэш, сопоставление, токен, журнал)")
    p.set_defaults(fn=cmd_paths)

    p = sub.add_parser("fetch", help="загрузить справочники МЭШ в кэш (только чтение)")
    auth(p)
    cache(p)
    p.add_argument("--class-levels", help="только эти параллели, через запятую (по умолчанию все)")
    p.set_defaults(fn=cmd_fetch)

    p = sub.add_parser("weeks", help="сводка по неделям учебного года")
    auth(p)
    cache(p)
    p.add_argument("--file", help=".chngf для расчёта дат недель")
    p.add_argument("--week1-monday")
    p.add_argument("--online", action="store_true", help="посчитать занятия на сервере")
    p.set_defaults(fn=cmd_weeks)

    p = sub.add_parser("match", help="сопоставить классы/предметы/учителей/кабинеты/группы, записать mapping")
    inputs(p)
    p.add_argument("--ignore-subjects", help="предметы, которые не переносим (через запятую)")
    p.add_argument("--ignore-classes", help="классы, которые не переносим (" + CLASSES_HELP + ")")
    p.add_argument("--only-classes", help="переносить только эти классы, остальные — в ignore_classes (" + CLASSES_HELP + ")")
    p.set_defaults(fn=cmd_match)

    def common(p):
        inputs(p)
        selection(p)
        p.add_argument("--week1-monday", help="понедельник 1-й учебной недели (ГГГГ-ММ-ДД); по умолчанию — по дате начала учебного года")
        p.add_argument("--replace", action="store_true", help="удалять на сервере занятия наших классов, которых нет в файле")
        auth(p)

    p = sub.add_parser("plan", help="показать, что будет создано (с --online — и сравнить с сервером)")
    common(p)
    p.add_argument("--online", action="store_true", help="сравнить с занятиями на сервере")
    p.add_argument("--export", metavar="ФАЙЛ", help="записать план в файл: .csv — таблица для Excel, иначе текстовый отчёт")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("upload", help="синхронизировать неделю(и) с МЭШ (без --apply — пробный прогон)")
    common(p)
    p.add_argument("--apply", action="store_true", help="действительно выполнить изменения")
    p.add_argument("--stop-on-error", action="store_true", help="остановиться на первой ошибке")
    workers(p)
    p.add_argument("--journal", default=paths.journal_file(),
                   help=f"журнал созданных занятий для undo (по умолчанию {paths.journal_file()})")
    p.set_defaults(fn=cmd_upload)

    p = sub.add_parser("undo", help="удалить занятия, созданные командой upload (по журналу)")
    auth(p)
    p.add_argument("--journal", default=paths.journal_file())
    workers(p)
    selection(p, classes_help="классы через запятую, как они названы в файле ХроноГрафа")
    p.add_argument("--apply", action="store_true", help="действительно удалить")
    p.set_defaults(fn=cmd_undo)

    p = sub.add_parser("gui", help="графический интерфейс (нужен PySide6)")
    p.set_defaults(fn=cmd_gui)

    p = sub.add_parser("copy", help="скопировать неделю на другие недели средствами МЭШ (по классам)")
    inputs(p)
    auth(p)
    p.add_argument("--from-week", type=int, required=True)
    p.add_argument("--to-weeks", required=True)
    p.add_argument("--classes", help=CLASSES_HELP)
    p.add_argument("--force", action="store_true", help="копировать, даже если на целевых неделях уже есть занятия")
    p.set_defaults(fn=cmd_copy)
    return ap


def main(argv=None):
    # в консоли Windows (cp1251/cp866) символы вроде «→» не кодируются — заменяем, а не падаем
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        args.fn(args)
    except (chngf.ChngfError, MeshError, ValueError, FileNotFoundError) as e:   # AuthError — тоже ValueError
        print("Ошибка:", e, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
