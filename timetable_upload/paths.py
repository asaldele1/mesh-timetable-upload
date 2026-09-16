"""Папка данных программы: кэш справочников, сопоставление, токен, журнал, логи.

Где она лежит:
  Linux    $XDG_DATA_HOME/timetable_upload  (обычно ~/.local/share/timetable_upload)
  Windows  %APPDATA%\\timetable_upload
  macOS    ~/Library/Application Support/timetable_upload
Переопределяется переменной окружения TIMETABLE_UPLOAD_HOME.
"""

from __future__ import annotations

import os
import sys

APP_NAME = "timetable_upload"
HOME_ENV = "TIMETABLE_UPLOAD_HOME"

CACHE_DIR = "cache"
MAPPING_FILE = "mapping.json"
TOKEN_FILE = "aupd_token.txt"
REFRESH_FILE = "aupd_refresh_token.txt"
JOURNAL_FILE = "upload.journal.jsonl"
LOGS_DIR = "logs"
WEB_DIR = "web"          # профиль встроенного браузера (cookie mos.ru)


def data_dir() -> str:
    """Папка данных (создаётся при первом обращении)."""
    home = os.environ.get(HOME_ENV, "").strip()
    if not home:
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        elif sys.platform == "darwin":
            base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
        else:
            base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        home = os.path.join(base, APP_NAME)
    os.makedirs(home, exist_ok=True)
    return home


def cache_dir() -> str:
    return os.path.join(data_dir(), CACHE_DIR)


def mapping_file() -> str:
    return os.path.join(data_dir(), MAPPING_FILE)


def token_file() -> str:
    return os.path.join(data_dir(), TOKEN_FILE)


def refresh_file() -> str:
    return os.path.join(data_dir(), REFRESH_FILE)


def journal_file() -> str:
    return os.path.join(data_dir(), JOURNAL_FILE)


def logs_dir() -> str:
    d = os.path.join(data_dir(), LOGS_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def web_dir() -> str:
    return os.path.join(data_dir(), WEB_DIR)


def describe() -> list[tuple[str, str, bool]]:
    """(что, путь, существует ли) — для команды paths и вкладки настроек."""
    items = [
        ("папка данных", data_dir()),
        ("кэш справочников МЭШ", cache_dir()),
        ("сопоставление", mapping_file()),
        ("токен aupd_token", token_file()),
        ("refresh-токен", refresh_file()),
        ("журнал загрузок", journal_file()),
        ("логи", os.path.join(data_dir(), LOGS_DIR)),
        ("сессия браузера mos.ru", web_dir()),
    ]
    return [(what, p, os.path.exists(p)) for what, p in items]
