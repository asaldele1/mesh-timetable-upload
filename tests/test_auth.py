"""Токены: срок действия, обновление по refresh-токену, поведение клиента при 401."""

import base64
import json
import time

import pytest

from timetable_upload import auth, paths
from timetable_upload.mesh_api import MeshClient, MeshError


def jwt(exp: float, **extra) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"sub": "1", "exp": int(exp), **extra}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


class FakeResponse:
    def __init__(self, status, body, cookies=None):
        self.status_code, self._body, self.reason = status, body, "reason"
        self.cookies = cookies or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeHttp:
    """Заглушка requests.Session для auth.refresh: запоминает вызовы, отдаёт заготовленные ответы."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.HOME_ENV, str(tmp_path))
    return tmp_path


def test_expiry_helpers():
    fresh, stale = jwt(time.time() + 3600), jwt(time.time() - 10)
    assert auth.token_is_fresh(fresh) and not auth.token_is_fresh(stale)
    assert auth.expires_at("not-a-jwt") is None and auth.jwt_payload("a.b.c") is None


def test_save_and_current_token_without_refresh(data_dir):
    token = jwt(time.time() + 3600)
    path = auth.save_tokens(token)
    assert path == paths.token_file() and auth.current_token() == token
    assert "нет" in auth.describe()          # refresh-токена нет
    # истёкший токен без refresh-токена — понятная ошибка
    auth.save_tokens(jwt(time.time() - 10))
    with pytest.raises(auth.AuthError, match="войдите заново"):
        auth.current_token()
    # нет ничего
    (data_dir / paths.TOKEN_FILE).unlink()
    with pytest.raises(auth.AuthError, match="нет токена"):
        auth.current_token()


def test_refresh_rotates_tokens(data_dir):
    old_refresh, new_refresh = jwt(time.time() + 86400 * 30, jti="r1"), jwt(time.time() + 86400 * 30, jti="r2")
    new_token = jwt(time.time() + 86400)
    auth.save_tokens(jwt(time.time() + 60), old_refresh)          # токену осталась минута
    http = FakeHttp(FakeResponse(201, {"access_token": new_token}, {auth.REFRESH_COOKIE: new_refresh}))
    assert auth.current_token(http=http) == new_token
    url, kw = http.calls[0]
    assert url.endswith(auth.REFRESH_PATH) and kw["cookies"] == {auth.REFRESH_COOKIE: old_refresh} and kw["json"] == {}
    # файлы переписаны новым токеном и новым refresh-токеном
    assert (data_dir / paths.TOKEN_FILE).read_text().strip() == new_token
    assert (data_dir / paths.REFRESH_FILE).read_text().strip() == new_refresh
    # свежий токен без force повторно не обновляется
    assert auth.current_token(http=FakeHttp()) == new_token
    # force — обновляется; refresh-токен из тела ответа тоже принимается
    newer = jwt(time.time() + 86400, jti="a2")
    http = FakeHttp(FakeResponse(201, {"access_token": newer, "refresh_token": "x.y.z"}))
    assert auth.current_token(force=True, http=http) == newer
    assert (data_dir / paths.REFRESH_FILE).read_text().strip() == "x.y.z"


def test_refresh_revoked(data_dir):
    auth.save_tokens(jwt(time.time() - 10), jwt(time.time() + 86400))
    http = FakeHttp(FakeResponse(403, {"msg": "token_revoked"}))
    with pytest.raises(auth.AuthError, match="token_revoked"):
        auth.current_token(http=http)


def test_client_refreshes_on_401(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body, self.content, self.reason = status, body, b"x", "r"

        def json(self):
            return self._body

    def fake_request(method, url, params=None, json=None, timeout=None):
        calls.append(client.http.headers["Authorization"])
        return Resp(401, {"message": "expired"}) if len(calls) == 1 else Resp(200, {"ok": True})

    client = MeshClient("old", delay=0, refresh=lambda: "new")
    monkeypatch.setattr(client.http, "request", fake_request)
    assert client.get("/x") == {"ok": True}
    assert calls == ["Bearer old", "Bearer new"] and client.token == "new"
    # повторный 401 после обновления — уже ошибка, без зацикливания
    calls.clear()
    monkeypatch.setattr(client.http, "request", lambda *a, **k: Resp(401, {"message": "still"}))
    with pytest.raises(MeshError):
        client.get("/x")
    # без refresh — сразу ошибка
    plain = MeshClient("t", delay=0)
    monkeypatch.setattr(plain.http, "request", lambda *a, **k: Resp(401, {"message": "no"}))
    with pytest.raises(MeshError):
        plain.get("/x")


def test_sessions_body_uses_refreshed_token(monkeypatch):
    """Тело /sessions несёт токен: после 401 повтор идёт не только с новым заголовком, но и с новым телом."""
    bodies = []

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body, self.content, self.reason = status, body, b"x", "r"

        def json(self):
            return self._body

    def fake_request(method, url, params=None, json=None, timeout=None):
        bodies.append(json)
        return Resp(401, {"message": "expired"}) if len(bodies) == 1 else Resp(200, {"profiles": []})

    client = MeshClient("old", delay=0, refresh=lambda: "new")
    monkeypatch.setattr(client.http, "request", fake_request)
    assert client.sessions() == {"profiles": []}
    assert bodies == [{"auth_token": "old"}, {"auth_token": "new"}]


def test_retry_on_429_is_logged(monkeypatch, caplog):
    """429/502/503/504 повторяются с паузой, каждый повтор — предупреждение в журнале; после исчерпания — MeshError."""
    from timetable_upload import mesh_api

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body, self.content, self.reason = status, body, b"x", "r"

        def json(self):
            return self._body

    pauses = []
    monkeypatch.setattr(mesh_api.time, "sleep", pauses.append)
    n = 0

    def fake_request(method, url, params=None, json=None, timeout=None):
        nonlocal n
        n += 1
        return Resp(429, {"message": "Too Many Requests"}) if n < 3 else Resp(200, {"ok": True})

    client = MeshClient("t", delay=0)
    monkeypatch.setattr(client.http, "request", fake_request)
    with caplog.at_level("WARNING", logger="timetable_upload.mesh_api"):
        assert client.get("/x") == {"ok": True}
    assert pauses == [2.0, 4.0]
    assert [("429" in r.message and "1 из 3" in r.message, "2 из 3" in r.message) for r in caplog.records] == [(True, False), (False, True)]
    # все повторы исчерпаны — ошибка с кодом сервера
    n = -10
    with pytest.raises(MeshError) as e:
        client.get("/x")
    assert e.value.status == 429 and pauses == [2.0, 4.0, 2.0, 4.0, 6.0]


def test_get_pages_parallel_and_fallback():
    """Первая страница даёт Pages в заголовках — остальные запрашиваются сразу (параллельно) и
    склеиваются по порядку; без заголовка — страница за страницей, пока не придёт неполная."""
    from timetable_upload.mesh_api import MeshClient

    class Fake(MeshClient):
        def __init__(self, items, per_page, headers=True):
            super().__init__("t")
            self.items, self.pp, self.hdr, self.calls = items, per_page, headers, []

        def request(self, method, path, params=None, json_body=None, _retry_auth=True, with_headers=False):
            page = params["page"]
            self.calls.append(page)
            chunk = self.items[(page - 1) * self.pp: page * self.pp]
            pages = -(-len(self.items) // self.pp)
            h = {"Page": str(page), "Pages": str(pages), "PageSize": str(self.pp)} if self.hdr else {}
            return (chunk, h) if with_headers else chunk

    items = list(range(1, 701))
    c = Fake(items, 300)
    assert c.get_pages("/x", per_page=300) == items and sorted(c.calls) == [1, 2, 3]
    c = Fake(items[:300], 300)                       # ровно одна страница
    assert c.get_pages("/x", per_page=300) == items[:300] and c.calls == [1]
    c = Fake([], 300)
    assert c.get_pages("/x", per_page=300) == [] and c.calls == [1]
    c = Fake(items, 300, headers=False)              # заголовков нет — последовательно
    assert c.get_pages("/x", per_page=300) == items and c.calls == [1, 2, 3]
    c = Fake(items[:600], 300, headers=False)        # последняя страница полная — ещё один пустой запрос
    assert c.get_pages("/x", per_page=300) == items[:600] and c.calls == [1, 2, 3]
