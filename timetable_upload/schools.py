"""Школы пользователя и раздельные данные для каждой.

У сотрудника нескольких школ ``/acl/v1/sessions`` возвращает по учительскому профилю на школу, школу
для всех запросов задаёт заголовок ``Profile-Id`` (см. :meth:`MeshClient.login`).  Выбранный профиль и
список профилей с последнего входа лежат в папке данных (``school.json``) — общие для окна и командной
строки.  Кэш справочников, сопоставление и журнал загрузок у каждой школы свои: ``schools/<school_id>/``
в папке данных.  Данные прежних версий (прямо в папке данных) при первом входе переносятся в папку
своей школы (:func:`migrate_legacy`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field

from . import paths, refdata
from .mesh_api import school_label

log = logging.getLogger(__name__)

CURRENT_FILE = "school.json"
SCHOOLS_DIR = "schools"
KEYS = {"cache": paths.CACHE_DIR, "mapping": paths.MAPPING_FILE, "journal": paths.JOURNAL_FILE}   # файлы школы
PROFILE_FIELDS = ("id", "school_id", "school_shortname", "school_name", "roles")               # что помним о профиле


def school_dir(school_id) -> str:
    return os.path.join(paths.data_dir(), SCHOOLS_DIR, str(school_id))


def default_path(key: str, school_id=None) -> str:
    """Стандартный путь файла школы (key — cache | mapping | journal); школа неизвестна — прежний общий."""
    return os.path.join(school_dir(school_id) if school_id is not None else paths.data_dir(), KEYS[key])


def is_default_path(key: str, value: str) -> bool:
    """Путь стандартный — пустой, прежний общий или любой школы: такие пути следуют за выбранной школой."""
    if not value:
        return True
    v = os.path.normpath(os.path.abspath(value))
    if v == os.path.normpath(default_path(key)):
        return True
    parent, name = os.path.split(v)
    return name == KEYS[key] and os.path.dirname(parent) == os.path.normpath(os.path.join(paths.data_dir(), SCHOOLS_DIR))


@dataclass
class Current:
    """Выбранная школа (её учительский профиль) и все профили пользователя с последнего входа."""
    profile_id: int | None = None
    school_id: int | None = None
    profiles: list[dict] = field(default_factory=list)

    def profile(self, profile_id=None) -> dict | None:
        pid = self.profile_id if profile_id is None else profile_id
        return next((p for p in self.profiles if pid is not None and int(p["id"]) == int(pid)), None)

    def school_of(self, profile_id) -> int | None:
        p = self.profile(profile_id)
        return int(p["school_id"]) if p and p.get("school_id") is not None else None

    @property
    def label(self) -> str:
        p = self.profile()
        return school_label(p) if p else (f"Школа id {self.school_id}" if self.school_id is not None else "")


def current_file() -> str:
    return os.path.join(paths.data_dir(), CURRENT_FILE)


def load_current() -> Current:
    try:
        with open(current_file(), encoding="utf-8") as f:
            d = json.load(f)
        return Current(d.get("profile_id"), d.get("school_id"), list(d.get("profiles") or []))
    except (OSError, ValueError, AttributeError):
        return Current()


def remember(profile: dict, profiles: list[dict]) -> Current:
    """Запомнить выбранный профиль и список профилей пользователя."""
    sid = profile.get("school_id")
    cur = Current(int(profile["id"]), int(sid) if sid is not None else None,
                  [{k: p.get(k) for k in PROFILE_FIELDS} for p in profiles])
    with open(current_file(), "w", encoding="utf-8") as f:
        json.dump({"profile_id": cur.profile_id, "school_id": cur.school_id, "profiles": cur.profiles},
                  f, ensure_ascii=False, indent=1)
    return cur


def legacy_owner() -> int | None:
    """Школа данных прежней версии (прямо в папке данных) — по кэшу справочников; неизвестно — None."""
    return refdata.cached_school_id(paths.cache_dir())


def migrate_legacy(school_id: int | None, n_schools: int = 1) -> list[str]:
    """Перенести кэш, сопоставление и журнал прежней версии в папку школы school_id, если они её: так
    говорит кэш справочников, а без кэша — если школа у пользователя одна (n_schools).  Уже существующие
    файлы школы не трогаются.  Возвращает перенесённые пути."""
    if school_id is None:
        return []
    legacy = {k: default_path(k) for k in KEYS}
    if not any(os.path.exists(p) for p in legacy.values()):
        return []
    owner = legacy_owner()
    if owner != school_id and not (owner is None and n_schools == 1):
        return []
    moved = []
    for key, src in legacy.items():
        dst = default_path(key, school_id)
        if not os.path.exists(src):
            continue
        if os.path.exists(dst):
            log.warning("%s: в папке школы уже есть %s — прежний файл оставлен на месте", src, dst)
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        moved.append(dst)
        log.info("перенесено в папку школы: %s → %s", src, dst)
    return moved
