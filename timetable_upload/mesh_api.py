"""Клиент учительского API МЭШ (school.mos.ru) в объёме, нужном для работы с расписанием.

Пути и параметры взяты из HAR-захвата и JS-бандла раздела «Управление
образованием → Календарь → Расписание по классам» и «Расписание внеурочной
деятельности» (пути ``/api/ej/ec/...``).

Вход — токен ``aupd_token`` (JWT; в браузере лежит в одноимённой cookie и
передаётся как ``Authorization: Bearer``).  По нему :meth:`MeshClient.login`
получает профиль сотрудника (заголовок ``Profile-Id``) и текущий учебный год
(заголовок ``aid`` и параметр ``academic_year_id``).  Если задан ``refresh``
(функция, возвращающая новый токен — см. :mod:`auth`), на ответ 401 клиент один
раз обновляет токен и повторяет запрос.
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://school.mos.ru"
ACL = "/api/ej/acl/v1"
CORE = "/api/ej/core/teacher/v1"
PLAN = "/api/ej/plan/teacher/v1"
EC = "/api/ej/ec/teacher/v1"          # внеурочная деятельность


class MeshError(RuntimeError):
    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.payload = payload


@dataclass
class Session:
    profile: dict            # профиль сотрудника из /acl/v1/sessions
    academic_year: dict      # учебный год из /academic_years

    @property
    def profile_id(self) -> int:
        return int(self.profile["id"])

    @property
    def academic_year_id(self) -> int:
        return int(self.academic_year["id"])


MAX_WORKERS = 16     # верхняя граница числа параллельных запросов (размер пула соединений)


def fmt_roles(roles: Any) -> str:
    """Роли из teacher_profile (список строк) — через запятую, без скобок и кавычек."""
    if isinstance(roles, (list, tuple, set)):
        return ", ".join(str(r) for r in roles) or "—"
    return str(roles) if roles else "—"


class MeshClient:
    """Клиент API МЭШ. Один экземпляр можно использовать из нескольких потоков одновременно
    (см. ``workers`` в uploader): пул соединений общий, пауза ``delay`` — общая на всех,
    токен по 401 обновляет один поток, остальные подхватывают новый."""

    def __init__(self, token: str, base_url: str = DEFAULT_BASE_URL,
                 delay: float = 0, timeout: float = 60, retries: int = 3,
                 refresh: Callable[[], str] | None = None):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.refresh = refresh
        self.session_info: Session | None = None
        self.http = requests.Session()
        adapter = HTTPAdapter(pool_maxsize=MAX_WORKERS)
        self.http.mount("https://", adapter)
        self.http.mount("http://", adapter)
        self.http.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {self.token}",
            "x-mes-subsystem": "hteacherweb",
            "x-mes-hostid": "22",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/educationmanagement/calendar/schedule/by-classes",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/140.0.0.0 Safari/537.36",
        })
        self._last = 0.0
        self._lock = threading.Lock()           # учёт паузы между запросами
        self._refresh_lock = threading.Lock()   # обновление токена — по одному

    def set_token(self, token: str) -> None:
        self.token = token.strip()
        self.http.headers["Authorization"] = f"Bearer {self.token}"

    # ------------------------------------------------------------------ низкий уровень
    def request(self, method: str, path: str, params: dict | None = None, json_body: Any = None,
                _retry_auth: bool = True) -> Any:
        """json_body — данные либо функция без аргументов, которая их строит: она вызывается заново при
        повторе после обновления токена (тело может зависеть от токена, см. :meth:`sessions`)."""
        url = self.base_url + path
        body = json_body() if callable(json_body) else json_body
        token_used = self.token
        for attempt in range(self.retries + 1):
            self._pause()
            log.debug("%s %s params=%s body=%s", method, path, params, body)
            resp = self.http.request(method, url, params=params, json=body, timeout=self.timeout)
            if resp.status_code in (429, 502, 503, 504) and attempt < self.retries:
                pause = 2.0 * (attempt + 1)
                log.warning("%s %s: сервер ответил %s%s — повтор через %.0f с (%d из %d)",
                            method, path, resp.status_code, " (слишком много запросов)" if resp.status_code == 429 else "",
                            pause, attempt + 1, self.retries)
                time.sleep(pause)
                continue
            break
        if resp.status_code == 401 and self.refresh is not None and _retry_auth:
            with self._refresh_lock:
                if self.token == token_used:        # иначе другой поток уже обновил
                    log.info("сервер ответил 401 — обновляем токен и повторяем запрос")
                    self.set_token(self.refresh())
            return self.request(method, path, params, json_body, _retry_auth=False)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
            except ValueError:
                payload = resp.text
            msg = payload.get("message") if isinstance(payload, dict) else str(payload)[:500]
            raise MeshError(resp.status_code, msg or resp.reason, payload)
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _pause(self) -> None:
        """Выдержать ``delay`` после предыдущего запроса (любого потока)."""
        if self.delay <= 0:
            return
        with self._lock:
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, json_body=body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def get_paged(self, path: str, per_page: int = 1000, **params) -> list:
        """Постраничная выборка (page/per_page); сервер отдаёт не более 1000 записей за раз."""
        out: list = []
        page = 1
        while True:
            chunk = self.get(path, page=page, per_page=per_page, **params) or []
            out.extend(chunk)
            if len(chunk) < per_page:
                return out
            page += 1

    # ------------------------------------------------------------------ вход
    def sessions(self) -> dict:
        """POST /acl/v1/sessions — профили пользователя по токену (токен и в теле: при повторе после 401
        тело строится заново, уже с обновлённым)."""
        return self.post(f"{ACL}/sessions", lambda: {"auth_token": self.token})

    def academic_years(self, only_current: bool = False) -> list:
        params = {"only_current_year": "true"} if only_current else {}
        return self.get(f"{CORE}/academic_years", **params)

    def login(self, profile_id: int | None = None, academic_year: int | str | None = None) -> Session:
        """Выбирает профиль сотрудника и учебный год, выставляет заголовки Profile-Id и aid.

        profile_id — если у пользователя несколько профилей сотрудника;
        academic_year — id либо название («2025/26»); по умолчанию текущий год.
        """
        data = self.sessions()
        profiles = [p for p in (data.get("profiles") or []) if str(p.get("type", "")).lower() == "teacher"]
        if not profiles:
            raise MeshError(403, f"по токену нет профилей сотрудника: {data}")
        if profile_id is not None:
            profile = next((p for p in profiles if int(p["id"]) == profile_id), None)
            if profile is None:
                raise MeshError(403, f"профиль {profile_id} не найден среди {[p['id'] for p in profiles]}")
        elif len(profiles) == 1:
            profile = profiles[0]
        else:
            raise MeshError(403, "несколько профилей сотрудника, укажите --profile-id: "
                            + "; ".join(f"{p['id']} ({p.get('school_name') or p.get('organization_id')})" for p in profiles))
        self.http.headers["Profile-Id"] = str(profile["id"])

        if academic_year is None:
            years = self.academic_years(only_current=True)
            if not years:
                raise MeshError(404, "сервер не вернул текущий учебный год")
            year = years[0]
        else:
            years = self.academic_years()
            year = next((y for y in years if str(y.get("id")) == str(academic_year) or y.get("name") == academic_year), None)
            if year is None:
                raise MeshError(404, f"учебный год {academic_year!r} не найден: {[(y.get('id'), y.get('name')) for y in years]}")
        self.http.headers["aid"] = str(year["id"])
        self.session_info = Session(profile, year)
        return self.session_info

    @property
    def ay(self) -> int:
        if not self.session_info:
            raise RuntimeError("сначала login()")
        return self.session_info.academic_year_id

    # ------------------------------------------------------------------ справочники
    def teacher_profile(self) -> Any:
        return self.get(f"{CORE}/teacher_profiles/{self.session_info.profile_id}",
                        with_assigned_groups="true", academic_year_id=self.ay)

    def class_levels(self) -> list:
        return self.get(f"{CORE}/class_levels")

    def class_units(self, class_level_id: int | None = None) -> list:
        params = {"with_home_based": "true"}
        if class_level_id is not None:
            params["class_level_id"] = class_level_id
        return self.get(f"{CORE}/class_units", **params)

    def rooms(self) -> list:
        return self.get(f"{CORE}/rooms")

    def teacher_profiles(self, class_level_id: int | None = None) -> list:
        params = {"academic_year_id": self.ay, "with_deleted": "true"}
        if class_level_id is not None:
            params["class_level_id"] = class_level_id
        return self.get_paged(f"{CORE}/teacher_profiles", per_page=300, **params)

    def bells_timetable_assignments(self, class_unit_ids: Iterable[int] | None = None) -> list:
        params = {"academic_year_id": self.ay}
        if class_unit_ids:
            params["class_unit_ids"] = ",".join(str(i) for i in class_unit_ids)
        return self.get_paged(f"{CORE}/bells_timetable_assignments", per_page=300, **params)

    def bells_timetables(self, ids: Iterable[int] | None = None) -> list:
        params = {"academic_year_id": self.ay, "include": "bells_day_timetables"}
        if ids:
            params["ids"] = ",".join(str(i) for i in ids)
        return self.get(f"{CORE}/bells_timetables", **params)

    def groups(self, class_level_id: int | None = None) -> list:
        params = {"academic_year_id": self.ay, "with_teacher_loads": "true",
                  "with_student_ids": "true", "with_incompatible_groups": "true"}
        if class_level_id is not None:
            params["class_level_id"] = class_level_id
        return self.get_paged(f"{PLAN}/groups", per_page=1000, **params)

    # ------------------------------------------------------------------ расписание
    def schedules(self) -> list:
        """Список «расписаний» — по одному на учебную неделю (id, week_number, число занятий)."""
        return self.get(f"{PLAN}/schedules", academic_year_id=self.ay, with_hours="true", with_count="true")

    def schedule_items(self, week_number: int, class_unit_ids: Iterable[int] | None = None) -> list:
        params = {"academic_year_id": self.ay, "week_number": week_number}
        if class_unit_ids:
            params["class_unit_id"] = ",".join(str(i) for i in class_unit_ids)
        return self.get(f"{PLAN}/schedules/items", **params) or []

    def create_item(self, schedule_id: int, group_id: int, bell_id: int, date: _dt.date,
                    teacher_id: int | None, room_id: int | None, building_id: int | None,
                    lesson_type: str = "NORMAL") -> dict:
        body: dict[str, Any] = {
            "bell_id": bell_id,
            "date": [date.year, date.month, date.day],
            "group_id": group_id,
            "lesson_type": lesson_type,
            "schedule_id": schedule_id,
        }
        if teacher_id is not None:
            body["teacher_id"] = teacher_id
        if room_id is not None:
            body["room_id"] = room_id
        if building_id is not None:
            body["building_id"] = building_id
        return self.post(f"{PLAN}/schedules/{schedule_id}/items", body)

    def delete_item(self, schedule_id: int, item_id: int) -> None:
        self.delete(f"{PLAN}/schedules/{schedule_id}/items/{item_id}")

    def copy_check(self, schedule_id: int, class_level_ids: list[int], class_unit_id: int,
                   week_numbers: list[int]) -> dict:
        return self.post(f"{PLAN}/schedules/{schedule_id}/copy_check", {
            "academic_year_id": self.ay, "class_level_ids": class_level_ids,
            "class_unit_id": class_unit_id, "week_numbers": week_numbers})

    def copy(self, schedule_id: int, class_level_ids: list[int], class_unit_id: int,
             week_numbers: list[int]) -> dict:
        return self.post(f"{PLAN}/schedules/{schedule_id}/copy", {
            "academic_year_id": self.ay, "class_level_ids": class_level_ids,
            "class_unit_id": class_unit_id, "week_numbers": week_numbers})

    # ------------------------------------------------------------------ внеурочная деятельность (ВД)
    def ec_activities(self) -> list:
        """Курсы ВД («активности»): название с параллелью, классы, учителя, направление."""
        return self.get_paged(f"{EC}/ec_activity", per_page=300)

    def ec_groups(self) -> list:
        """Группы ВД (POST со страницами; фильтр по учителям не нужен)."""
        out: list = []
        page = 1
        while True:
            chunk = self.post(f"{EC}/ec_groups/list", {"page": page, "per_page": 300, "teacher_ids": None}) or []
            out.extend(chunk)
            if len(chunk) < 300:
                return out
            page += 1

    def ec_schedules(self) -> list:
        """Расписания ВД по неделям текущего года (id, week_number); недостающие недели создаёт
        :meth:`create_ec_schedule` (веб-интерфейс делает это сам при открытии раздела).
        id расписания недели не постоянен — сайт их пересоздаёт, поэтому перед записью
        занятий ВД список запрашивается заново (``uploader.sync_ec_schedule``)."""
        return self.get(f"{EC}/ec_schedules", per_page=300) or []

    def create_ec_schedule(self, week_number: int) -> dict:
        return self.post(f"{EC}/ec_schedules", {"academic_year_id": self.ay, "week_number": week_number})

    def ec_schedule_items(self, ec_schedule_id: int, class_unit_ids: Iterable[int] | None = None,
                          ec_group_ids: Iterable[int] | None = None) -> list:
        """Занятия ВД недели; без фильтра по классам/группам сервер отдаёт пустой ответ."""
        params: dict[str, Any] = {"ec_schedule_id": ec_schedule_id}
        if class_unit_ids:
            params["class_unit_ids"] = ",".join(str(i) for i in class_unit_ids)
        if ec_group_ids:
            params["ec_group_ids"] = ",".join(str(i) for i in ec_group_ids)
        return self.get_paged(f"{EC}/ec_schedule_items", per_page=300, **params)

    def create_ec_item(self, ec_schedule_id: int, ec_group_id: int, date: _dt.date, begin_time: str,
                       end_time: str, duration: str, teacher_id: int | None, room_id: int | None,
                       building_id: int | None) -> dict:
        """Занятие ВД: время задаётся строками «ЧЧ:ММ», длительность — «00:45» (как dtoForCreateSubject в UI)."""
        body: dict[str, Any] = {
            "begin_time": begin_time,
            "end_time": end_time,
            "date": date.isoformat(),
            "duration": duration,
            "ec_group_id": ec_group_id,
            "ec_schedule_id": ec_schedule_id,
        }
        if teacher_id is not None:
            body["teacher_id"] = teacher_id
        if room_id is not None:
            body["room_id"] = room_id
        if building_id is not None:
            body["building_id"] = building_id
        return self.post(f"{EC}/ec_schedule_items", body)

    def delete_ec_item(self, item_id: int) -> None:
        self.delete(f"{EC}/ec_schedule_items/{item_id}")
