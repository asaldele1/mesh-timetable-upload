"""Состояние приложения: пути к файлам, загруженные данные, клиент МЭШ."""

from __future__ import annotations

import datetime as _dt
import logging
import os

from PySide6.QtCore import QObject, QSettings, Signal

from .. import auth, chngf, paths, refdata, schools, uploader
from ..mapping import Mapping
from ..mesh_api import MAX_WORKERS, MeshClient, MeshError

log = logging.getLogger(__name__)


def default_paths(school_id: int | None = None) -> dict[str, str]:
    """Пути по умолчанию — в папке данных программы (см. paths.data_dir); кэш, сопоставление и журнал —
    в папке школы (см. schools)."""
    return {
        "file": "",
        "cache": schools.default_path("cache", school_id),
        "mapping": schools.default_path("mapping", school_id),
        "token_file": paths.token_file(),
        "journal": schools.default_path("journal", school_id),
        "academic_year": "",
        "week1_monday": "",
    }


class SchoolChanged(MeshError):
    """Вход выполнен не в ту школу, под которую открыты файлы (первый вход, сменился список школ):
    переключить школу нужно в главном потоке (:meth:`AppState.choose_school`), затем повторить."""

    def __init__(self, profile: dict, profiles: list[dict]):
        super().__init__(409, "сменилась школа входа — повторите действие")
        self.profile, self.profiles = profile, profiles


DEFAULT_WORKERS = 8


class AppState(QObject):
    """Всё, что разделяют вкладки: пути, файл расписания, кэш справочников, сопоставление.

    Сигнал ``changed(what)`` — 'file' | 'refdata' | 'mapping' (загружено другое сопоставление) |
    'mapping_edited' (правка в памяти) | 'mapping_saved' | 'session' | 'paths' | 'journal' |
    'refreshing' (началось/закончилось обновление справочников с сервера)."""
    changed = Signal(str)

    def __init__(self):
        super().__init__()
        self.settings = QSettings("timetable_upload", "gui")
        self.school = schools.load_current()
        old_pid = str(self.settings.value("paths/profile_id", "")).strip()   # прежняя версия: профиль вписывали руками
        if self.school.profile_id is None and old_pid.isdigit():
            self.school.profile_id = int(old_pid)       # станет выбранной школой при первом входе
        self.school_id = self.school.school_id           # школа, под которую открыты кэш, сопоставление и журнал
        defaults = default_paths(self.school_id)
        self.paths: dict[str, str] = {k: str(self.settings.value("paths/" + k, v)) for k, v in defaults.items()}
        for key in schools.KEYS:          # стандартные пути (в т.ч. прежние общие) следуют за школой
            if schools.is_default_path(key, self.paths[key]):
                self.paths[key] = defaults[key]
        self.tt: chngf.Timetable | None = None
        self.rd: refdata.RefData | None = None
        self.m: Mapping = Mapping()
        self.mapping_dirty = False
        self.client: MeshClient | None = None
        self.session_text = ""
        self.session_short = ""
        # обновление справочников с сервера (см. MainWindow.start_refresh): пока идёт, части, которым
        # нужен список групп, недоступны; refresh_gen растёт при смене входа — результат старого входа не берём
        self.refreshing = False
        self.refresh_error = ""
        self.refresh_gen = 0

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
        # стандартный путь файла школы хранится пустым: при смене школы он сменится вместе с ней
        self.settings.setValue("paths/" + key, "" if key in schools.KEYS and schools.is_default_path(key, value) else value)
        self.changed.emit("paths")

    def reset_paths(self) -> None:
        """Вернуть стандартные пути к файлам программы (папка данных, файлы школы — в её папке); файл
        расписания и настройки входа (год, первая неделя) не трогаются."""
        for k, v in default_paths(self.school_id).items():
            if k in ("cache", "mapping", "token_file", "journal"):
                self.paths[k] = v
                self.settings.setValue("paths/" + k, "" if k in schools.KEYS else v)
        self.changed.emit("paths")

    # ------------------------------------------------------------------ школа
    def choose_school(self, profile: dict, profiles: list[dict]) -> None:
        """Выбрать школу (её учительский профиль) — вызывать в главном потоке.  Другая школа — стандартные
        пути переходят в её папку (данные прежней версии переезжают туда при первом входе), кэш,
        сопоставление и журнал перечитываются, вход — заново.  Несохранённое сопоставление вызывающий
        должен сохранить или отбросить заранее."""
        self.school = schools.remember(profile, profiles)
        if self.school.school_id != self.school_id:
            old = self.school_id
            schools.migrate_legacy(self.school.school_id, len(profiles))
            self.school_id = self.school.school_id
            for key in schools.KEYS:
                if schools.is_default_path(key, self.paths[key]):
                    self.paths[key] = schools.default_path(key, self.school_id)
            log.info("школа: %s (профиль %s)%s", self.school.label, self.school.profile_id,
                     "" if old is None else f", файлы — {schools.school_dir(self.school_id)}")
            self.forget_client()
            self.changed.emit("paths")
            for load, what in ((self.load_mapping, "сопоставление"), (self.load_refdata, "кэш")):
                try:
                    load()
                except Exception as e:
                    log.error("%s: %s", what, e)
            self.changed.emit("journal")
        elif self.client is not None and self.client.session_info.profile_id != self.school.profile_id:
            self.forget_client()
        self.changed.emit("session")

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

    def set_refdata(self, rd: refdata.RefData) -> None:
        """Подменить справочники свежезагруженными (они уже записаны в кэш)."""
        rd._index(self.m.ec_course_suffix)
        self.rd = rd
        self.refresh_error = ""
        log.info("справочники обновлены с сервера: %s", rd.summary())
        self.changed.emit("refdata")

    def set_refreshing(self, on: bool, error: str = "") -> None:
        self.refreshing = on
        if error:
            self.refresh_error = error
        self.changed.emit("refreshing")

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
        ay = self.paths["academic_year"].strip() or None
        s = client.login(profile_id=self.school.profile_id, academic_year=ay)    # несколько школ без выбора — ProfileChoiceRequired
        if s.school_id != self.school_id or s.profile_id != self.school.profile_id:
            raise SchoolChanged(s.profile, client.profiles)
        self.client = client
        self.session_text = (f"школа {s.school_label} (профиль {s.profile_id}), "
                             f"учебный год {s.academic_year.get('name')} (id {s.academic_year_id})")
        self.session_short = f"вход: {s.school_label}, год {s.academic_year.get('name')}"
        log.info("вход выполнен: %s", self.session_text)
        return client

    def get_client_for_refdata(self) -> MeshClient:
        """Клиент для работы с кэшем справочников: школа и учебный год входа должны совпадать с кэшем
        (иначе id недель и групп — чужие).  Для загрузки справочников и проверки входа — :meth:`get_client`."""
        client = self.get_client()
        if self.rd is not None:
            self.rd.check_session(client)
        return client

    def forget_client(self) -> None:
        """Сменились токен/школа/год: клиент заново, идущее обновление справочников — не в счёт."""
        self.client, self.session_text, self.session_short = None, "", ""
        self.refresh_gen += 1

    # ------------------------------------------------------------------ производные
    def week1_monday(self) -> _dt.date:
        override = self.paths["week1_monday"].strip()
        return uploader.week1_monday_for(self.tt, _dt.date.fromisoformat(override) if override else None,
                                         self.rd.academic_year if self.rd else None)

    def ready(self) -> tuple[bool, str]:
        if self.tt is None:
            return False, "не загружен файл .chngf (вкладка «Данные и вход»)"
        if self.rd is None:
            if self.refreshing:
                return False, "справочники МЭШ ещё загружаются с сервера — подождите"
            return False, "нет кэша справочников МЭШ — загрузите их (вкладка «Данные и вход»)"
        return True, ""
