"""Токены school.mos.ru: хранение, срок действия, обновление по refresh-токену.

Вход (``/v3/auth/sudir/callback``) даёт две cookie: ``aupd_token`` — JWT на сутки, которым
подписываются запросы к API, и ``aupd_refresh_token`` — JWT на 30 дней (HttpOnly, только для
``POST /v3/token/refresh``).  Обновление: ``POST https://school.mos.ru/v3/token/refresh`` с cookie
``aupd_refresh_token`` и телом ``{}`` → ``201 {"access_token": …}`` плюс новые cookie.  Refresh-токен
одноразовый: после обновления сервер отдаёт новый, старый отвечает ``403 token_revoked``, поэтому
новый нужно сразу записать.

Файлы в папке данных: ``aupd_token.txt`` (можно положить и вручную) и ``aupd_refresh_token.txt``
(пишет окно входа).  :func:`current_token` отдаёт годный токен, при необходимости обновив его.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time

import requests

from . import paths

log = logging.getLogger(__name__)

TOKEN_ENV = "MESH_TOKEN"
REFRESH_PATH = "/v3/token/refresh"
REFRESH_COOKIE = "aupd_refresh_token"
TOKEN_COOKIE = "aupd_token"
REFRESH_MARGIN = 15 * 60          # обновлять, если токену осталось меньше 15 минут
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36")


class AuthError(ValueError):
    """Нет годного токена; в тексте — что делать."""


def jwt_payload(token: str) -> dict | None:
    """Полезная нагрузка JWT без проверки подписи; None, если это не JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def expires_at(token: str) -> float | None:
    """Момент истечения JWT (unix time) либо None, если не JWT или без exp."""
    payload = jwt_payload(token)
    exp = payload.get("exp") if payload else None
    return float(exp) if isinstance(exp, (int, float)) else None


def token_is_fresh(token: str, now: float | None = None, margin: int = 60) -> bool:
    """JWT с неистёкшим сроком действия — хотя бы на margin секунд вперёд."""
    exp = expires_at(token)
    return exp is not None and exp > (time.time() if now is None else now) + margin


def refresh_file_for(token_file: str) -> str:
    """Файл refresh-токена — рядом с файлом токена."""
    return os.path.join(os.path.dirname(os.path.abspath(token_file)), paths.REFRESH_FILE)


def _read(path: str) -> str:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def _write(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(value.strip() + "\n")


def save_tokens(token: str, refresh_token: str = "", token_file: str | None = None) -> str:
    """Записать токен (и refresh-токен, если есть) в файлы; возвращает путь к файлу токена."""
    token_file = token_file or paths.token_file()
    _write(token_file, token)
    if refresh_token:
        _write(refresh_file_for(token_file), refresh_token)
    return token_file


def refresh(refresh_token: str, base_url: str = "https://school.mos.ru",
            http: requests.Session | None = None) -> tuple[str, str]:
    """Обновить токен по refresh-токену → (новый токен, новый refresh-токен или '' если сервер его не вернул)."""
    http = http or requests.Session()
    resp = http.post(base_url + REFRESH_PATH, json={}, cookies={REFRESH_COOKIE: refresh_token},
                     headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*",
                              "Origin": base_url, "Referer": base_url + "/"},
                     timeout=60)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or not isinstance(body, dict) or not body.get("access_token"):
        msg = body.get("msg") or body.get("message") if isinstance(body, dict) else resp.text[:200]
        raise AuthError(f"обновить токен не удалось (HTTP {resp.status_code}: {msg or resp.reason}) — "
                        f"войдите заново: «Войти через mos.ru» или команда login")
    new_refresh = resp.cookies.get(REFRESH_COOKIE) or body.get("refresh_token") or ""
    return body["access_token"], new_refresh


def current_token(token_file: str | None = None, force: bool = False, margin: int = REFRESH_MARGIN,
                  http: requests.Session | None = None) -> str:
    """Годный токен: из файла (или переменной MESH_TOKEN); если он истекает и есть refresh-токен —
    обновляется и переписывается вместе с новым refresh-токеном.  force — обновить в любом случае
    (например, после ответа 401)."""
    token_file = token_file or paths.token_file()
    token = _read(token_file) or os.environ.get(TOKEN_ENV, "").strip()
    refresh_token = _read(refresh_file_for(token_file))
    exp = expires_at(token) if token else None
    stale = force or not token or (exp is not None and exp < time.time() + margin)
    if not stale:
        return token
    if not refresh_token:
        if not token:
            raise AuthError(f"нет токена: «Войти через mos.ru» / команда login, либо положите aupd_token в файл "
                            f"{token_file} или в переменную {TOKEN_ENV}")
        if exp is not None and exp < time.time():
            raise AuthError("токен истёк, refresh-токена нет — войдите заново: «Войти через mos.ru» или команда login")
        return token                  # истекает скоро, но обновить нечем — пробуем как есть
    log.info("токен %s — обновляем по refresh-токену", "истёк" if exp is None or exp < time.time() else "скоро истечёт")
    token, new_refresh = refresh(refresh_token, http=http)
    save_tokens(token, new_refresh, token_file)
    if not new_refresh:
        log.warning("сервер не вернул новый refresh-токен; следующий вход, возможно, потребует пароля")
    return token


def describe(token_file: str | None = None) -> str:
    """Строка о сроках действия токена и refresh-токена (для check-auth и окна)."""
    token_file = token_file or paths.token_file()
    parts = []
    for label, value in (("токен", _read(token_file) or os.environ.get(TOKEN_ENV, "")),
                         ("refresh-токен", _read(refresh_file_for(token_file)))):
        exp = expires_at(value) if value else None
        if not value:
            parts.append(f"{label}: нет")
        elif exp is None:
            parts.append(f"{label}: есть (срок неизвестен)")
        else:
            parts.append(f"{label} до {time.strftime('%d.%m.%Y %H:%M', time.localtime(exp))}"
                         + (" (истёк)" if exp < time.time() else ""))
    return "; ".join(parts)
