"""Вкладка «Данные и вход»: файл расписания, вход в МЭШ, загрузка справочников.

Редко нужное (токен вручную, профиль и учебный год, пути к файлам программы) убрано в окно
«Дополнительно…», чтобы основная вкладка целиком помещалась на невысоком экране."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import auth, paths, refdata
from .common import error, info, scrollable


def file_info_text(tt) -> str:
    lessons = list(tt.iter_lessons())
    bells = list(zip(tt.lesson_begin, tt.lesson_end))[:tt.lesson_count]
    lines = [
        f"Проект: {tt.title.strip()} / {tt.project_name} (версия {tt.version}, сохранён {tt.saved_at})",
        f"Школа: {tt.school_name}; учебный год {tt.teach_year} {tt.half_year_name}; период {tt.period_begin} — {tt.period_end}",
        f"Сетка: {tt.day_count} дн. × {tt.lesson_count} ур. × {tt.week_count} нед.; "
        f"звонки: {', '.join(f'{b:%H:%M}-{e:%H:%M}' for b, e in bells)}",
        f"Классов {len(tt.classes)}, учителей {len(tt.teachers)}, предметов {len(tt.subjects)}, кабинетов {len(tt.cabinets)}, "
        f"занятий в расписании {len(lessons)} (с кабинетом: {sum(1 for l in lessons if l.cabinet)})",
        "Классы: " + ", ".join(c.name for c in tt.classes),
    ]
    if tt.langs:
        lines.append("Иностранные языки: " + ", ".join(l.name for l in tt.langs))
    return "\n".join(lines)


def gray_hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setStyleSheet("color: gray")
    return lab


class PathRow(QWidget):
    """Поле пути с кнопкой «Обзор…»."""

    def __init__(self, state, key: str, mode: str, filt: str = "", on_change=None):
        super().__init__()
        self.state, self.key, self.mode, self.filt = state, key, mode, filt
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(state.paths.get(key, ""))
        self.edit.editingFinished.connect(self._commit)
        lay.addWidget(self.edit, 1)
        b = QPushButton("Обзор…")
        b.clicked.connect(self._browse)
        lay.addWidget(b)
        self.on_change = on_change
        state.changed.connect(self._sync)

    def _sync(self, what: str):
        if what == "paths" and self.edit.text().strip() != self.state.paths.get(self.key, ""):
            self.edit.setText(self.state.paths.get(self.key, ""))

    def _commit(self):
        value = self.edit.text().strip()
        if value != self.state.paths.get(self.key):
            self.state.set_path(self.key, value)
            if self.on_change:
                self.on_change()

    def _browse(self):
        cur = self.edit.text().strip() or paths.data_dir()
        if self.mode == "dir":
            path = QFileDialog.getExistingDirectory(self, "Каталог", cur)
        elif self.mode == "save":
            path, _ = QFileDialog.getSaveFileName(self, "Файл", cur, self.filt, options=QFileDialog.DontConfirmOverwrite)
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Файл", cur, self.filt)
        if path:
            self.edit.setText(path)
            self._commit()


class SetupTab(QWidget):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.state = app.state
        self.advanced: AdvancedDialog | None = None
        st = self.state
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QWidget()
        outer.addWidget(scrollable(body))
        root = QVBoxLayout(body)

        # ---- файл расписания
        files = QGroupBox("Расписание ХроноГрафа")
        form = QFormLayout(files)
        form.setVerticalSpacing(6)             # компактнее: на невысоких экранах каждая строка на счету
        self.file_row = PathRow(st, "file", "open", "Расписание ХроноГраф (*.chngf);;Все файлы (*)", self.load_file)
        form.addRow("Файл (.chngf):", self.file_row)
        self.file_info = QPlainTextEdit()
        self.file_info.setReadOnly(True)
        self.file_info.setMinimumHeight(80)
        self.file_info.setMaximumHeight(150)
        form.addRow("Сведения о файле:", self.file_info)
        root.addWidget(files)

        # ---- МЭШ
        mesh = QGroupBox("МЭШ (school.mos.ru)")
        form = QFormLayout(mesh)
        form.setVerticalSpacing(6)
        row = QHBoxLayout()
        b = QPushButton("Войти через mos.ru…")
        b.setToolTip("Открыть окно входа school.mos.ru. Токен сохраняется сам и дальше обновляется автоматически; "
                     "входить заново нужно, только если программа не запускалась больше месяца")
        b.clicked.connect(self.web_login)
        row.addWidget(b)
        b = QPushButton("Проверить вход")
        b.clicked.connect(self.check_auth)
        row.addWidget(b)
        self.auth_label = QLabel(self._auth_text())
        self.auth_label.setWordWrap(True)
        row.addWidget(self.auth_label, 1)
        form.addRow("Вход:", row)
        row = QHBoxLayout()
        b = QPushButton("Загрузить справочники из МЭШ (только чтение)")
        b.setToolTip("Классы, группы, учителя, кабинеты, звонки, недели, ВД — в кэш на диске; "
                     "повторять после изменений в МЭШ и при смене учебного года")
        b.clicked.connect(self.fetch_refdata)
        row.addWidget(b)
        self.cache_label = QLabel("")
        self.cache_label.setWordWrap(True)
        row.addWidget(self.cache_label, 1)
        form.addRow("Справочники:", row)
        root.addWidget(mesh)

        # ---- редко нужное
        row = QHBoxLayout()
        row.addWidget(gray_hint("Токен вручную, профиль сотрудника и учебный год, папка данных и пути к файлам программы:"), 1)
        b = QPushButton("Дополнительно…")
        b.clicked.connect(self.show_advanced)
        row.addWidget(b)
        root.addLayout(row)
        root.addStretch()

        st.changed.connect(self._on_changed)
        self._on_changed("file")
        self._on_changed("refdata")

    # ------------------------------------------------------------------ обработчики
    def _on_changed(self, what: str):
        if what == "file":
            self.file_info.setPlainText(file_info_text(self.state.tt) if self.state.tt else "файл не загружен")
        elif what == "refdata":
            rd = self.state.rd
            self.cache_label.setText(("в кэше: " + rd.summary()) if rd else
                                     "кэш пуст — загрузите справочники (классы, группы, учителя, кабинеты, звонки, недели, ВД)")
        elif what in ("session", "paths"):
            self.auth_label.setText(self._auth_text())

    def _auth_text(self) -> str:
        return self.state.session_text or ("вход не проверялся; " + auth.describe(self.state.paths["token_file"] or None))

    def show_advanced(self):
        if self.advanced is None:
            self.advanced = AdvancedDialog(self)
        self.advanced.exec()

    def load_file(self):
        if not self.state.paths["file"]:
            return
        try:
            self.state.load_file()
        except Exception as e:
            self.state.tt = None
            self.state.changed.emit("file")
            error(self, "Файл расписания", f"Не удалось прочитать файл:\n{e}")

    def load_mapping(self):
        try:
            self.state.load_mapping()
        except Exception as e:
            error(self, "Сопоставление", f"Не удалось прочитать файл сопоставления:\n{e}")

    def load_refdata(self):
        try:
            self.state.load_refdata()
        except Exception as e:
            self.state.rd = None
            self.state.changed.emit("refdata")
            error(self, "Кэш справочников", f"Не удалось прочитать кэш:\n{e}")

    def web_login(self):
        try:
            from . import web_login
            token, refresh_token = web_login.run_login(self)
        except ImportError as e:
            error(self, "Вход через mos.ru", f"Нужен QtWebEngine из состава PySide6 (pip install PySide6):\n{e}\n\n"
                  "Либо вставьте токен вручную: «Дополнительно…» → «Токен вручную».")
            return
        if not token:
            self.auth_label.setText("вход через mos.ru отменён")
            return
        self.state.write_token(token, refresh_token)
        self.check_auth()

    def check_auth(self):
        st = self.state

        def job(progress, message):
            client = st.get_client()
            p = client.teacher_profile()
            return (f"{st.session_text}; сотрудник {p.get('name')} (id {p.get('id')}), роли {p.get('roles')}; "
                    + auth.describe(st.paths["token_file"] or None))

        def done(text):
            st.changed.emit("session")      # обработчик ставит краткий текст — подробный поверх него
            self.auth_label.setText(text)

        self.app.run(job, done, "Проверка входа")

    def fetch_refdata(self):
        st = self.state
        cache = st.paths["cache"] or "cache"

        def job(progress, message):
            client = st.get_client()
            message("загрузка справочников…")
            return refdata.fetch(client, cache, None)

        def done(rd):
            st.changed.emit("session")
            self.load_refdata()
            info(self, "Справочники", "Загружено: " + rd.summary())

        self.app.run(job, done, "Загрузка справочников")


class AdvancedDialog(QDialog):
    """Окно «Дополнительно…»: токен вручную, профиль и учебный год, папка данных и пути к файлам программы.

    Поля пишут в состояние сразу (как и на вкладке), так что кнопка одна — «Закрыть»."""

    def __init__(self, tab: SetupTab):
        super().__init__(tab)
        self.tab = tab
        self.state = st = tab.state
        self.setWindowTitle("Дополнительно")
        self.setMinimumWidth(780)
        root = QVBoxLayout(self)

        # ---- вход
        mesh = QGroupBox("Вход в МЭШ")
        form = QFormLayout(mesh)
        form.setVerticalSpacing(6)
        row = QHBoxLayout()
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.token_edit.setPlaceholderText("значение cookie aupd_token из браузера после входа в school.mos.ru")
        row.addWidget(self.token_edit, 1)
        b = QPushButton("Сохранить в файл")
        b.clicked.connect(self.save_token)
        row.addWidget(b)
        form.addRow("Токен вручную:", row)
        form.addRow("", gray_hint("если окно входа mos.ru недоступно; такой токен живёт сутки и сам не обновляется"))
        self.token_row = PathRow(st, "token_file", "save", "Текст (*.txt);;Все файлы (*)", st.forget_client)
        form.addRow("Файл с токеном:", self.token_row)
        self.profile_edit = QLineEdit(st.paths["profile_id"])
        self.profile_edit.editingFinished.connect(lambda: (st.set_path("profile_id", self.profile_edit.text()), st.forget_client()))
        self.year_edit = QLineEdit(st.paths["academic_year"])
        self.year_edit.setPlaceholderText("текущий")
        self.year_edit.editingFinished.connect(lambda: (st.set_path("academic_year", self.year_edit.text()), st.forget_client()))
        self.week1_edit = QLineEdit(st.paths["week1_monday"])
        self.week1_edit.setPlaceholderText("ГГГГ-ММ-ДД")
        self.week1_edit.editingFinished.connect(lambda: (st.set_path("week1_monday", self.week1_edit.text()), st.changed.emit("refdata")))
        for label, edit, tip in (
            ("Id профиля сотрудника:", self.profile_edit, "только если у сотрудника несколько профилей"),
            ("Учебный год:", self.year_edit, "например 2025/26; пусто — текущий"),
            ("Понедельник 1-й учебной недели:", self.week1_edit, "ГГГГ-ММ-ДД; пусто — по началу учебного года в МЭШ"),
        ):
            edit.setToolTip(tip)
            edit.setMaximumWidth(180)
            row = QHBoxLayout()
            row.addWidget(edit)
            row.addWidget(gray_hint(tip), 1)
            form.addRow(label, row)
        row = QHBoxLayout()
        b = QPushButton("Забыть сессию mos.ru")
        b.setToolTip("Удалить cookie встроенного браузера — следующий вход потребует пароль (например, для другого аккаунта)")
        b.clicked.connect(self.forget_web_session)
        row.addWidget(b)
        row.addStretch(1)
        form.addRow("", row)
        root.addWidget(mesh)

        # ---- файлы программы
        files = QGroupBox("Файлы программы")
        form = QFormLayout(files)
        form.setVerticalSpacing(6)
        row = QHBoxLayout()
        folder = QLabel(paths.data_dir())
        folder.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(folder, 1)
        b = QPushButton("Открыть папку")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(paths.data_dir())))
        row.addWidget(b)
        form.addRow("Папка данных:", row)
        form.addRow("", gray_hint("кэш справочников, сопоставление, токены, журнал, логи, сессия браузера; "
                                  "пути ниже менять обычно не нужно"))
        self.mapping_row = PathRow(st, "mapping", "save", "JSON (*.json)", tab.load_mapping)
        form.addRow("Сопоставление (mapping.json):", self.mapping_row)
        self.cache_row = PathRow(st, "cache", "dir", "", tab.load_refdata)
        form.addRow("Кэш справочников МЭШ:", self.cache_row)
        self.journal_row = PathRow(st, "journal", "save", "JSON Lines (*.jsonl)", lambda: st.changed.emit("paths"))
        form.addRow("Журнал созданных занятий:", self.journal_row)
        row = QHBoxLayout()
        b = QPushButton("Стандартные пути")
        b.setToolTip("Вернуть пути к файлам в папке данных")
        b.clicked.connect(self.reset_paths)
        row.addWidget(b)
        row.addStretch(1)
        form.addRow("", row)
        root.addWidget(files)

        row = QHBoxLayout()
        row.addStretch(1)
        b = QPushButton("Закрыть")
        b.setDefault(True)
        b.clicked.connect(self.accept)
        row.addWidget(b)
        root.addLayout(row)

    def save_token(self):
        token = self.token_edit.text().strip()
        if not token:
            error(self, "Токен", "Вставьте токен в поле «Токен вручную».")
            return
        path = self.state.write_token(token)
        self.token_edit.clear()
        info(self, "Токен", f"Токен записан в {path}.")

    def forget_web_session(self):
        from . import web_login
        web_login.forget_session()
        info(self, "Сессия mos.ru", "Cookie встроенного браузера удалены; при следующем входе понадобится пароль.")

    def reset_paths(self):
        self.state.forget_client()
        self.state.reset_paths()          # сигнал 'paths' обновит и надпись о входе
        self.tab.load_mapping()
        self.tab.load_refdata()
