"""Вход в school.mos.ru через встроенный браузер (QtWebEngine).

Открывается страница входа school.mos.ru → login.mos.ru; пользователь входит как обычно
(пароль, код из СМС и т. п.).  После возврата на school.mos.ru сервер ставит cookie
``aupd_token`` и ``aupd_refresh_token`` (см. ``/v3/auth/sudir/callback``) — они перехватываются,
окно закрывается, токены записываются в файлы (см. :mod:`..auth`).  Cookie mos.ru сохраняются в
папке данных (``web/``), поэтому повторный вход в течение сессии mos.ru проходит без ввода пароля.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys

from PySide6.QtCore import QCoreApplication, Qt, QTimer, QUrl
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .. import auth, paths
from ..auth import token_is_fresh

log = logging.getLogger(__name__)

LOGIN_URL = "https://school.mos.ru/v3/auth/sudir/login"
TOKEN_COOKIE = auth.TOKEN_COOKIE
REFRESH_COOKIE = auth.REFRESH_COOKIE
COOKIE_DOMAIN = "mos.ru"
# QtWebEngine представляется как «QtWebEngine/6.x»; mos.ru рассчитан на обычные браузеры
USER_AGENT = auth.USER_AGENT

_profile = None      # QWebEngineProfile на всё время работы программы (создаётся при первом входе)


def prepare_application() -> None:
    """Вызывать до создания QApplication: иначе QtWebEngine нельзя подключить позже."""
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)


def profile():
    """Профиль браузера с постоянными cookie в папке данных (создаётся один раз)."""
    global _profile
    if _profile is None:
        from PySide6.QtWebEngineCore import QWebEngineProfile

        storage = paths.web_dir()
        _profile = QWebEngineProfile(paths.APP_NAME, QApplication.instance())
        _profile.setPersistentStoragePath(storage)
        _profile.setCachePath(os.path.join(storage, "cache"))
        _profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        _profile.setHttpUserAgent(USER_AGENT)
    return _profile


def forget_session() -> None:
    """Удалить cookie и кэш браузера (следующий вход — с вводом пароля)."""
    if _profile is not None:
        _profile.cookieStore().deleteAllCookies()
        _profile.clearHttpCache()
    else:
        shutil.rmtree(paths.web_dir(), ignore_errors=True)


class WebLoginDialog(QDialog):
    """Окно входа; после появления cookie с токеном закрывается с результатом Accepted.

    ``token`` — полученный токен ('' при отмене), ``refresh_token`` — refresh-токен, если сервер его
    поставил (cookie HttpOnly, хранилищу браузера она видна).  Параметры url/cookie_domain — для тестов."""

    def __init__(self, parent=None, url: str = LOGIN_URL, cookie_name: str = TOKEN_COOKIE,
                 cookie_domain: str = COOKIE_DOMAIN):
        super().__init__(parent)
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self.token = ""
        self.refresh_token = ""
        self.url, self.cookie_name, self.cookie_domain = url, cookie_name, cookie_domain
        self._finished = False
        self.setWindowTitle("Вход в school.mos.ru")
        self.resize(1000, 800)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.page = QWebEnginePage(profile(), self.view)
        self.view.setPage(self.page)
        lay.addWidget(self.view, 1)
        bar = QHBoxLayout()
        bar.setContentsMargins(8, 4, 8, 4)
        self.status = QLabel("Войдите в mos.ru — после входа окно закроется само.")
        bar.addWidget(self.status, 1)
        b = QPushButton("Отмена")
        b.clicked.connect(self.reject)
        bar.addWidget(b)
        lay.addLayout(bar)

        self.store = profile().cookieStore()
        self.store.cookieAdded.connect(self._cookie_added)
        self.view.loadFinished.connect(self._load_finished)
        self.page.certificateError.connect(self._certificate_error)
        self.view.load(QUrl(url))

    def _certificate_error(self, err) -> None:
        # в журнал — чтобы было видно, если страница входа не открывается из-за сертификата
        log.warning("сертификат %s: %s", err.url().toString(), err.description())
        err.rejectCertificate()

    def _cookie_added(self, cookie) -> None:
        name = bytes(cookie.name()).decode("latin-1")
        if name not in (self.cookie_name, REFRESH_COOKIE):
            return
        domain = cookie.domain().lstrip(".")
        if domain != self.cookie_domain and not domain.endswith("." + self.cookie_domain):
            return
        value = bytes(cookie.value()).decode("latin-1").strip()
        if name == REFRESH_COOKIE:
            if token_is_fresh(value):
                self.refresh_token = value
                if self.token:
                    QTimer.singleShot(0, self.accept)
        elif not self.token:
            self._accept(value)

    def _load_finished(self, ok: bool) -> None:
        # Если сервер пустил по сохранённой сессии, не выставляя cookie заново, сигнала cookieAdded не будет
        # (сохранённые cookie хранилище не объявляет): вернувшись на school.mos.ru, читаем document.cookie —
        # aupd_token ставится без HttpOnly.
        if self.token:
            return
        url = self.view.url()
        log.debug("страница %s: %s", "загружена" if ok else "не загружена", url.toString())
        host = url.host().lower()
        if (host == self.cookie_domain or host.endswith("." + self.cookie_domain)) and url.toString() != self.url:
            self.page.runJavaScript("document.cookie", 0, self._document_cookie)

    def _document_cookie(self, text) -> None:
        if self.token or not isinstance(text, str):
            return
        for part in text.split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.cookie_name:
                self._accept(value)

    def _accept(self, value: str) -> None:
        value = value.strip()
        if self.token or not token_is_fresh(value):
            return
        self.token = value
        self.status.setText("Вход выполнен.")
        log.info("вход через mos.ru выполнен, токен получен")
        # cookie одного ответа приходят по очереди: даём refresh-токену секунду, чтобы дойти
        QTimer.singleShot(0 if self.refresh_token else 1000, self.accept)

    def done(self, result: int) -> None:
        if not self._finished:
            self._finished = True
            self.store.cookieAdded.disconnect(self._cookie_added)
            self.view.stop()
            super().done(result)


def run_login(parent=None, url: str = LOGIN_URL) -> tuple[str, str]:
    """Показать окно входа модально; вернуть (токен, refresh-токен) — пустые строки при отмене."""
    dlg = WebLoginDialog(parent, url=url)
    dlg.exec()
    token, refresh_token = dlg.token, dlg.refresh_token
    dlg.deleteLater()
    return token, refresh_token


def main(argv: list[str] | None = None, message_box: bool = False) -> int:
    """Отдельный запуск (``login`` в командной строке, ``timetable-upload-gui --login``): окно входа,
    токен — в файл argv[0] или стандартный.  message_box — сообщить о результате окном, а не в консоль."""
    args = sys.argv[1:] if argv is None else argv
    token_path = args[0] if args else paths.token_file()
    prepare_application()
    app = QApplication.instance() or QApplication([sys.argv[0]])
    token, refresh_token = run_login()
    app.processEvents()
    if token:
        auth.save_tokens(token, refresh_token, token_path)
    text = (f"Токен записан в {token_path}" + ("" if refresh_token else " (без refresh-токена)")) if token \
        else "Вход не выполнен."
    if message_box:
        from PySide6.QtWidgets import QMessageBox
        (QMessageBox.information if token else QMessageBox.warning)(None, "Вход в school.mos.ru", text)
    else:
        print(text, file=None if token else sys.stderr)
    return 0 if token else 1
