"""Главное окно: вкладки, журнал сообщений, выполнение фоновых задач."""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QWidget,
)

from .. import refdata
from ..mesh_api import ProfileChoiceRequired
from .common import LogHandler, Worker, error, info
from .mapping_tab import MappingTab
from .setup_tab import SchoolDialog, SetupTab
from .state import AppState, SchoolChanged
from .undo_tab import UndoTab
from .upload_tab import UploadTab

log = logging.getLogger(__name__)


class LogView(QPlainTextEdit):
    """Журнал внизу окна: по умолчанию невысокий, чтобы на маленьком экране место оставалось вкладкам."""

    def sizeHint(self) -> QSize:
        return QSize(600, 120)

    def minimumSizeHint(self) -> QSize:
        return QSize(200, 60)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.state = AppState()
        self.worker: Worker | None = None
        self.refresh_worker: Worker | None = None   # обновление справочников — отдельно, окно не блокирует
        self._refresh_again = False
        self._pending_rd: refdata.RefData | None = None
        self._choosing_school = False           # окно выбора школы уже открыто (его могут запросить две задачи)
        # стартовый размер — не больше рабочей области экрана (ноутбуки с 768 px по высоте)
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1400, screen.width() - 40), min(900, screen.height() - 80))

        # журнал сообщений внизу
        dock = QDockWidget("Журнал сообщений", self)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        box = QWidget()
        bl = QHBoxLayout(box)                    # кнопка сбоку, а не под журналом: экономит высоту
        bl.setContentsMargins(2, 2, 2, 2)
        self.log_view = LogView()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        bl.addWidget(self.log_view, 1)
        b = QPushButton("Очистить")
        b.clicked.connect(self.log_view.clear)
        bl.addWidget(b, 0, Qt.AlignTop)
        dock.setWidget(box)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.log_handler = LogHandler()
        self.log_handler.emitter.text.connect(self.log_view.appendPlainText)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.INFO)

        # строка состояния
        self.status_label = QLabel("")
        self.statusBar().addWidget(self.status_label, 1)
        self.refresh_label = QLabel("")
        self.statusBar().addPermanentWidget(self.refresh_label)
        self.task_label = QLabel("")
        self.statusBar().addPermanentWidget(self.task_label)
        self.task_progress = QProgressBar()
        self.task_progress.setMaximumWidth(220)
        self.task_progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.task_progress)
        self.stop_btn = QPushButton("Остановить")
        self.stop_btn.setToolTip("Прервать задачу после текущего запроса; сделанное остаётся (и в журнале отката)")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self.stop_task)
        self.statusBar().addPermanentWidget(self.stop_btn)

        # начальная загрузка локальных данных (файл, кэш, сопоставление)
        self._initial_load()

        self.tabs = QTabWidget()
        self.setup_tab = SetupTab(self)
        self.mapping_tab = MappingTab(self)
        self.upload_tab = UploadTab(self)
        self.undo_tab = UndoTab(self)
        self.tabs.addTab(self.setup_tab, "1. Данные и вход")
        self.tabs.addTab(self.mapping_tab, "2. Сопоставление")
        self.tabs.addTab(self.upload_tab, "3. Перенос в МЭШ")
        self.tabs.addTab(self.undo_tab, "4. Откат")
        self.setCentralWidget(self.tabs)
        self.state.changed.connect(lambda what: self.update_title())
        self.update_title()
        # справочники — с сервера при каждом запуске (в фоне; без входа — остаётся кэш, сообщение в журнале)
        QTimer.singleShot(0, self.start_refresh)

    def _initial_load(self):
        st = self.state
        try:
            st.load_mapping()
        except Exception as e:
            log.error("сопоставление %s: %s", st.mapping_path, e)
        if st.paths["file"] and os.path.exists(st.paths["file"]):
            try:
                st.load_file()
            except Exception as e:
                log.error("файл %s: %s", st.paths["file"], e)
        try:
            st.load_refdata()
        except Exception as e:
            log.error("кэш %s: %s", st.cache_path, e)

    # ------------------------------------------------------------------ служебное
    def update_title(self):
        st = self.state
        name = os.path.basename(st.paths["file"]) if st.paths["file"] else "файл не выбран"
        self.setWindowTitle(f"ХроноГраф → МЭШ — {name}" + (" *" if st.mapping_dirty else ""))
        parts = []
        parts.append(f"файл: {name}" + (f" ({len(st.tt.classes)} кл.)" if st.tt else " (не загружен)"))
        parts.append("кэш МЭШ: " + (st.rd.academic_year.get("name", "?") if st.rd else "нет"))
        parts.append("сопоставление: " + (os.path.basename(st.mapping_path) or "—") + (" (не сохранено)" if st.mapping_dirty else ""))
        if st.session_short:
            parts.append(st.session_short)
        elif st.school.label:
            parts.append("школа: " + st.school.label)
        self.status_label.setText(" | ".join(parts))

    def log_text(self, text: str):
        self.log_view.appendPlainText(text)

    def run(self, fn, on_done, title: str, stoppable: bool = False, retry=None):
        """Выполняет fn(progress, message) в фоне; на время задачи вкладки недоступны.
        stoppable — задача опрашивает :meth:`stop_requested` и умеет прерваться: показывается «Остановить».
        Вход потребовал выбрать или сменить школу — после выбора вызывается retry(), без него пользователя
        просят повторить действие (задача могла запомнить пути файлов прежней школы)."""
        if self.worker is not None and self.worker.isRunning():
            error(self, title, "Дождитесь завершения текущей задачи.")
            return
        self.tabs.setEnabled(False)
        self.task_label.setText(title + "…")
        self.task_progress.setRange(0, 0)
        self.task_progress.setVisible(True)
        self.stop_btn.setEnabled(True)
        self.stop_btn.setVisible(stoppable)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        log.info("%s: начало", title)
        w = Worker(fn, self)
        self.worker = w

        def finish():
            QApplication.restoreOverrideCursor()
            self.tabs.setEnabled(True)
            self.task_label.setText("")
            self.task_progress.setVisible(False)
            self.stop_btn.setVisible(False)
            if self._pending_rd is not None:      # справочники пришли, пока шла задача, — подменяем теперь
                rd, self._pending_rd = self._pending_rd, None
                self.state.set_refdata(rd)
            self.update_title()

        def ok(result):
            finish()
            log.info("%s: готово", title)
            try:
                on_done(result)
            except Exception as e:
                log.exception("ошибка обработки результата")
                error(self, title, str(e))

        def failed(msg):
            finish()
            if isinstance(w.exc, (ProfileChoiceRequired, SchoolChanged)):
                if self.school_from_error(w.exc):
                    if retry:
                        retry()
                    else:
                        info(self, title, f"Школа: {self.state.school.label}. Повторите действие.")
                else:
                    error(self, title, "Школа не выбрана.")
                return
            log.error("%s: %s", title, msg)
            error(self, title, msg)

        def progress(done, total):
            if total:
                self.task_progress.setRange(0, total)
                self.task_progress.setValue(done)

        w.done.connect(ok)
        w.failed.connect(failed)
        w.progress.connect(progress)
        w.message.connect(lambda t: self.task_label.setText(f"{title}: {t}"))
        w.start()

    def stop_task(self):
        """Просит текущую задачу остановиться (она прервётся после текущего запроса к серверу)."""
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel.set()
            self.stop_btn.setEnabled(False)
            self.task_label.setText(self.task_label.text().split(":")[0].rstrip("…") + ": остановка…")
            log.info("остановка по запросу пользователя — ждём завершения текущего запроса")

    def stop_requested(self) -> bool:
        """Для фоновых задач: нажата ли «Остановить» (можно вызывать из рабочего потока)."""
        w = self.worker
        return w is not None and w.cancel.is_set()

    # ------------------------------------------------------------------ справочники с сервера
    def start_refresh(self, manual: bool = False):
        """Перезагрузить справочники с сервера в фоне, не блокируя окно: пока идёт, недоступны только
        части, которым нужен список групп (сопоставление предметов и групп, план, загрузка), остальное
        работает по прежнему кэшу.  Сменился вход, пока шла загрузка (refresh_gen), — результат отбрасывается
        и загрузка повторяется.  manual — по кнопке: об ошибке сообщаем окном, иначе — только в журнале."""
        st = self.state
        if self.refresh_worker is not None and self.refresh_worker.isRunning():
            self._refresh_again = True
            return
        cache = st.cache_path
        gen = st.refresh_gen

        def job(progress, message):
            client = st.get_client()
            return refdata.fetch(client, cache, None, message)

        w = Worker(job, self)
        self.refresh_worker = w
        st.set_refreshing(True)
        self.refresh_label.setText("справочники: загрузка с сервера…")
        log.info("обновление справочников с сервера: начало")

        def ok(rd):
            if gen != st.refresh_gen:
                log.info("справочники загружены для прежнего входа — отброшены, загружаем заново")
                self._refresh_again = True
                return
            if self.worker is not None and self.worker.isRunning():
                self._pending_rd = rd            # идёт другая задача со старыми справочниками — подменим после неё
            else:
                st.set_refdata(rd)
            st.changed.emit("session")
            if manual:
                info(self, "Справочники", "Загружено: " + rd.summary())

        def failed(msg):
            if isinstance(w.exc, (ProfileChoiceRequired, SchoolChanged)):
                if self.school_from_error(w.exc):
                    self._refresh_again = True
                    return
                msg = "школа не выбрана (вкладка «Данные и вход» → «Проверить вход»)"
            log.error("обновление справочников: %s", msg)
            st.refresh_error = msg
            if manual:
                error(self, "Загрузка справочников", msg)

        def thread_done():
            again, self._refresh_again = self._refresh_again or gen != st.refresh_gen, False
            self.refresh_label.setText("")
            if again:
                self.start_refresh()
            else:
                st.set_refreshing(False)

        w.done.connect(ok)
        w.failed.connect(failed)
        w.message.connect(lambda t: self.refresh_label.setText(f"справочники: {t}"))
        w.finished.connect(thread_done)          # после done/failed: поток действительно завершён
        w.start()

    # ------------------------------------------------------------------ школа
    def school_from_error(self, exc) -> bool:
        """Вход не состоялся из-за школы: несколько школ без выбора — окно выбора; вход в другую школу
        (первый вход) — переключиться на неё.  False — пользователь отказался выбирать."""
        if isinstance(exc, SchoolChanged):
            return self.switch_school(exc.profile, exc.profiles)
        reason = ("Прежде выбранная школа недоступна. Выберите школу:" if self.state.school.profile_id is not None
                  else "")
        return self.pick_school(exc.profiles, reason)

    def pick_school(self, profiles: list[dict], reason: str = "") -> bool:
        """Окно выбора школы; выбранная становится текущей.  False — отменено."""
        if self._choosing_school or not profiles:
            return False
        self._choosing_school = True
        try:
            dlg = SchoolDialog(self, profiles, self.state.school.profile_id, reason)
            return dlg.exec() == QDialog.Accepted and self.switch_school(dlg.chosen(), profiles)
        finally:
            self._choosing_school = False

    def switch_school(self, profile: dict, profiles: list[dict]) -> bool:
        """Сделать школу текущей (см. AppState.choose_school).  Сопоставление перечитается из папки другой
        школы — несохранённое сперва сохраняем: при первом входе молча (файл переедет в папку школы),
        при смене школы — спросив.  False — пользователь отменил."""
        st = self.state
        sid = profile.get("school_id")
        if st.mapping_dirty and (int(sid) if sid is not None else None) != st.school_id:
            save = True
            if st.school_id is not None:
                r = QMessageBox.question(self, "Несохранённое сопоставление",
                                         f"Сопоставление школы {st.school.label} не сохранено. Сохранить перед сменой школы?",
                                         QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
                if r == QMessageBox.Cancel:
                    return False
                save = r == QMessageBox.Save
            if save:
                try:
                    st.save_mapping()
                except OSError as e:
                    error(self, "Сопоставление", str(e))
                    return False
        changed = st.school_id != (int(sid) if sid is not None else None)
        st.choose_school(profile, profiles)
        if changed:
            self.start_refresh()
        return True

    def closeEvent(self, event: QCloseEvent):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "Задача выполняется", "Дождитесь завершения текущей задачи или нажмите «Остановить».")
            event.ignore()
            return
        if self.refresh_worker is not None and self.refresh_worker.isRunning():
            # прервать запрос нельзя — дожидаемся (несколько секунд), иначе поток убьётся вместе с окном
            QApplication.setOverrideCursor(Qt.BusyCursor)
            self.refresh_worker.wait(60000)
            QApplication.restoreOverrideCursor()
        if self.state.mapping_dirty:
            r = QMessageBox.question(self, "Несохранённое сопоставление", "Сохранить изменения сопоставления перед выходом?",
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
            if r == QMessageBox.Cancel:
                event.ignore()
                return
            if r == QMessageBox.Save:
                try:
                    self.state.save_mapping()
                except OSError as e:
                    error(self, "Сопоставление", str(e))
                    event.ignore()
                    return
        logging.getLogger().removeHandler(self.log_handler)
        event.accept()
