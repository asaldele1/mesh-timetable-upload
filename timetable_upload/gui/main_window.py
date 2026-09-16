"""Главное окно: вкладки, журнал сообщений, выполнение фоновых задач."""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
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

from .common import LogHandler, Worker, error
from .mapping_tab import MappingTab
from .setup_tab import SetupTab
from .state import AppState
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

    def _initial_load(self):
        st = self.state
        try:
            st.load_mapping()
        except Exception as e:
            log.error("сопоставление %s: %s", st.paths["mapping"], e)
        if st.paths["file"] and os.path.exists(st.paths["file"]):
            try:
                st.load_file()
            except Exception as e:
                log.error("файл %s: %s", st.paths["file"], e)
        try:
            st.load_refdata()
        except Exception as e:
            log.error("кэш %s: %s", st.paths["cache"], e)

    # ------------------------------------------------------------------ служебное
    def update_title(self):
        st = self.state
        name = os.path.basename(st.paths["file"]) if st.paths["file"] else "файл не выбран"
        self.setWindowTitle(f"ХроноГраф → МЭШ — {name}" + (" *" if st.mapping_dirty else ""))
        parts = []
        parts.append(f"файл: {name}" + (f" ({len(st.tt.classes)} кл.)" if st.tt else " (не загружен)"))
        parts.append("кэш МЭШ: " + (st.rd.academic_year.get("name", "?") if st.rd else "нет"))
        parts.append("сопоставление: " + (os.path.basename(st.paths["mapping"]) or "—") + (" (не сохранено)" if st.mapping_dirty else ""))
        if st.session_short:
            parts.append(st.session_short)
        self.status_label.setText(" | ".join(parts))

    def log_text(self, text: str):
        self.log_view.appendPlainText(text)

    def run(self, fn, on_done, title: str, stoppable: bool = False):
        """Выполняет fn(progress, message) в фоне; на время задачи вкладки недоступны.
        stoppable — задача опрашивает :meth:`stop_requested` и умеет прерваться: показывается «Остановить»."""
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

    def closeEvent(self, event: QCloseEvent):
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(self, "Задача выполняется", "Дождитесь завершения текущей задачи или нажмите «Остановить».")
            event.ignore()
            return
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
