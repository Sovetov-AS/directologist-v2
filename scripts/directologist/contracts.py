"""Строгий контракт профиля и привязка локальных путей к проекту."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SCHEMA_VERSION = 1
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z", re.ASCII)
PROVIDERS = {"direct", "metrika", "wordstat", "crm"}
PROFILE_FIELDS = {
    "schema_version", "project_id", "display_name", "timezone",
    "binding_version", "bindings",
}
RESOURCE_FIELDS = {
    "direct": {"client_login", "campaign_ids"},
    "metrika": {"counter_id", "goal_ids"},
    "wordstat": {"folder_id", "region_ids"},
    "crm": {"bridge_id"},
}


class ContractError(ValueError):
    """Сообщение не должно содержать непроверенное входное значение."""


def identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ContractError("Идентификатор должен содержать строчные латинские буквы, цифры и дефис.")
    return value


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("Повторяющиеся поля JSON запрещены.")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ContractError("Неконечные числа JSON запрещены.")


def read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ContractError("Ожидался обычный JSON-файл без символьной ссылки.")
    if path.stat().st_size > 1024 * 1024:
        raise ContractError("JSON-файл превышает допустимый размер.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError("Не удалось прочитать корректный UTF-8 JSON.") from exc
    if not isinstance(data, dict):
        raise ContractError("Корень JSON должен быть объектом.")
    return data


def confined(root: Path, *parts: str) -> Path:
    """Reject traversal and existing symlinks; not an OS sandbox/TOCTOU guarantee."""
    candidate = root.joinpath(*parts)
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ContractError("Путь вне выбранного проекта.") from exc
    cursor = root
    for part in relative.parts:
        if part in {"..", "."}:
            raise ContractError("Переходы между каталогами запрещены.")
        cursor = cursor / part
        if cursor.is_symlink():
            raise ContractError("Символьные ссылки в данных проекта запрещены.")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ContractError("Путь вне выбранного проекта.")
    return candidate


@dataclass(frozen=True)
class ProjectContext:
    workspace: Path
    project_id: str
    profile_json: str

    @property
    def profile(self) -> dict:
        # Возвращается копия: изменение caller-словаря не меняет исходный контекст.
        return json.loads(self.profile_json)

    @property
    def directory(self) -> Path:
        return confined(self.workspace, "projects", self.project_id)

    @property
    def context_hash(self) -> str:
        data = self.profile
        return digest({k: data[k] for k in ("schema_version", "project_id", "timezone", "binding_version", "bindings")})

    @property
    def database(self) -> Path:
        return confined(self.directory, "state.sqlite3")


def load_project(workspace: Path, project_id: str) -> ProjectContext:
    identifier(project_id)
    workspace = workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ContractError("Workspace должен быть каталогом.")
    path = confined(workspace, "projects", project_id, "profile.json")
    data = read_json(path)
    return ProjectContext(workspace, project_id, validate_profile(data, project_id))


def validate_profile(data: dict, project_id: str) -> str:
    identifier(project_id)
    if set(data) != PROFILE_FIELDS:
        raise ContractError("Набор полей профиля не соответствует контракту; секреты здесь запрещены.")
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        raise ContractError("Неподдерживаемая версия профиля; требуется явная миграция.")
    if data["project_id"] != project_id:
        raise ContractError("Профиль принадлежит другому проекту.")
    if not isinstance(data["display_name"], str) or not data["display_name"].strip() or len(data["display_name"]) > 120:
        raise ContractError("Некорректное отображаемое имя проекта.")
    if not isinstance(data["timezone"], str):
        raise ContractError("Часовой пояс должен быть строкой IANA.")
    try:
        ZoneInfo(data["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError("Неизвестный часовой пояс.") from exc
    if type(data["binding_version"]) is not int or data["binding_version"] < 0:
        raise ContractError("Версия привязок должна быть неотрицательным целым числом.")
    bindings = data["bindings"]
    if not isinstance(bindings, dict) or set(bindings) - PROVIDERS:
        raise ContractError("Неизвестное подключение в профиле.")
    for provider, binding in bindings.items():
        if not isinstance(binding, dict) or set(binding) != {"connection_id", "resources"}:
            raise ContractError("Привязка должна содержать connection_id и resources, без credentials.")
        identifier(binding["connection_id"])
        resources = binding["resources"]
        if not isinstance(resources, dict) or not resources or set(resources) - RESOURCE_FIELDS[provider]:
            raise ContractError("Неподдерживаемый набор ресурсов подключения.")
        for key, value in resources.items():
            values = value if key.endswith("_ids") else [value]
            if not isinstance(values, list) or (not values and not (provider == "direct" and key == "campaign_ids")):
                raise ContractError("Ожидался непустой список ресурсов.")
            if any(not isinstance(v, str) or not v or len(v) > 128 or any(ord(ch) < 32 for ch in v) for v in values):
                raise ContractError("Идентификатор ресурса должен быть непустой строкой.")
            if len(set(values)) != len(values):
                raise ContractError("Повторяющиеся ресурсы запрещены.")
    if bindings and data["binding_version"] == 0:
        raise ContractError("Привязанные ресурсы требуют положительной версии привязок.")
    return canonical(data)
