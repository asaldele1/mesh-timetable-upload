"""Окно входа через встроенный браузер: перехват cookie с токеном (на локальном HTTP-сервере)."""

import base64
import http.server
import json
import os
import threading
import time

import pytest

pytest.importorskip("PySide6.QtWebEngineWidgets")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from timetable_upload import paths  # noqa: E402
from timetable_upload.gui import web_login  # noqa: E402


def jwt(exp: float) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"sub": "1", "exp": int(exp)}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


FRESH = jwt(time.time() + 86400)
STALE = jwt(time.time() - 10)
REFRESH = jwt(time.time() + 86400 * 30)


class Handler(http.server.BaseHTTPRequestHandler):
    """/login — как school.mos.ru: ставит cookie и перебрасывает на /home; /stale — то же с просроченным
    токеном; /start — переброс на /home без cookie; /home — страница без cookie."""

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if self.path == "/login":
            self.send_header("Set-Cookie", f"aupd_token={FRESH}; Max-Age=86400; Path=/")
            self.send_header("Set-Cookie", f"aupd_refresh_token={REFRESH}; Max-Age=2592000; Path=/v3/token/refresh; HttpOnly")
            self.send_header("Set-Cookie", "user_login=x; Path=/")
        elif self.path == "/stale":
            self.send_header("Set-Cookie", f"aupd_token={STALE}; Max-Age=86400; Path=/")
        self.end_headers()
        body = '<html><head><meta http-equiv="refresh" content="0;url=/home"></head><body>ok</body></html>'
        self.wfile.write((body if self.path != "/home" else "<html><body>home</body></html>").encode())


@pytest.fixture(scope="module")
def server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    os.environ[paths.HOME_ENV] = str(tmp_path_factory.mktemp("data"))
    web_login.prepare_application()
    return QApplication.instance() or QApplication([])


def _run(app, dlg, timeout_ms=15000):
    QTimer.singleShot(timeout_ms, dlg.reject)
    return dlg.exec()


def test_login_dialog_catches_token(app, server):
    dlg = web_login.WebLoginDialog(url=server + "/login", cookie_domain="127.0.0.1")
    assert _run(app, dlg) == QDialog.Accepted
    assert dlg.token == FRESH
    assert dlg.refresh_token == REFRESH          # HttpOnly, но хранилищу браузера видна
    assert paths.web_dir().startswith(os.environ[paths.HOME_ENV])


def test_existing_session_cookie_is_reused(app, server):
    # cookie уже лежит в профиле после первого теста; /start → /home её заново не ставят
    dlg = web_login.WebLoginDialog(url=server + "/start", cookie_domain="127.0.0.1")
    assert _run(app, dlg) == QDialog.Accepted
    assert dlg.token == FRESH and dlg.refresh_token == ""      # через document.cookie HttpOnly не видна


def test_stale_cookie_is_ignored(app, server):
    dlg = web_login.WebLoginDialog(url=server + "/stale", cookie_domain="127.0.0.1")
    assert _run(app, dlg, timeout_ms=3000) == QDialog.Rejected
    assert dlg.token == ""


def test_forget_session(app, server):
    web_login.forget_session()
    app.processEvents()
    dlg = web_login.WebLoginDialog(url=server + "/start", cookie_domain="127.0.0.1")
    assert _run(app, dlg, timeout_ms=3000) == QDialog.Rejected
