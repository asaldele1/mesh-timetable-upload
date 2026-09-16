"""Графический интерфейс (PySide6): python -m timetable_upload gui  или  timetable-upload-gui.

``timetable-upload-gui --login [файл]`` — только окно входа через mos.ru: токен записывается в файл
(по умолчанию в папку данных), окно закрывается."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .. import paths


def setup_logging() -> None:
    """Лог в файл в папке данных (в собранном exe без консоли это единственный след ошибок)."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    has_console = any(type(h) is logging.StreamHandler for h in root.handlers)   # basicConfig из cli
    fh = RotatingFileHandler(os.path.join(paths.logs_dir(), "gui.log"), maxBytes=1_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(fh)
    if sys.stderr is not None and not has_console:
        root.addHandler(logging.StreamHandler())


def main(argv: list[str] | None = None) -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as e:  # pragma: no cover
        print("Для графического интерфейса нужен PySide6: pip install 'timetable-upload[gui]'", file=sys.stderr)
        raise SystemExit(2) from e
    setup_logging()
    from . import web_login

    args = sys.argv[1:] if argv is None else list(argv)
    if args[:1] == ["--login"]:
        return web_login.main(args[1:], message_box=True)
    from .main_window import MainWindow

    web_login.prepare_application()      # окно входа через mos.ru подключает QtWebEngine уже после старта
    app = QApplication([sys.argv[0], *args])
    app.setApplicationName("timetable_upload")
    # стандартные кнопки и диалоги Qt (Да/Нет, Открыть, Отмена) — по-русски, как и всё окно
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    translator = QTranslator(app)
    if translator.load(QLocale("ru"), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(translator)
    win = MainWindow()
    win.show()
    return app.exec()
