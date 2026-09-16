"""Вкладка «Откат»: журнал созданных занятий и удаление их с сервера."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import uploader
from ..uploader import Journal, journal_key
from .common import ask, cell, check_cell, error, filter_rows, make_table


class UndoTab(QWidget):
    COLS = ["", "Вид", "Класс", "Дата", "Урок", "Группа МЭШ", "Учитель id", "Кабинет id", "Неделя", "Id занятия", "Создано"]

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.state = app.state
        self.records: list[dict] = []
        lay = QVBoxLayout(self)
        hint = QLabel("Здесь перечислено всё, что создал этот инструмент (журнал). Отметьте записи и нажмите "
                      "«Удалить с сервера» — будут удалены ровно эти занятия; чужие занятия журнал не содержит.")
        hint.setWordWrap(True)
        lay.addWidget(hint)
        top = QHBoxLayout()
        b = QPushButton("Обновить")
        b.clicked.connect(self.reload)
        top.addWidget(b)
        self.search = QLineEdit()
        self.search.setPlaceholderText("фильтр: класс, дата, группа…")
        self.search.textChanged.connect(self.apply_filter)
        top.addWidget(self.search, 1)
        b = QPushButton("Отметить видимые")
        b.clicked.connect(lambda: self._check_visible(True))
        top.addWidget(b)
        b = QPushButton("Снять все")
        b.clicked.connect(lambda: self._check_visible(False, all_rows=True))
        top.addWidget(b)
        self.count_label = QLabel("")
        top.addWidget(self.count_label)
        lay.addLayout(top)
        self.table = make_table(self.COLS)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.itemChanged.connect(self._changed)
        lay.addWidget(self.table, 1)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)
        row = QHBoxLayout()
        self.undo_btn = QPushButton("Удалить отмеченные занятия с сервера…")
        self.undo_btn.clicked.connect(self.undo)
        row.addWidget(self.undo_btn)
        row.addStretch()
        lay.addLayout(row)
        self.state.changed.connect(self._on_changed)
        self.reload()

    def _on_changed(self, what: str):
        if what in ("paths", "journal"):
            self.reload()

    def reload(self):
        path = self.state.paths["journal"] or "upload.journal.jsonl"
        try:
            self.records = Journal(path).read()
        except (OSError, ValueError) as e:
            self.records = []
            error(self, "Журнал", f"Не удалось прочитать журнал {path}:\n{e}")
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for rec in self.records:
            r = self.table.rowCount()
            self.table.insertRow(r)
            it = check_cell(False)
            it.setData(Qt.UserRole, journal_key(rec))     # (вид, id): у уроков и ВД разные нумерации
            self.table.setItem(r, 0, it)
            vals = ["ВД" if rec.get("kind") == "ec" else "урок", rec.get("class"), rec.get("date"),
                    rec.get("begin_time") or rec.get("lesson"), rec.get("group_name"), rec.get("teacher_id"),
                    rec.get("room_id"), rec.get("week"), rec.get("item_id"), rec.get("created_at")]
            for c, v in enumerate(vals, start=1):
                self.table.setItem(r, c, cell("" if v is None else v))
        self.table.fit_columns()
        self.table.blockSignals(False)
        self.apply_filter()

    def apply_filter(self):
        filter_rows(self.table, self.search.text())
        self._update_count()

    def _changed(self, item):
        if item.column() == 0:
            self._update_count()

    def _update_count(self):
        n = sum(1 for r in range(self.table.rowCount()) if self.table.item(r, 0).checkState() == Qt.Checked)
        shown = sum(1 for r in range(self.table.rowCount()) if not self.table.isRowHidden(r))
        self.count_label.setText(f"в журнале {len(self.records)}, показано {shown}, отмечено {n}")
        self.undo_btn.setEnabled(n > 0)

    def _check_visible(self, on: bool, all_rows: bool = False):
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):
            if all_rows or not self.table.isRowHidden(r):
                self.table.item(r, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)
        self.table.blockSignals(False)
        self._update_count()

    def checked_keys(self) -> set[uploader.ItemKey]:
        return {tuple(self.table.item(r, 0).data(Qt.UserRole)) for r in range(self.table.rowCount())
                if self.table.item(r, 0).checkState() == Qt.Checked}

    def undo(self):
        st = self.state
        keys = self.checked_keys()
        if not keys:
            return
        chosen = [r for r in self.records if uploader.journal_key(r) in keys]
        weeks = sorted({r["week"] for r in chosen})
        dates = sorted({r["date"] for r in chosen})
        span = f"недели {', '.join(map(str, weeks))}; даты {dates[0]}" + (f" — {dates[-1]}" if len(dates) > 1 else "")
        if not ask(self, "Откат", f"Удалить с сервера {len(keys)} занятий, созданных этим инструментом?\n"
                                  f"{span} ({len(dates)} дн.).\n\n"
                                  "Удалённые записи будут убраны из журнала.", danger=True):
            return
        path = st.paths["journal"] or "upload.journal.jsonl"
        workers = st.workers

        def job(progress, message):
            client = st.get_client()
            return uploader.undo(client, Journal(path), items=keys, progress=progress, should_stop=self.app.stop_requested,
                                 workers=workers)

        def done(res):
            st.changed.emit("session")
            self.reload()
            msg = f"Удалено {res.deleted}, ошибок {len(res.errors)}."
            if res.stopped:
                msg = "Остановлено. " + msg + " Неудалённое осталось в журнале."
            if res.errors:
                msg += "\n\n" + "\n".join(f"{what}: {m}" for what, m in res.errors[:20])
                error(self, "Откат", msg)
            else:
                self.app.log_text(msg)

        self.app.run(job, done, "Откат", stoppable=True)
