"""Состояние приложения: пути к файлам, загруженные данные, клиент МЭШ."""

from __future__ import annotations

import datetime as _dt
import logging
import os

from PySide6.QtCore import QObject, QSettings, Signal

from .. import auth, chngf, paths, refdata, uploader
from ..mapping import Mapping
from ..mesh_api import MAX_WORKERS, MeshClient

log = logging.getLogger(__name__)


def default_paths() -> dict[str, str]:
    """Пути по умолчанию — в папке данных программы (см. paths.data_dir)."""
    return {
        "file": "",
        "cache": paths.cache_dir(),
        "mapping": paths.mapping_file(),
        "token_file": paths.token_file(),
        "journal": paths.journal_file(),
        "profile_id": "",
        "academic_year": "",
        "week1_monday": "",
    }


DEFAULT_WORKERS = 8


class AppState(QObject):
    """Всё, что разделяют вкладки: пути, файл расписания, кэш справочников, сопоставление.

    Сигнал ``changed(what)`` — 'file' | 'refdata' | 'mapping' (загружено другое сопоставление) |
    'mapping_edited' (правка в памяти) | 'mapping_saved' | 'session' | 'paths' | 'journal'."""
    changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.settings = QSettings("timetable_upload", "gui")
        self.paths: dict[str, str] = {k: str(self.settings.value("paths/" + k, v)) for k, v in default_paths().items()}
        self.tt: chngf.Timetable | None = None
        self.rd: refdata.RefData | None = None
        self.m: Mapping = Mapping()
        self.mapping_dirty = False
        self.client: MeshClient | None = None
        self.session_text = ""
        self.session_short = ""

    # ------------------------------------------------------------------ настройки
    @property
    def workers(self) -> int:
        """Сколько запросов слать одновременно при загрузке и откате."""
        try:
            return min(max(int(self.settings.value("upload/workers", DEFAULT_WORKERS)), 1), MAX_WORKERS)
        except (TypeError, ValueError):
            return DEFAULT_WORKERS

    def set_workers(self, n: int) -> None:
        self.settings.setValue("upload/workers", int(n))

    # ------------------------------------------------------------------ пути
    def set_path(self, key: str, value: str) -> None:
        if self.paths.get(key) == value:
            return
        self.paths[key] = value
        self.settings.setValue("paths/" + key, value)
        self.changed.emit("paths")

    def reset_paths(self) -> None:
        """Вернуть стандартные пути к файлам программы (папка данных); файл расписания и настройки
        входа (профиль, год, первая неделя) не трогаются."""
        for k, v in default_paths().items():
            if k in ("cache", "mapping", "token_file", "journal"):
                self.paths[k] = v
                self.settings.setValue("paths/" + k, v)
        self.changed.emit("paths")

    # ------------------------------------------------------------------ загрузка данных (локально)
    def load_file(self) -> chngf.Timetable:
        self.tt = chngf.load(self.paths["file"])
        log.info("файл %s: классов %d, занятий %d", self.paths["file"], len(self.tt.classes),
                 sum(1 for _ in self.tt.iter_lessons()))
        self.changed.emit("file")
        return self.tt

    def load_refdata(self) -> refdata.RefData | None:
        cache = self.paths["cache"]
        if not os.path.isdir(cache) or not os.path.exists(os.path.join(cache, "class_units.json")):
            self.rd = None
        else:
            self.rd = refdata.RefData.load(cache, self.m.ec_course_suffix)
            log.info("кэш %s: %s", cache, self.rd.summary())
        self.changed.emit("refdata")
        return self.rd

    def load_mapping(self) -> Mapping:
        self.m = Mapping.load(self.paths["mapping"])
        self.mapping_dirty = False
        if self.rd is not None:       # индекс курсов ВД зависит от настройки в сопоставлении
            self.rd._index(self.m.ec_course_suffix)
        self.changed.emit("mapping")
        return self.m

    def save_mapping(self) -> None:
        self.m.save(self.paths["mapping"])
        self.mapping_dirty = False
        log.info("сопоставление записано в %s", self.paths["mapping"])
        self.changed.emit("mapping_saved")      # данные те же — построенные планы остаются в силе

    def mark_mapping_dirty(self) -> None:
        self.mapping_dirty = True
        self.changed.emit("mapping_edited")

    # ------------------------------------------------------------------ МЭШ
    def read_token(self) -> str:
        """Годный токен из файла (истекающий обновляется по refresh-токену); иначе понятная ошибка."""
        return auth.current_token(self.paths["token_file"] or paths.token_file())

    def write_token(self, token: str, refresh_token: str = "") -> str:
        """Записать токен (и refresh-токен) в файлы; возвращает путь к файлу токена."""
        path = auth.save_tokens(token, refresh_token, self.paths["token_file"] or paths.token_file())
        self.forget_client()
        log.info("токен записан в %s%s", path, " (с refresh-токеном)" if refresh_token else "")
        return path

    def get_client(self) -> MeshClient:
        """Клиент с выполненным входом (вызывать из фоновой задачи: делает запросы к серверу)."""
        token = self.read_token()
        if self.client is not None and self.client.token == token:   # токен мог обновиться внутри клиента
            return self.client
        token_file = self.paths["token_file"] or paths.token_file()
        client = MeshClient(token, refresh=lambda: auth.current_token(token_file, force=True))
        pid = int(self.paths["profile_id"]) if self.paths["profile_id"].strip() else None
        ay = self.paths["academic_year"].strip() or None
        s = client.login(profile_id=pid, academic_year=ay)
        self.client = client
        school = s.profile.get("name") or s.profile.get("school_name") or ""
        self.session_text = f"профиль {s.profile_id} ({school}), учебный год {s.academic_year.get('name')} (id {s.academic_year_id})"
        self.session_short = f"вход: профиль {s.profile_id}, год {s.academic_year.get('name')}"
        log.info("вход выполнен: %s", self.session_text)
        return client

    def get_client_for_refdata(self) -> MeshClient:
        """Клиент для работы с кэшем справочников: учебный год входа должен совпадать с годом кэша
        (иначе id недель и групп — чужие).  Для загрузки справочников и проверки входа — :meth:`get_client`."""
        client = self.get_client()
        if self.rd is not None:
            self.rd.check_academic_year(client)
        return client

    def forget_client(self) -> None:
        self.client, self.session_text, self.session_short = None, "", ""

    # ------------------------------------------------------------------ производные
    def week1_monday(self) -> _dt.date:
        override = self.paths["week1_monday"].strip()
        return uploader.week1_monday_for(self.tt, _dt.date.fromisoformat(override) if override else None,
                                         self.rd.academic_year if self.rd else None)

    def ready(self) -> tuple[bool, str]:
        if self.tt is None:
            return False, "не загружен файл .chngf (вкладка «Данные и вход»)"
        if self.rd is None:
            return False, "нет кэша справочников МЭШ — загрузите их (вкладка «Данные и вход»)"
        return True, ""
