"""Вкладка «Перенос в МЭШ»: выбор недель/дат/классов, план, сравнение с сервером, загрузка."""

from __future__ import annotations

import datetime as _dt
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import export, uploader
from ..mesh_api import MAX_WORKERS
from ..uploader import DAYS, Journal, Selection
from .common import CheckListDialog, WrapDelegate, ask, cell, check_cell, error, make_table, scrollable

_D = "%d.%m.%Y"


def week_span(monday: _dt.date) -> str:
    return f"{monday:%d.%m} — {monday + _dt.timedelta(days=6):{_D}}"


class UploadTab(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.state = app.state
        self.plans: list[uploader.Plan] = []
        self.plans_online = False
        self.shown: tuple[str, object] | None = None    # что сейчас в дереве: ("plan", планы) | ("result", итоги загрузки)

        split = QSplitter(Qt.Horizontal)
        lay = QHBoxLayout(self)
        lay.addWidget(split)

        # ---------------------------------------------------------- левая панель: что переносим
        left = QWidget()
        ll = QVBoxLayout(left)
        wbox = QGroupBox("Учебные недели")
        wl = QVBoxLayout(wbox)
        self.weeks = make_table(["", "№", "Даты", "На сервере: уроков / ВД"])
        self.weeks.setSelectionMode(QAbstractItemView.NoSelection)
        self.weeks.itemChanged.connect(self._weeks_changed)
        wl.addWidget(self.weeks)
        row = QHBoxLayout()
        b = QPushButton("Снять все")
        b.clicked.connect(lambda: self._check_weeks(False))
        row.addWidget(b)
        b = QPushButton("Посчитать занятия на сервере")
        b.setToolTip("Для отмеченных недель (или всех, если ничего не отмечено) запросить у сервера число уроков и занятий ВД")
        b.clicked.connect(self.count_on_server)
        row.addWidget(b)
        row.addStretch()
        wl.addLayout(row)
        ll.addWidget(wbox, 1)

        sbox = QGroupBox("Уточнение (необязательно)")
        form = QFormLayout(sbox)
        self.dates_edit = QLineEdit()
        self.dates_edit.setPlaceholderText("2027-05-26, 24.05.2027, 2027-05-24..2027-05-28")
        self.dates_edit.setToolTip("Конкретные даты; недели при этом можно не отмечать")
        self.dates_edit.textChanged.connect(lambda *_: self.invalidate())
        form.addRow("Даты:", self.dates_edit)
        drow = QHBoxLayout()
        self.day_boxes: list[QCheckBox] = []
        for d in DAYS:
            cb = QCheckBox(d)
            cb.setChecked(True)
            cb.toggled.connect(lambda *_: self.invalidate())
            drow.addWidget(cb)
            self.day_boxes.append(cb)
        drow.addStretch()
        form.addRow("Дни недели:", drow)
        crow = QHBoxLayout()
        self.classes_edit = QLineEdit()
        self.classes_edit.setPlaceholderText("все; либо 5а,5б | 5 (параллель) | 10* (шаблон)")
        self.classes_edit.textChanged.connect(self._classes_changed)
        crow.addWidget(self.classes_edit, 1)
        b = QPushButton("Выбрать…")
        b.clicked.connect(self.pick_classes)
        crow.addWidget(b)
        form.addRow("Классы:", crow)
        self.classes_label = QLabel("")
        self.classes_label.setWordWrap(True)
        form.addRow("", self.classes_label)
        krow = QHBoxLayout()
        self.kind_group = QButtonGroup(self)
        for i, (txt, val) in enumerate((("уроки и ВД", ""), ("только уроки", "oo"), ("только ВД", "ec"))):
            rb = QRadioButton(txt)
            rb.setProperty("kind", val)
            rb.setChecked(i == 0)
            self.kind_group.addButton(rb, i)
            krow.addWidget(rb)
        self.kind_group.buttonToggled.connect(lambda *_: self.invalidate())
        krow.addStretch()
        form.addRow("Вид занятий:", krow)
        self.replace_box = QCheckBox("Удалять лишнее на сервере и пересоздавать отличающиеся")
        self.replace_box.setToolTip("Удаляются только занятия выбранных классов на выбранные даты, которых нет в файле, "
                                    "и занятия с другим учителем/кабинетом (они создаются заново).\n"
                                    "Без этой отметки ничего не удаляется: только создаются недостающие занятия.")
        self.replace_box.toggled.connect(lambda *_: self.invalidate())
        form.addRow("", self.replace_box)
        ll.addWidget(sbox)

        wrow = QHBoxLayout()
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, MAX_WORKERS)
        self.workers_spin.setValue(self.state.workers)
        self.workers_spin.setToolTip("Сколько занятий отправлять на сервер одновременно (при загрузке и откате).\n"
                                     "Если в журнале сообщений появляются ответы 429 («слишком много запросов») — уменьшите.")
        self.workers_spin.valueChanged.connect(self.state.set_workers)
        wrow.addWidget(self.workers_spin)
        wrow.addWidget(QLabel("запросов одновременно"))
        wrow.addStretch()
        form.addRow("Параллельно:", wrow)

        brow = QHBoxLayout()
        self.plan_btn = QPushButton("Показать план")
        self.plan_btn.setToolTip("Построить план по файлу и кэшу, без обращения к серверу")
        self.plan_btn.clicked.connect(lambda: self.build(online=False))
        brow.addWidget(self.plan_btn)
        self.compare_btn = QPushButton("Сравнить с сервером")
        self.compare_btn.setToolTip("Построить план и сверить с занятиями на сервере (только чтение)")
        self.compare_btn.clicked.connect(lambda: self.build(online=True))
        brow.addWidget(self.compare_btn)
        self.apply_btn = QPushButton("Загрузить в МЭШ…")
        self.apply_btn.setEnabled(False)
        self.apply_btn.setToolTip("Доступно после сравнения с сервером")
        self.apply_btn.clicked.connect(self.apply)
        brow.addWidget(self.apply_btn)
        ll.addLayout(brow)
        left_scroll = scrollable(left)
        left_scroll.setMinimumWidth(max(560, left.minimumSizeHint().width() + 24))   # без горизонтальной прокрутки
        left_scroll.setMaximumWidth(720)
        split.addWidget(left_scroll)

        # ---------------------------------------------------------- правая панель: план
        right = QWidget()
        rl = QVBoxLayout(right)
        srow = QHBoxLayout()
        self.summary = QLabel("План не построен.")
        self.summary.setWordWrap(True)
        srow.addWidget(self.summary, 1)
        self.export_btn = QPushButton("Сохранить в файл…")
        self.export_btn.setToolTip("Записать показанный план или итоги загрузки (ошибки) в файл:\n"
                                   ".csv — таблица для Excel, .txt — текстовый отчёт")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_shown)
        srow.addWidget(self.export_btn, 0, Qt.AlignTop)
        rl.addLayout(srow)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Занятие", "Подробности"])
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setStretchLastSection(True)
        self.tree.setItemDelegate(WrapDelegate(self.tree))   # длинные ошибки и заметки переносятся, а не обрезаются
        rl.addWidget(self.tree, 1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        rl.addWidget(self.progress)
        split.addWidget(right)
        split.setStretchFactor(1, 1)
        split.setSizes([600, 800])

        self.state.changed.connect(self._on_changed)
        self._on_changed("refdata")

    # ------------------------------------------------------------------ недели
    def _on_changed(self, what: str):
        if what in ("file", "refdata", "paths"):
            self.fill_weeks()
            self._classes_changed()
        if what in ("file", "refdata", "mapping", "mapping_edited"):
            self.invalidate()

    def fill_weeks(self):
        st = self.state
        checked = {self.weeks.item(r, 1).data(Qt.UserRole) for r in range(self.weeks.rowCount())
                   if self.weeks.item(r, 0).checkState() == Qt.Checked}
        self.weeks.blockSignals(True)
        self.weeks.setRowCount(0)
        if st.rd is not None:
            try:
                w1 = st.week1_monday()
            except ValueError:
                w1 = None
            for s in sorted(st.rd.schedules, key=lambda x: x["week_number"]):
                wk = s["week_number"]
                r = self.weeks.rowCount()
                self.weeks.insertRow(r)
                self.weeks.setItem(r, 0, check_cell(wk in checked))
                self.weeks.setItem(r, 1, cell(wk, wk))
                self.weeks.setItem(r, 2, cell(week_span(w1 + _dt.timedelta(days=7 * (wk - 1))) if w1 else "?"))
                self.weeks.setItem(r, 3, cell(""))
            self.weeks.fit_columns()
        self.weeks.blockSignals(False)

    def _weeks_changed(self, item):
        if item.column() == 0:
            self.invalidate()

    def _check_weeks(self, on: bool):
        self.weeks.blockSignals(True)
        for r in range(self.weeks.rowCount()):
            self.weeks.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.weeks.blockSignals(False)
        self.invalidate()

    def checked_weeks(self) -> list[int]:
        return [self.weeks.item(r, 1).data(Qt.UserRole) for r in range(self.weeks.rowCount())
                if self.weeks.item(r, 0).checkState() == Qt.Checked]

    def count_on_server(self):
        st = self.state
        ok, why = st.ready()
        if not ok:
            error(self, "Недели", why)
            return
        weeks = self.checked_weeks() or [self.weeks.item(r, 1).data(Qt.UserRole) for r in range(self.weeks.rowCount())]
        rd = st.rd
        cu_ids = [cu["id"] for cu in rd.class_units]

        def job(progress, message):
            client = st.get_client_for_refdata()
            out = {}
            ec_by_week = {x["week_number"]: x for x in client.ec_schedules()}   # id недель ВД в кэше могли устареть
            for i, wk in enumerate(weeks):
                if self.app.stop_requested():
                    break
                items = client.schedule_items(wk, cu_ids)
                ec = ec_by_week.get(wk)
                ec_items = client.ec_schedule_items(ec["id"], cu_ids) if ec else []
                out[wk] = (len(items), len({it.get("class_unit_id") for it in items}), len(ec_items))
                progress(i + 1, len(weeks))
            return out

        def done(out):
            st.changed.emit("session")
            for r in range(self.weeks.rowCount()):
                wk = self.weeks.item(r, 1).data(Qt.UserRole)
                if wk in out:
                    n, classes, n_ec = out[wk]
                    self.weeks.item(r, 3).setText(f"{n} (классов {classes}) / {n_ec}")
            self.weeks.resizeColumnsToContents()

        self.app.run(job, done, "Подсчёт занятий на сервере", stoppable=True)

    # ------------------------------------------------------------------ классы
    def _classes_changed(self):
        st = self.state
        spec = self.classes_edit.text()
        if st.tt is None:
            self.classes_label.setText("")
            return
        try:
            names = uploader.select_classes(spec, st.tt)
        except ValueError as e:
            self.classes_label.setText(f"ошибка: {e}")
            self.classes_label.setStyleSheet("color: #b00")
            return
        self.classes_label.setStyleSheet("color: gray")
        if names is None:
            skip = len(st.m.ignore_classes)
            self.classes_label.setText(f"все классы файла ({len(st.tt.classes)})" + (f", кроме исключённых в сопоставлении ({skip})" if skip else ""))
        else:
            self.classes_label.setText(f"{len(names)}: " + ", ".join(names))
        self.invalidate()

    def pick_classes(self):
        st = self.state
        if st.tt is None:
            error(self, "Классы", "Сначала загрузите файл расписания.")
            return
        try:
            current = uploader.select_classes(self.classes_edit.text(), st.tt) or []
        except ValueError:
            current = []
        dlg = CheckListDialog("Классы для переноса", [(c.name, c.name) for c in st.tt.classes], current, self,
                              "Отметьте классы. Пустой выбор — все классы файла.")
        if dlg.exec():
            self.classes_edit.setText(",".join(dlg.values()))

    # ------------------------------------------------------------------ выбор
    def selection(self) -> tuple[list[int], Selection]:
        st = self.state
        classes = uploader.select_classes(self.classes_edit.text(), st.tt)
        days = {i for i, cb in enumerate(self.day_boxes) if cb.isChecked()}
        if not days:
            raise ValueError("не отмечен ни один день недели")
        dates = uploader.parse_dates(self.dates_edit.text())
        only = self.kind_group.checkedButton().property("kind") or None
        sel = Selection(classes=classes, days=None if len(days) == 7 else days, dates=dates, only=only)
        weeks = sel.weeks_for(self.checked_weeks(), st.week1_monday())
        if not weeks:
            raise ValueError("отметьте учебные недели или укажите даты")
        return weeks, sel

    def describe_selection(self, weeks: list[int], sel: Selection) -> str:
        parts = [f"недели {', '.join(map(str, weeks))}"]
        if sel.dates:
            parts.append("даты " + ", ".join(d.strftime(_D) for d in sorted(sel.dates)))
        if sel.days is not None:
            parts.append("дни " + ", ".join(DAYS[i] for i in sorted(sel.days)))
        parts.append(f"классы: {', '.join(sel.classes)}" if sel.classes else "все классы")
        if sel.only:
            parts.append("только уроки" if sel.only == "oo" else "только ВД")
        if self.replace_box.isChecked():
            parts.append("с удалением лишнего")
        return "; ".join(parts)

    def invalidate(self):
        """Любое изменение выбора (недели, даты, дни, классы, вид, «удалять лишнее») или данных
        (файл, кэш, сопоставление) делает построенный план неактуальным."""
        if self.plans:
            self.plans = []
            self.summary.setText("Выбор изменился — постройте план заново.")
            self._show(None)
        self.apply_btn.setEnabled(False)

    # ------------------------------------------------------------------ план
    def build(self, online: bool):
        st = self.state
        ok, why = st.ready()
        if not ok:
            error(self, "План", why)
            return
        try:
            weeks, sel = self.selection()
            w1 = st.week1_monday()
        except ValueError as e:
            error(self, "План", str(e))
            return
        replace = self.replace_box.isChecked()
        tt, rd, m = st.tt, st.rd, st.m

        def job(progress, message):
            client = st.get_client_for_refdata() if online else None
            plans = []
            for i, week in enumerate(weeks):
                if self.app.stop_requested():
                    return None
                if client:
                    message(f"неделя {week}: запрос занятий с сервера…")
                plans.append(uploader.plan_week(tt, rd, m, week, w1, sel, client, replace))
                progress(i + 1, len(weeks))
            return plans

        def done(plans):
            if online:
                st.changed.emit("session")
            if plans is None:
                self.summary.setText("Сравнение остановлено, план не построен.")
                return
            self.plans, self.plans_online = plans, online
            self.show_plans(self.describe_selection(weeks, sel))
            self._show(("plan", plans))
            self.apply_btn.setEnabled(online and any(p.to_create or p.to_delete for p in plans))

        self.app.run(job, done, "Сравнение с сервером" if online else "Построение плана", stoppable=online)

    def show_plans(self, selection_text: str):
        self.tree.clear()
        lines = [selection_text]
        n_items = n_create = n_delete = 0
        for plan in self.plans:
            monday = self.state.week1_monday() + _dt.timedelta(days=7 * (plan.week - 1))
            n_items += len(plan.items)
            n_create += len(plan.to_create)
            n_delete += len(plan.to_delete)
            top = QTreeWidgetItem([f"Неделя {plan.week} ({week_span(monday)})", plan.summary().split("\n")[0].split(": ", 1)[-1]])
            self.tree.addTopLevelItem(top)
            top.setExpanded(True)
            if self.plans_online:
                top.addChild(QTreeWidgetItem(["На сервере сейчас", f"{len(plan.existing)} (на выбранные даты, по классам плана)"]))
                self._add_group(top, f"Создать ({len(plan.to_create)})", [(p.describe(), p.note) for p in plan.to_create])
                self._add_group(top, f"Удалить ({len(plan.to_delete)})", [(uploader.describe_existing(it), "") for it in plan.to_delete],
                                expand=True)
                self._add_group(top, f"Отличаются учителем/кабинетом — не трогаем ({len(plan.differs)})",
                                [(p.describe(), f"на сервере: учитель {cur.get('teacher_name')}, кабинет {cur.get('room_name') or cur.get('room_id')}")
                                 for cur, p in plan.differs])
                top.addChild(QTreeWidgetItem([f"Без изменений ({plan.unchanged})", ""]))
            else:
                self._add_group(top, f"К переносу ({len(plan.items)})", [(p.describe(), p.note) for p in plan.items])
            by_reason: dict[str, list] = {}
            for l, r in plan.skipped:
                by_reason.setdefault(r.split(":")[0], []).append((l, r))
            sk = QTreeWidgetItem([f"Пропущено ({len(plan.skipped)})", "; ".join(f"{k}: {len(v)}" for k, v in
                                                                                 sorted(by_reason.items(), key=lambda x: -len(x[1])))])
            top.addChild(sk)
            for reason, lst in sorted(by_reason.items(), key=lambda x: -len(x[1])):
                node = QTreeWidgetItem([f"{reason} ({len(lst)})", ""])
                sk.addChild(node)
                for l, r in lst:
                    node.addChild(QTreeWidgetItem([f"{l.cls} {DAYS[l.day]} {l.lesson + 1}: {l.subject}"
                                                   f"{('/' + str(l.group)) if l.group else ''} — {l.teacher or '—'}", r]))
        if self.plans_online:
            lines.append(f"Итого: создать {n_create}, удалить {n_delete}, занятий в плане {n_items}.")
        else:
            lines.append(f"Итого занятий к переносу: {n_items}. Чтобы узнать, что из этого уже есть на сервере, нажмите «Сравнить с сервером».")
        self.summary.setText("\n".join(lines))
        self._fit_first_column()

    # ------------------------------------------------------------------ экспорт
    def _show(self, shown: tuple[str, object] | None):
        self.shown = shown
        self.export_btn.setEnabled(shown is not None)

    def export_shown(self):
        if self.shown is None:
            return
        kind, data = self.shown
        name = "plan" if kind == "plan" else "upload-errors"
        weeks = "-".join(str(p.week) for p in (data if kind == "plan" else [p for p, _ in data]))
        start = os.path.join(self.state.settings.value("export/dir", os.path.expanduser("~")), f"{name}-week{weeks}.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить в файл", start,
                                              "Таблица CSV для Excel (*.csv);;Текстовый отчёт (*.txt)")
        if not path:
            return
        try:
            if kind == "plan":
                export.save_plans(path, data, self.summary.text())
            else:
                export.save_results(path, data)
        except OSError as e:
            error(self, "Сохранить в файл", f"Не удалось записать файл:\n{e}")
            return
        self.state.settings.setValue("export/dir", os.path.dirname(path))
        self.app.log_text(f"{'План' if kind == 'plan' else 'Итоги загрузки'} записаны в {path}")

    def _fit_first_column(self):
        """Первая колонка по содержимому, но не шире половины дерева — остальное «Подробностям»."""
        self.tree.resizeColumnToContents(0)
        limit = max(self.tree.viewport().width() // 2, 200)
        if self.tree.columnWidth(0) > limit:
            self.tree.setColumnWidth(0, limit)

    def _add_group(self, parent: QTreeWidgetItem, title: str, rows: list[tuple[str, str]], expand: bool = False):
        node = QTreeWidgetItem([title, ""])
        parent.addChild(node)
        for a, b in rows:
            node.addChild(QTreeWidgetItem([a, b]))
        node.setExpanded(expand and bool(rows))

    # ------------------------------------------------------------------ загрузка
    def apply(self):
        st = self.state
        if not self.plans or not self.plans_online:
            error(self, "Загрузка", "Сначала выполните «Сравнить с сервером».")
            return
        n_create = sum(len(p.to_create) for p in self.plans)
        n_delete = sum(len(p.to_delete) for p in self.plans)
        n_ec = sum(1 for p in self.plans for x in p.to_create if x.kind == "ec")
        weeks = ", ".join(str(p.week) for p in self.plans)
        dates = sorted(d for p in self.plans for d in p.dates)
        text = (f"Недели {weeks}, даты {dates[0].strftime(_D)} — {dates[-1].strftime(_D)} ({len(dates)} дн.).\n\n"
                f"Создать занятий: {n_create} (уроков {n_create - n_ec}, ВД {n_ec}).\n"
                f"Удалить занятий на сервере: {n_delete}.\n\n")
        if n_delete:
            text += "ВНИМАНИЕ: занятия будут удалены с сервера безвозвратно (в журнал попадают только созданные).\n\n"
        text += "Всё созданное записывается в журнал, по нему можно откатить (вкладка «Откат»). Продолжить?"
        if not ask(self, "Загрузка в МЭШ", text, danger=bool(n_delete)):
            return
        plans = self.plans
        if st.mapping_dirty:
            # план строился по этому же сопоставлению — сохраняем, чтобы файл соответствовал загруженному
            try:
                st.save_mapping()
            except OSError as e:
                error(self, "Сопоставление", f"Не удалось сохранить сопоставление:\n{e}")
                return
        journal_path = st.paths["journal"] or "upload.journal.jsonl"
        workers = st.workers

        def job(progress, message):
            client = st.get_client_for_refdata()
            journal = Journal(journal_path)
            total = sum(len(p.to_create) + len(p.to_delete) for p in plans)
            base = 0
            results = []
            for plan in plans:
                if self.app.stop_requested():
                    break
                message(f"неделя {plan.week}…")
                res = uploader.apply_plan(client, plan, journal, progress=lambda d, t, b=base: progress(b + d, total),
                                          should_stop=self.app.stop_requested, workers=workers)
                results.append((plan, res))
                base += len(plan.to_create) + len(plan.to_delete)
            return results

        def done(results):
            created = sum(r.created for _, r in results)
            deleted = sum(r.deleted for _, r in results)
            errors = [(p.week, what, msg) for p, r in results for what, msg in r.errors]
            stopped = self.app.stop_requested()
            self.tree.clear()
            for plan, res in results:
                top = QTreeWidgetItem([f"Неделя {plan.week}", f"создано {res.created}, удалено {res.deleted}, ошибок {len(res.errors)}"
                                       + (" — остановлено, остальное не загружено" if res.stopped else "")])
                self.tree.addTopLevelItem(top)
                top.setExpanded(True)
                for what, msg in res.errors:
                    top.addChild(QTreeWidgetItem([what, msg]))
            for plan in plans[len(results):]:
                self.tree.addTopLevelItem(QTreeWidgetItem([f"Неделя {plan.week}", "не начата — остановлено"]))
            self._fit_first_column()
            self.summary.setText(f"{'Остановлено' if stopped else 'Готово'}: создано {created}, удалено {deleted}, ошибок {len(errors)}. "
                                 f"Журнал: {journal_path}. Для нового прогона снова нажмите «Сравнить с сервером»"
                                 f"{' — план покажет, что осталось' if stopped else ''}.")
            self.plans = []
            self.apply_btn.setEnabled(False)
            self._show(("result", results))
            st.changed.emit("journal")
            if errors:
                error(self, "Загрузка", f"Ошибок: {len(errors)} — см. список в плане и журнал внизу окна.")

        self.app.run(job, done, "Загрузка в МЭШ", stoppable=True)
