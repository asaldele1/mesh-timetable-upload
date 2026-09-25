"""Вкладка «Сопоставление»: ручное и автоматическое сопоставление сущностей ХроноГрафа с МЭШ."""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from collections.abc import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import chngf
from ..mapping import (
    GroupResolver,
    Mapping,
    group_key,
    group_teacher_ids,
    is_ec,
    match_all,
    relevant_lessons,
    split_group_key,
)
from ..refdata import ec_course_name, norm_text
from .common import (
    COLOR_BAD,
    COLOR_OFF,
    COLOR_WARN,
    Cell,
    CheckListDialog,
    ChoiceDelegate,
    PickDialog,
    RowModel,
    TableView,
    error,
    info,
    item,
    make_table,
    scrollable,
)


# ---------------------------------------------------------------------- данные для таблиц
class Ctx:
    """Производные данные, общие для всех таблиц вкладки (пересчитываются при обновлении)."""

    def __init__(self, tt: chngf.Timetable, rd, m):
        self.tt, self.rd, self.m = tt, rd, m
        self.lessons = list(tt.iter_lessons())
        self.class_count = Counter(l.cls for l in self.lessons)
        self.subject_count = Counter(l.subject for l in self.lessons)
        self.teacher_count = Counter(l.teacher for l in self.lessons if l.teacher)
        self.cabinet_count = Counter(l.cabinet for l in self.lessons if l.cabinet)
        self.ec_only = set(rd.ec_course_names) - set(rd.subject_names)
        self.mesh_subject_names = sorted(set(rd.subject_names) | self.ec_only, key=str.lower)
        self.groups_per_subject = Counter(g.get("subject_name") for g in rd.groups)
        self.ec_groups_per_subject = Counter(g.get("subject_name") for g in rd.ec_group_by_id.values())
        self.class_choices = [(cu["name"], cu["name"]) for cu in
                              sorted(rd.class_units, key=lambda c: (c.get("class_level_id") or 0, c.get("name") or ""))]
        self.teacher_choices = sorted(((f"{t['name']} (id {t['id']})", t["id"]) for t in rd.teachers if t.get("name")),
                                      key=lambda x: x[0].lower())
        self.building_names: dict[int, str] = {}
        for t in rd.teachers:
            for b in t.get("buildings") or []:
                if b.get("id") and b.get("name"):
                    self.building_names.setdefault(b["id"], b["name"])
        self.rooms = [r for r in rd.rooms if not r.get("deleted_at")]
        self.room_choices = [(self.room_label(r), r["id"]) for r in
                             sorted(self.rooms, key=lambda r: (r.get("building_id") or 0, _natural(r.get("number") or r.get("name") or "")))]
        self.building_choices = [(self.building_label(b), b) for b in sorted({r.get("building_id") for r in self.rooms if r.get("building_id")})]

    def building_label(self, bid) -> str:
        return f"{self.building_names.get(bid, 'здание')} (id {bid})"

    def room_label(self, r: dict) -> str:
        num, name = (r.get("number") or "").strip(), (r.get("name") or "").strip()
        head = name if not num or name.startswith(num) else (f"{num} — {name}" if name else num)
        return f"{head} · {self.building_names.get(r.get('building_id'), 'здание ' + str(r.get('building_id')))}"

    def group_label(self, g: dict) -> str:
        return f"{g['name']} [{g.get('subject_name')}] — {self._teachers(g)}" + (" (ВД)" if is_ec(g) else "") + f" · id {g['id']}"

    def group_short(self, g: dict) -> str:
        """Подпись группы для ячейки таблицы: без предмета (он в соседнем столбце) и id (они — в подсказке)."""
        return f"{g['name']} — {self._teachers(g)}" + (" (ВД)" if is_ec(g) else "")

    @staticmethod
    def _teachers(g: dict) -> str:
        return ", ".join(t.get("full_name") or str(t.get("id")) for t in g.get("teachers") or []) or "без учителя"

    def class_unit_id(self, cls_name: str) -> int | None:
        cu_name = self.m.classes.get(cls_name)
        cu = self.rd.class_unit(cu_name) if cu_name else None
        return cu["id"] if cu else None


def _short_reason(why: str) -> str:
    """Краткое состояние для столбца по пояснению resolve_group()."""
    if not why:
        return "не найдена"
    if why.startswith("несколько групп"):
        return "несколько групп — выберите вручную"
    if why.startswith("в классе"):
        return "нет групп по предмету"
    return why.split(":")[0][:60]


def _natural(s: str):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------------- базовая таблица
class EntityTab(QWidget):
    """Таблица сущностей (RowModel + TableView) с фильтром по тексту и переключателем «только проблемы»."""
    headers: list[str] = []
    problem_col: int | None = None
    editable_cols: tuple[int, ...] = ()
    check_cols: tuple[int, ...] = ()

    def __init__(self, owner: MappingTab):
        super().__init__()
        self.owner = owner
        self.ctx: Ctx | None = None
        self.stale = False             # данные изменились, таблица ещё не перестроена (перестроится при показе)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("поиск по таблице…")
        self.search.textChanged.connect(self.apply_filter)
        top.addWidget(self.search, 1)
        self.only_problems = QCheckBox("только проблемы")
        self.only_problems.toggled.connect(self.apply_filter)
        top.addWidget(self.only_problems)
        self.count_label = QLabel("")
        top.addWidget(self.count_label)
        self.extra_controls(top)
        lay.addLayout(top)
        self.model = RowModel(self.headers, self.editable_cols, self.check_cols, self)
        self.model.edited.connect(self._edited)
        self.table = TableView(self.model)
        self.table.doubleClicked.connect(lambda idx: self._double_clicked(idx.row(), idx.column()))
        lay.addWidget(self.table, 1)
        self.setup_table()

    def extra_controls(self, layout: QHBoxLayout) -> None:
        pass

    def setup_table(self) -> None:
        pass

    def fill(self, ctx: Ctx | None) -> None:
        """Перестроить таблицу по ``ctx``.  Невидимая вкладка (другая вкладка окна или подвкладка) не
        заполняется, а лишь помечается: таблица строится при показе."""
        self.ctx = ctx
        self.stale = True
        if self.isVisible():
            self._fill_now()

    def showEvent(self, event):
        super().showEvent(event)
        if self.stale:
            self._fill_now()

    def _fill_now(self) -> None:
        ctx = self.ctx
        self.stale = False
        vbar = self.table.verticalScrollBar().value()
        with self.model.bulk():
            if ctx is not None:
                self.fill_rows(ctx)
            else:
                self.model.set_rows([])
        self.table.fit_columns()
        self.table.verticalScrollBar().setValue(vbar)
        self.apply_filter()
        self.filled()

    def filled(self) -> None:
        """Вызывается после (возможно, отложенного) заполнения таблицы."""

    def fill_rows(self, ctx: Ctx) -> None:
        """Заполнить модель (``self.model.set_rows``) — вызывается внутри ``model.bulk()``."""
        raise NotImplementedError

    def apply_filter(self) -> None:
        shown = self.table.filter(self.search.text(), self.only_problems.isChecked(), self.problem_col)
        self.count_label.setText(f"{shown} из {self.model.rowCount()}")

    def _edited(self, row: int, col: int) -> None:
        if self.ctx is None:
            return
        if self.on_edited(row, col):
            self.owner.mapping_edited()

    def on_edited(self, row: int, col: int) -> bool:
        """Пользователь изменил ячейку (редактор или флажок); True — сопоставление изменилось."""
        return False

    def _double_clicked(self, row: int, col: int) -> None:
        if self.ctx is not None:
            self.on_double_clicked(row, col)

    def on_double_clicked(self, row: int, col: int) -> None:
        pass

    def set_status(self, row: int, col: int, text: str, color) -> None:
        self.model.put(row, col, text, tip=text, color=color)

    def key_at(self, row: int):
        """Ключ строки: имя сущности, для групп — занятие."""
        return self.model.key(row)

    def value_at(self, row: int, col: int):
        return self.model.value(row, col)

    def text_at(self, row: int, col: int) -> str:
        return self.model.text(row, col)


def _suggest(name: str, options: list[str], n: int = 3) -> str:
    sugg = difflib.get_close_matches(norm_text(name), [norm_text(o) for o in options], n=n, cutoff=0.5)
    back = {norm_text(o): o for o in options}
    return ", ".join(back[s] for s in sugg if s in back)


# ---------------------------------------------------------------------- классы
class ClassesTab(EntityTab):
    headers = ["Переносить", "Класс", "Полное имя", "Занятий", "Класс МЭШ", "Состояние"]
    problem_col = 5
    COL_ON, COL_NAME, COL_FULL, COL_N, COL_MESH, COL_STATUS = range(6)
    editable_cols = (COL_MESH,)
    check_cols = (COL_ON,)

    def setup_table(self):
        self.table.setItemDelegateForColumn(self.COL_MESH, ChoiceDelegate(lambda row: self.ctx.class_choices, self.table))

    def fill_rows(self, ctx: Ctx):
        m = ctx.m
        rows, keys = [], []
        for c in ctx.tt.classes:
            cur = m.classes.get(c.name)
            rows.append([Cell("", c.name not in m.ignore_classes, "снимите отметку, чтобы не переносить класс"),
                         Cell(c.name), Cell(c.full_name), Cell(ctx.class_count.get(c.name, 0)),
                         Cell(cur or "", cur), Cell("")])
            keys.append(c.name)
        self.model.set_rows(rows, keys)
        for r in range(len(rows)):
            self.update_status(r)

    def update_status(self, row: int):
        ctx = self.ctx
        name = self.key_at(row)
        on = self.value_at(row, self.COL_ON)
        cur = self.value_at(row, self.COL_MESH)
        if not on:
            self.set_status(row, self.COL_STATUS, "", COLOR_OFF)
        elif not cur:
            sugg = _suggest(name, [n for _, n in ctx.class_choices])
            self.set_status(row, self.COL_STATUS, "не сопоставлен" + (f"; похожие: {sugg}" if sugg else ""), COLOR_BAD)
        elif not ctx.rd.class_unit(cur):
            self.set_status(row, self.COL_STATUS, f"в МЭШ нет класса «{cur}»", COLOR_BAD)
        else:
            self.set_status(row, self.COL_STATUS, "", None)

    def on_edited(self, row, col) -> bool:
        ctx, name = self.ctx, self.key_at(row)
        if col == self.COL_ON:
            on = self.value_at(row, col)
            if on and name in ctx.m.ignore_classes:
                ctx.m.ignore_classes.remove(name)
            elif not on and name not in ctx.m.ignore_classes:
                ctx.m.ignore_classes.append(name)
        elif col == self.COL_MESH:
            ctx.m.classes[name] = self.value_at(row, col)
        else:
            return False
        self.update_status(row)
        return True


# ---------------------------------------------------------------------- предметы
class SubjectsTab(EntityTab):
    headers = ["Переносить", "Предмет ХроноГрафа", "Уроков", "Предметы / курсы ВД в МЭШ по приоритету (двойной щелчок)", "Групп МЭШ", "Состояние"]
    problem_col = 5
    COL_ON, COL_NAME, COL_N, COL_MESH, COL_GROUPS, COL_STATUS = range(6)
    check_cols = (COL_ON,)

    def fill_rows(self, ctx: Ctx):
        m = ctx.m
        names = sorted(ctx.subject_count, key=lambda s: -ctx.subject_count[s])
        rows = [[Cell("", name not in m.ignore_subjects, "снимите отметку, чтобы не переносить предмет"),
                 Cell(name), Cell(ctx.subject_count[name]), Cell(""), Cell(""), Cell("")] for name in names]
        self.model.set_rows(rows, names)
        for r in range(len(rows)):
            self.update_row(r)

    def update_row(self, row: int):
        ctx, name = self.ctx, self.key_at(row)
        on = self.value_at(row, self.COL_ON)
        cur = ctx.m.subjects.get(name) or []
        self.model.put(row, self.COL_MESH, " → ".join(f"{n} (ВД)" if n in ctx.ec_only else n for n in cur))
        n_oo = sum(ctx.groups_per_subject.get(n, 0) for n in cur)
        n_ec = sum(ctx.ec_groups_per_subject.get(n, 0) for n in cur)
        self.model.put(row, self.COL_GROUPS, f"{n_oo}" + (f" + {n_ec} ВД" if n_ec else "") if cur else "")
        if not on:
            self.set_status(row, self.COL_STATUS, "", COLOR_OFF)
        elif not cur:
            sugg = _suggest(name, ctx.mesh_subject_names, 4)
            self.set_status(row, self.COL_STATUS, "не сопоставлен" + (f"; похожие: {sugg}" if sugg else ""), COLOR_BAD)
        else:
            missing = [n for n in cur if n not in ctx.mesh_subject_names]
            if missing:
                self.set_status(row, self.COL_STATUS, f"нет в МЭШ: {', '.join(missing)}", COLOR_BAD)
            elif n_oo + n_ec == 0:
                self.set_status(row, self.COL_STATUS, "в МЭШ нет групп по этим предметам", COLOR_WARN)
            else:
                self.set_status(row, self.COL_STATUS, "", None)

    def on_edited(self, row, col) -> bool:
        if col != self.COL_ON:
            return False
        ctx, name = self.ctx, self.key_at(row)
        on = self.value_at(row, col)
        if on and name in ctx.m.ignore_subjects:
            ctx.m.ignore_subjects.remove(name)
        elif not on and name not in ctx.m.ignore_subjects:
            ctx.m.ignore_subjects.append(name)
        self.update_row(row)
        return True

    def on_double_clicked(self, row, col):
        if col not in (self.COL_MESH, self.COL_GROUPS, self.COL_STATUS):
            return
        ctx, name = self.ctx, self.key_at(row)
        options = [(f"{n} (ВД)" if n in ctx.ec_only else n, n) for n in ctx.mesh_subject_names]
        options = [(f"{lab}  ·  групп {ctx.groups_per_subject.get(v, 0) + ctx.ec_groups_per_subject.get(v, 0)}", v)
                   for lab, v in options]
        dlg = CheckListDialog(f"Предмет «{name}» — предметы и курсы МЭШ", options, ctx.m.subjects.get(name) or [], self,
                              "Отметьте названия предметов МЭШ (и курсов внеурочной деятельности), среди групп которых "
                              "искать группу для этого предмета. Порядок — приоритет: уроки недели ставятся в группу "
                              "первого названия, пока не исчерпаны её часы по нагрузке учителя, затем — второго и т. д. "
                              "(например, «Информатика → Технология»). В пределах названия обычные группы имеют "
                              "приоритет над группами ВД.", ordered=True)
        if dlg.exec():
            ctx.m.subjects[name] = dlg.values() or None
            self.update_row(row)
            self.owner.mapping_edited()


# ---------------------------------------------------------------------- учителя
class TeachersTab(EntityTab):
    headers = ["Учитель ХроноГрафа", "Уроков", "Учитель МЭШ", "Состояние"]
    problem_col = 3
    COL_NAME, COL_N, COL_MESH, COL_STATUS = range(4)
    editable_cols = (COL_MESH,)

    def setup_table(self):
        self.table.setItemDelegateForColumn(self.COL_MESH, ChoiceDelegate(lambda row: self.ctx.teacher_choices, self.table))

    def fill_rows(self, ctx: Ctx):
        names = sorted(ctx.teacher_count)
        self.model.set_rows([[Cell(name), Cell(ctx.teacher_count[name]), Cell(""), Cell("")] for name in names], names)
        for r in range(len(names)):
            self.update_row(r)

    def update_row(self, row: int):
        ctx, name = self.ctx, self.key_at(row)
        cur = ctx.m.teachers.get(name)
        tp = ctx.rd.teacher_by_id.get(cur) if cur else None
        self.model.put(row, self.COL_MESH, f"{tp['name']} (id {cur})" if tp else (f"id {cur}" if cur else ""), cur)
        if not cur:
            last = norm_text(name).split()[0] if norm_text(name) else ""
            sugg = ", ".join(lab for lab, _ in ctx.teacher_choices if last and norm_text(lab).startswith(last))[:200]
            self.set_status(row, self.COL_STATUS, "не сопоставлен" + (f"; похожие: {sugg}" if sugg else ""), COLOR_BAD)
        elif tp is None:
            self.set_status(row, self.COL_STATUS, f"id {cur} нет среди профилей в кэше", COLOR_BAD)
        elif tp.get("deleted"):
            self.set_status(row, self.COL_STATUS, "профиль удалён в МЭШ — сервер его не примет", COLOR_BAD)
        else:
            self.set_status(row, self.COL_STATUS, "", None)

    def on_edited(self, row, col) -> bool:
        if col != self.COL_MESH:
            return False
        self.ctx.m.teachers[self.key_at(row)] = self.value_at(row, col)
        self.update_row(row)
        return True


# ---------------------------------------------------------------------- кабинеты
class RoomsTab(EntityTab):
    headers = ["Кабинет ХроноГрафа", "Уроков", "Кабинет МЭШ", "Состояние"]
    problem_col = 3
    COL_NAME, COL_N, COL_MESH, COL_STATUS = range(4)
    editable_cols = (COL_MESH,)

    def __init__(self, owner):
        self._filling = False              # идёт заполнение таблицы префиксов — itemChanged не обрабатывать
        super().__init__(owner)

    def setup_table(self):
        self.table.setItemDelegateForColumn(self.COL_MESH, ChoiceDelegate(lambda row: self.ctx.room_choices, self.table))
        # таблица префиксов → здание
        self.prefixes = make_table(["Префикс кабинета", "Здание МЭШ (для автоподбора номеров)"])
        self.prefixes.setItemDelegateForColumn(1, ChoiceDelegate(lambda row: self.ctx.building_choices, self.prefixes))
        self.prefixes.setMaximumHeight(140)
        self.prefixes.itemChanged.connect(self._prefix_changed)
        box = QGroupBox("Префиксы кабинетов → здания")
        bl = QVBoxLayout(box)
        bl.addWidget(self.prefixes)
        self.layout().addWidget(box)

    def fill(self, ctx):
        super().fill(ctx)
        self._filling = True
        try:
            self.prefixes.setRowCount(0)
            if ctx is not None:
                for pfx, bid in sorted(ctx.m.room_prefixes.items()):
                    r = self.prefixes.rowCount()
                    self.prefixes.insertRow(r)
                    self.prefixes.setItem(r, 0, item(repr(pfx), pfx))
                    lab = ctx.building_label(bid) if bid else ""
                    self.prefixes.setItem(r, 1, item(lab, bid, editable=True, color=None if bid else COLOR_WARN))
                self.prefixes.fit_columns()
            # префиксов обычно 1–3: не отбирать у основной таблицы больше места, чем нужно
            self.prefixes.setMaximumHeight(min(140, 40 + 31 * max(1, self.prefixes.rowCount())))
        finally:
            self._filling = False

    def _prefix_changed(self, item):
        if self._filling or self.ctx is None or item.column() != 1:
            return
        pfx = self.prefixes.item(item.row(), 0).data(Qt.UserRole)
        self.ctx.m.room_prefixes[pfx] = item.data(Qt.UserRole)
        self.owner.mapping_edited()

    def fill_rows(self, ctx: Ctx):
        names = sorted(ctx.cabinet_count, key=_natural)
        self.model.set_rows([[Cell(name), Cell(ctx.cabinet_count[name]), Cell(""), Cell("")] for name in names], names)
        for r in range(len(names)):
            self.update_row(r)

    def update_row(self, row: int):
        ctx, name = self.ctx, self.key_at(row)
        cur = ctx.m.rooms.get(name)
        room = ctx.rd.room_by_id.get(cur) if cur else None
        self.model.put(row, self.COL_MESH, ctx.room_label(room) if room else (f"id {cur}" if cur else ""), cur)
        if not cur:
            self.set_status(row, self.COL_STATUS, "не сопоставлен: уроки получат кабинет учителя/здания по умолчанию, ВД — без кабинета", COLOR_WARN)
        elif room is None:
            self.set_status(row, self.COL_STATUS, f"id {cur} нет среди кабинетов в кэше", COLOR_BAD)
        elif room.get("deleted_at"):
            self.set_status(row, self.COL_STATUS, "кабинет удалён в МЭШ", COLOR_BAD)
        else:
            self.set_status(row, self.COL_STATUS, "", None)

    def on_edited(self, row, col) -> bool:
        if col != self.COL_MESH:
            return False
        self.ctx.m.rooms[self.key_at(row)] = self.value_at(row, col)
        self.update_row(row)
        return True


# ---------------------------------------------------------------------- группы
class GroupsTab(EntityTab):
    """Строка на каждую классогруппу и каждый приоритет её предмета (название МЭШ, по которому в классе
    есть группы): уроки недели заполняют группу 1-го приоритета до её часов, затем 2-го и т. д."""
    headers = ["Класс", "Предмет", "Гр.", "Прио.", "Учитель (двойной щелчок)", "Ур./нед", "Часов/нед", "Состояние",
               "Группа МЭШ", "Задано вручную (двойной щелчок)", "Как выбрана"]
    problem_col = 7
    COL_CLS, COL_SUBJ, COL_GRP, COL_PRIO, COL_TEACHER, COL_N, COL_HOURS, COL_STATUS, COL_MESH, COL_MANUAL, COL_WHY = range(11)
    stats: Counter = Counter()                 # ok / weak / over / bad / ec по строкам (после заполнения)

    def fill_rows(self, ctx: Ctx):
        seen: dict[str, chngf.Lesson] = {}
        per_week: Counter = Counter()          # (ключ, неделя цикла) → уроков
        for l in relevant_lessons(ctx.tt, ctx.m):
            key = group_key(l)
            seen.setdefault(key, l)
            per_week[(key, l.week)] += 1
        self._rows_by_clsgrp: dict[int, list[int]] = defaultdict(list)
        self._resolver = GroupResolver(ctx.tt, ctx.rd, ctx.m)
        weeks = range(max(1, ctx.tt.week_count))
        rows, keys = [], []
        for key, l in seen.items():
            n_les = max(per_week[(key, w)] for w in weeks)
            for link in self._resolver.resolve_chain(l):
                self._rows_by_clsgrp[l.clsgrp_index].append(len(rows))
                rows.append([Cell(l.cls), Cell(l.subject), Cell(l.group or ""),
                             Cell(link.prio, link.prio, f"приоритет {link.prio}: {link.name or '—'}"),
                             Cell(l.teacher), Cell(n_les, n_les),
                             Cell(""), Cell(""), Cell(""), Cell(""), Cell("")])
                keys.append(l)
        self.model.set_rows(rows, keys)
        self._kinds = [("ok", False)] * len(rows)      # по строкам: (ok | weak | over | bad, группа ВД)
        self.refresh_rows(range(len(rows)), self._resolver)     # тот же resolver: цепочки уже посчитаны

    def prio_at(self, row: int) -> int:
        return self.value_at(row, self.COL_PRIO)

    def filled(self) -> None:
        self.owner.update_summary()          # сводка считается по строкам этой таблицы

    def refresh_rows(self, rows: Iterable[int], resolver: GroupResolver | None = None) -> list[int]:
        """Заново выбрать группу МЭШ в строках ``rows``, переписав только изменившиеся ячейки
        (таблица не перестраивается — прокрутка, выделение и ширина столбцов остаются).
        Возвращает номера изменившихся строк."""
        ctx, m, model = self.ctx, self.ctx.m, self.model
        resolver = self._resolver = resolver or GroupResolver(ctx.tt, ctx.rd, m)
        changed = []
        for r in rows:
            l, prio = self.key_at(r), self.prio_at(r)
            chain = resolver.resolve_chain(l)
            link = next((lk for lk in chain if lk.prio == prio), None)
            if link is None:               # звено исчезло (снят ручной выбор по названию без групп) — до перезаполнения
                continue
            g, why = link.group, link.why
            explicit = m.groups.get(group_key(l, prio))
            eg = ctx.rd.any_group(explicit) if explicit else None
            tid, note = resolver.teacher_for(l, link) if g else (None, "")
            if note:
                why += f"; {note}"
            hours = ctx.rd.group_hours_span(g, tid) if g else ""
            n_les = self.value_at(r, self.COL_N)
            if g is None:
                kind, status, color = "bad", _short_reason(why), COLOR_BAD
            elif tid is None:
                # план такую группу пропустит: сервер принимает только учителя, привязанного к группе
                kind, status, color = "weak", "учитель не привязан к группе МЭШ", COLOR_WARN
            elif "проверьте" in why:
                kind, status, color = "weak", "проверьте", COLOR_WARN
            else:
                kind, status, color = "ok", "", None
                if prio == 1 and hours and n_les > min(_hours_range(hours)):
                    rest = [lk for lk in chain[1:] if lk.group]
                    if rest:
                        why += f"; часов меньше уроков — остальные по приоритету {', '.join(str(lk.prio) for lk in rest)}"
                    else:
                        kind, status, color = "over", "уроков больше часов группы — лишние встанут сверх часов", COLOR_WARN
            self._kinds[r] = (kind, is_ec(g))
            t_text, t_tip = self._teacher_cell(l, prio)
            touched = model.put(r, self.COL_TEACHER, t_text, tip=t_tip)
            touched |= model.put(r, self.COL_HOURS, hours, tip="часов в неделю по нагрузке учителя в МЭШ" if hours else "нет данных о нагрузке")
            touched |= model.put(r, self.COL_MESH, ctx.group_short(g) if g else "", tip=ctx.group_label(g) if g else "")
            touched |= model.put(r, self.COL_WHY, why, tip=why)
            touched |= model.put(r, self.COL_MANUAL, ctx.group_short(eg) if eg else (f"id {explicit}" if explicit else ""),
                                 explicit, tip=ctx.group_label(eg) if eg else "")
            if touched or model.text(r, self.COL_STATUS) != status:
                self.set_status(r, self.COL_STATUS, status, color)
                changed.append(r)
        self.stats = Counter(kind for kind, _ in self._kinds)
        self.stats["ec"] = sum(ec for _, ec in self._kinds)
        return changed

    def _teacher_cell(self, l: chngf.Lesson, prio: int) -> tuple[str, str]:
        """(текст, подсказка) столбца «Учитель»: учитель ХроноГрафа и, если задана принудительная
        привязка, учитель МЭШ, от имени которого ставятся занятия."""
        forced = self._resolver.forced_teacher(l, prio)
        if not forced:
            return l.teacher or "", ""
        name = (self.ctx.rd.teacher_by_id.get(forced) or {}).get("name") or f"id {forced}"
        return (f"{l.teacher or '—'} → {name}",
                f"привязано принудительно: занятия поставятся от учителя МЭШ {name} (id {forced})")

    def on_double_clicked(self, row, col):
        if col == self.COL_TEACHER:
            self.force_teacher(row)
            return
        ctx, l, prio = self.ctx, self.key_at(row), self.prio_at(row)
        cu_id = ctx.class_unit_id(l.cls)
        if cu_id is None:
            info(self, "Группа МЭШ", f"Класс «{l.cls}» не сопоставлен с классом МЭШ — сначала задайте его на вкладке «Классы».")
            return
        names = ctx.m.subjects.get(l.subject) or []
        name = names[prio - 1] if prio <= len(names) else ""
        key = group_key(l, prio)
        # кому ещё эта группа назначена вручную — чтобы не отдать одну группу двум подгруппам по ошибке
        taken: dict[int, list[str]] = defaultdict(list)
        for k, gid in ctx.m.groups.items():
            if gid and k != key:
                c, sub, grp, p = split_group_key(k)
                taken[gid].append(f"{c} {sub}" + (f"/{grp}" if grp and grp != "0" else "") + (f" (прио. {p})" if p > 1 else ""))
        tid = self._resolver.teacher_id(l)
        rows = []
        for g in list(ctx.rd.groups_by_class.get(cu_id, [])) + list(ctx.rd.ec_groups_by_class.get(cu_id, [])):
            rows.append(([g.get("name") or "", g.get("subject_name") or "", ctx._teachers(g), ctx.rd.group_hours_span(g, tid),
                          len(g.get("student_ids") or []), "ВД" if is_ec(g) else "",
                          "; ".join(taken.get(g["id"], [])), g["id"]],
                         g["id"], g.get("subject_name") == name if name else g.get("subject_name") in names))
        auto = self.text_at(row, self.COL_MESH)
        why = self.text_at(row, self.COL_WHY)
        hint = (f"Занятие: {l.cls}, {l.subject}" + (f"/{l.group}" if l.group else "") + f", учитель {l.teacher or '—'}"
                + (f"; приоритет {prio} — «{name}»" if len(names) > 1 else "") + ". "
                + (f"Сейчас выбрана автоматически: {auto} ({why})." if auto else f"Автоматически группа не выбрана: {why or '—'}.")
                + " Выберите группу МЭШ из списка (двойной щелчок) или нажмите «Автоматически», чтобы убрать ручной выбор."
                + ("" if names else " Предмет не сопоставлен — список не отфильтрован по предмету."))
        dlg = PickDialog(f"Группа МЭШ для {l.cls} — {l.subject}" + (f"/{l.group}" if l.group else "")
                         + (f", приоритет {prio}" if len(names) > 1 else ""),
                         ["Группа", "Предмет", "Учителя", "Часов/нед", "Учеников", "ВД", "Уже задана вручную для", "id"],
                         rows, ctx.m.groups.get(key), self, hint, auto_label="Автоматически", only_fit_label="только по предмету")
        res = dlg.exec()
        if not res:
            return
        value = None if dlg.is_auto() else dlg.value()
        if value == ctx.m.groups.get(key):
            return
        if value is None:
            ctx.m.groups.pop(key, None)
        else:
            ctx.m.groups[key] = value
        forced = ctx.m.force_teachers.get(key)
        if forced and forced not in group_teacher_ids(ctx.rd.any_group(value) if value else None):
            ctx.m.force_teachers.pop(key, None)    # привязка к учителю прежней группы устарела
        n_before = len(self._resolver.resolve_chain(l))
        # от выбора зависят ещё строки идентичных подгрупп (GroupResolver подбирает им группу по составу
        # учеников уже сопоставленных) — пересчитываем их, остальные строки не трогаем
        identical = self._resolver.identical.get(l.clsgrp_index, ())
        rows_ = list(self._rows_by_clsgrp.get(l.clsgrp_index, ())) + \
                [r for i in sorted(identical) for r in self._rows_by_clsgrp.get(i, ())]
        rebuilt = self._apply_edit(rows_, l, n_before)
        # выбранная группа не примет занятия от учителя ХроноГрафа — сразу предлагаем принудительную привязку
        if not rebuilt and self.text_at(row, self.COL_STATUS) == "учитель не привязан к группе МЭШ":
            self.force_teacher(row)

    def force_teacher(self, row: int) -> None:
        """Принудительная привязка классогруппы к учителю группы МЭШ: занятия ставятся от его имени,
        даже если учитель ХроноГрафа — другой (сервер принимает только учителя группы)."""
        ctx, l, prio = self.ctx, self.key_at(row), self.prio_at(row)
        key = group_key(l, prio)
        link = next((lk for lk in self._resolver.resolve_chain(l) if lk.prio == prio), None)
        g = link.group if link else None
        if g is None:
            info(self, "Учитель группы МЭШ",
                 "Для этой строки не выбрана группа МЭШ — сначала задайте её (двойной щелчок в столбце "
                 "«Задано вручную»).")
            return
        ids = group_teacher_ids(g)
        if not ids:
            info(self, "Учитель группы МЭШ", f"В группе «{g['name']}» нет учителей — привязывать не к кому, "
                                             "и сервер не примет в неё занятия. Назначьте учителя группе в МЭШ "
                                             "и обновите справочники.")
            return
        tid = self._resolver.teacher_id(l)
        rows = [([(ctx.rd.teacher_by_id.get(i) or {}).get("name") or "?", ctx.rd.group_hours_span(g, i),
                  "да" if i == tid else "", i], i, True) for i in ids]
        hint = (f"Занятие: {l.cls}, {l.subject}" + (f"/{l.group}" if l.group else "")
                + f", учитель ХроноГрафа {l.teacher or '—'}"
                + (f" (в МЭШ id {tid})" if tid else " (не сопоставлен)")
                + f". Группа МЭШ: {ctx.group_label(g)}. "
                + "Выберите учителя группы, от имени которого ставить занятия: так занятия попадут в эту "
                + "группу, даже если учитель в ней другой. Часы берутся из нагрузки выбранного учителя.")
        dlg = PickDialog(f"Учитель для {l.cls} — {l.subject}" + (f"/{l.group}" if l.group else "")
                         + (f", приоритет {prio}" if prio > 1 else ""),
                         ["Учитель группы МЭШ", "Часов/нед", "Учитель занятия", "id"],
                         rows, ctx.m.force_teachers.get(key), self, hint,
                         auto_label="Без принудительной привязки")
        if not dlg.exec():
            return
        value = None if dlg.is_auto() else dlg.value()
        if value == ctx.m.force_teachers.get(key):
            return
        if value is None:
            ctx.m.force_teachers.pop(key, None)
        else:
            ctx.m.force_teachers[key] = value
        self._apply_edit(self._rows_by_clsgrp.get(l.clsgrp_index, [row]), l,
                         len(self._resolver.resolve_chain(l)))

    def _apply_edit(self, rows_: Iterable[int], l: chngf.Lesson, n_before: int) -> bool:
        """Пересчитать строки после правки сопоставления; True — таблицу пришлось перестроить целиком."""
        changed = self.refresh_rows(rows_)
        rebuilt = len(self._resolver.resolve_chain(l)) != n_before
        if rebuilt:
            self.fill(self.ctx)                # число звеньев изменилось — строки перестраиваем целиком
        elif changed and (self.search.text().strip() or self.only_problems.isChecked()):
            self.apply_filter()                # состояние строк изменилось — фильтр «только проблемы» мог устареть
        self.owner.mapping_edited(groups_stale=False)
        return rebuilt


def _hours_range(span: str) -> tuple[int, int]:
    """«1» → (1, 1), «1–2» → (1, 2)."""
    lo, _, hi = span.partition("–")
    return int(lo), int(hi or lo)


# ---------------------------------------------------------------------- настройки
class SettingsTab(QWidget):
    def __init__(self, owner: MappingTab):
        super().__init__()
        self.owner = owner
        self.m = None
        self._filling = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QWidget()
        outer.addWidget(scrollable(body))
        lay = QVBoxLayout(body)
        box = QGroupBox("Синонимы предметов (для автосопоставления)")
        bl = QVBoxLayout(box)
        hint = QLabel("Слева — название предмета ХроноГрафа в нормализованном виде (строчные буквы, без знаков "
                      "препинания, ё → е), справа — названия предметов/курсов МЭШ через «;». Применяются при "
                      "автосопоставлении к предметам, которые ещё не сопоставлены.")
        hint.setWordWrap(True)
        bl.addWidget(hint)
        self.syn = make_table(["Предмет ХроноГрафа (нормализованный)", "Названия в МЭШ через «;»"])
        self.syn.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked | QAbstractItemView.EditKeyPressed)
        self.syn.itemChanged.connect(self._syn_changed)
        bl.addWidget(self.syn)
        row = QHBoxLayout()
        b = QPushButton("Добавить")
        b.clicked.connect(self._syn_add)
        row.addWidget(b)
        b = QPushButton("Удалить выбранные")
        b.clicked.connect(self._syn_remove)
        row.addWidget(b)
        row.addStretch()
        bl.addLayout(row)
        lay.addWidget(box, 1)

        box = QGroupBox("Внеурочная деятельность и подгруппы")
        form = QFormLayout(box)
        self.durations = QLineEdit()
        self.durations.setToolTip("допустимые длительности занятия ВД в минутах, через запятую")
        self.durations.editingFinished.connect(self._settings_changed)
        form.addRow("Длительности занятий ВД, мин:", self.durations)
        self.ec_suffix = QLineEdit()
        self.ec_suffix.editingFinished.connect(self._settings_changed)
        form.addRow("Regex хвоста с классами в названии курса ВД:", self.ec_suffix)
        self.ec_example = QLabel("")
        self.ec_example.setWordWrap(True)
        form.addRow("Пример:", self.ec_example)
        self.grp_suffix = QLineEdit()
        self.grp_suffix.editingFinished.connect(self._settings_changed)
        form.addRow("Regex номера подгруппы в названии группы МЭШ:", self.grp_suffix)
        self.grp_example = QLabel("")
        self.grp_example.setWordWrap(True)
        form.addRow("Пример:", self.grp_example)
        lay.addWidget(box)

    def fill(self, m):
        self.m = m
        self._filling = True
        try:
            self.syn.setRowCount(0)
            for k, v in sorted(m.subject_synonyms.items()):
                r = self.syn.rowCount()
                self.syn.insertRow(r)
                self.syn.setItem(r, 0, item(k, editable=True))
                self.syn.setItem(r, 1, item("; ".join(v), editable=True))
            self.syn.fit_columns(600)
            self.durations.setText(", ".join(str(x) for x in m.ec_durations))
            self.ec_suffix.setText(m.ec_course_suffix)
            self.grp_suffix.setText(m.group_ordinal_suffix)
            self._examples()
        finally:
            self._filling = False

    def _examples(self):
        try:
            self.ec_example.setText("«Разговоры о важном 2-4» → «" + ec_course_name("Разговоры о важном 2-4", self.ec_suffix.text()) + "»")
        except re.error as e:
            self.ec_example.setText(f"ошибка в regex: {e}")
        try:
            mm = re.search(self.grp_suffix.text(), "Технология 5-А 2гр") if self.grp_suffix.text() else None
            self.grp_example.setText("«Технология 5-А 2гр» → номер " + (mm.group(1) if mm else "не найден"))
        except (re.error, IndexError) as e:
            self.grp_example.setText(f"ошибка в regex: {e}")

    def _settings_changed(self):
        if self.m is None or self._filling:
            return
        try:
            durations = [int(x) for x in re.split(r"[,\s;]+", self.durations.text().strip()) if x]
        except ValueError:
            error(self, "Настройки", "Длительности — целые числа через запятую.")
            return
        try:
            Mapping(ec_durations=durations, ec_course_suffix=self.ec_suffix.text(),
                    group_ordinal_suffix=self.grp_suffix.text()).check_settings()
        except ValueError as e:
            error(self, "Настройки", str(e))
            return
        changed = (durations != self.m.ec_durations or self.ec_suffix.text() != self.m.ec_course_suffix
                   or self.grp_suffix.text() != self.m.group_ordinal_suffix)
        self.m.ec_durations = durations
        self.m.ec_course_suffix = self.ec_suffix.text()
        self.m.group_ordinal_suffix = self.grp_suffix.text()
        self._examples()
        if changed:
            self.owner.mapping_edited(reindex=True)

    def _collect_synonyms(self):
        d: dict[str, list[str]] = {}
        for r in range(self.syn.rowCount()):
            k = norm_text(self.syn.item(r, 0).text())
            v = [x.strip() for x in self.syn.item(r, 1).text().split(";") if x.strip()]
            if k:
                d[k] = v
        self.m.subject_synonyms = d

    def _syn_changed(self, item):
        if self.m is None or self._filling:
            return
        self._collect_synonyms()
        self.owner.mapping_edited()

    def _syn_add(self):
        if self.m is None:
            return
        r = self.syn.rowCount()
        self._filling = True
        self.syn.insertRow(r)
        self.syn.setItem(r, 0, item("", editable=True))
        self.syn.setItem(r, 1, item("", editable=True))
        self._filling = False
        self.syn.editItem(self.syn.item(r, 0))

    def _syn_remove(self):
        if self.m is None:
            return
        rows = sorted({i.row() for i in self.syn.selectedIndexes()}, reverse=True)
        for r in rows:
            self.syn.removeRow(r)
        if rows:
            self._collect_synonyms()
            self.owner.mapping_edited()


# ---------------------------------------------------------------------- вкладка целиком
class MappingTab(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.state = app.state
        lay = QVBoxLayout(self)
        top = QHBoxLayout()
        self.auto_btn = QPushButton("Автосопоставление")
        self.auto_btn.setToolTip("Заполнить то, что удаётся сопоставить автоматически; заданное вручную не трогается")
        self.auto_btn.clicked.connect(self.auto_match)
        top.addWidget(self.auto_btn)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.clicked.connect(self.save)
        top.addWidget(self.save_btn)
        b = QPushButton("Перечитать из файла")
        b.clicked.connect(self.reload)
        top.addWidget(b)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        top.addWidget(self.summary, 1)
        lay.addLayout(top)
        self.tabs = QTabWidget()
        self.classes = ClassesTab(self)
        self.subjects = SubjectsTab(self)
        self.teachers = TeachersTab(self)
        self.rooms = RoomsTab(self)
        self.groups = GroupsTab(self)
        self.settings = SettingsTab(self)
        for w, name in ((self.classes, "Классы"), (self.subjects, "Предметы"), (self.teachers, "Учителя"),
                        (self.rooms, "Кабинеты"), (self.groups, "Группы"), (self.settings, "Настройки")):
            self.tabs.addTab(w, name)
        lay.addWidget(self.tabs, 1)
        self.state.changed.connect(self._on_changed)
        self.refresh()

    def _on_changed(self, what: str):
        if what in ("file", "refdata", "mapping"):
            self.refresh()
        elif what == "mapping_saved":
            self.update_summary()
        elif what == "refreshing":
            self._gate()

    def refresh(self):
        st = self.state
        ok, why = st.ready()
        ctx = Ctx(st.tt, st.rd, st.m) if ok else None
        for tab in (self.classes, self.subjects, self.teachers, self.rooms, self.groups):
            tab.fill(ctx)              # невидимые вкладки заполнятся при показе
        self.settings.fill(st.m)
        self._gate()
        self.update_summary(why)

    def _gate(self):
        """Пока справочники обновляются с сервера, то, что зависит от списка групп (предметы, группы,
        автосопоставление), недоступно; классы, учителя, кабинеты и настройки работают по прежнему кэшу."""
        st = self.state
        ok, _ = st.ready()
        busy = st.refreshing
        self.auto_btn.setEnabled(ok and not busy)
        self.auto_btn.setToolTip("справочники обновляются с сервера — подождите" if busy else "")
        for tab in (self.subjects, self.groups):
            i = self.tabs.indexOf(tab)
            self.tabs.setTabEnabled(i, not busy)
            self.tabs.setTabToolTip(i, "справочники обновляются с сервера — подождите" if busy else "")

    def update_summary(self, why: str = ""):
        st = self.state
        if st.tt is None or st.rd is None:
            self.summary.setText(why)
            return
        ctx = self.groups.ctx
        m = st.m
        cls_all = [c.name for c in st.tt.classes if c.name not in m.ignore_classes]
        subj_all = [s for s in ctx.subject_count if s not in m.ignore_subjects]
        relevant = relevant_lessons(st.tt, m)          # ctx.relevant устаревает при правках флажков
        used_teachers = {l.teacher for l in relevant if l.teacher}
        used_rooms = {l.cabinet for l in relevant if l.cabinet}
        gs = self.groups.stats
        groups = ("группы: пересчёт при открытии вкладки «Группы»" if self.groups.stale else
                  f"группы {gs['ok'] + gs['weak'] + gs['over']}/{gs['ok'] + gs['weak'] + gs['over'] + gs['bad']} (ВД {gs['ec']}, проверить {gs['weak']}"
                  + (f", уроков больше часов {gs['over']}" if gs['over'] else "") + ")")
        self.summary.setText(
            f"классы {sum(1 for c in cls_all if m.classes.get(c) and st.rd.class_unit(m.classes[c]))}/{len(cls_all)} · "
            f"предметы {sum(1 for s in subj_all if m.subjects.get(s))}/{len(subj_all)} · "
            f"учителя {sum(1 for t in used_teachers if m.teachers.get(t) in st.rd.teacher_by_id and not st.rd.teacher_deleted(m.teachers.get(t)))}"
            f"/{len(used_teachers)} · "
            f"кабинеты {sum(1 for r in used_rooms if m.rooms.get(r) in st.rd.room_by_id)}/{len(used_rooms)} · "
            + groups + ("   — есть несохранённые изменения" if st.mapping_dirty else ""))

    def mapping_edited(self, reindex: bool = False, groups_stale: bool = True):
        st = self.state
        st.mark_mapping_dirty()
        if reindex and st.rd is not None:
            st.rd._index(st.m.ec_course_suffix)
            self.refresh()
            return
        # выбор групп зависит от классов, предметов и учителей — вкладку «Группы» перестроим при её открытии
        # (она сейчас не видна: правка пришла с другой вкладки)
        if groups_stale:
            self.groups.fill(self.groups.ctx)      # видна — перестроится сейчас, иначе при показе
        self.update_summary()
        self.app.update_title()

    def save(self):
        try:
            self.state.save_mapping()
        except OSError as e:
            error(self, "Сопоставление", f"Не удалось записать файл:\n{e}")

    def reload(self):
        try:
            self.state.load_mapping()
        except Exception as e:
            error(self, "Сопоставление", f"Не удалось прочитать файл:\n{e}")

    def auto_match(self):
        st = self.state
        ok, why = st.ready()
        if not ok:
            error(self, "Автосопоставление", why)
            return

        def job(progress, message):
            return match_all(st.tt, st.rd, st.m)

        def done(rep):
            st.mark_mapping_dirty()
            self.app.log_text("== Отчёт автосопоставления ==\n" + rep.text())
            self.refresh()
            self.app.update_title()
            info(self, "Автосопоставление", f"Готово. Не сопоставлено: {rep.unresolved}.\n"
                 "Подробный отчёт — в журнале внизу окна; проблемные строки выделены в таблицах "
                 "(фильтр «только проблемы»). Не забудьте сохранить.")

        self.app.run(job, done, "Автосопоставление")
