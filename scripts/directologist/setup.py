"""Единый TTY-мастер и атомарная смена привязок с восстанавливаемой публикацией."""
import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
import warnings
from contextlib import closing, contextmanager

from .adapters import Adapter, Probe, ProbeError, STATES, Transport, origin
from .contracts import ContractError, ProjectContext, PROVIDERS, canonical, confined, load_project, read_json, validate_profile
from .secrets import Credential, SecretStore
from .locks import file_lock
from .storage import Store, StateError, connect, now

ORDER = ("direct", "metrika", "wordstat", "crm")
OPTIONAL = {"crm"}


class PublicationPending(ContractError):
    def __init__(self):
        super().__init__("Привязка сохранена в SQLite, публикация профиля прервана. Выполните connections recover.")


class Terminal:
    def __init__(self):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ContractError("Нет пользовательского TTY. Откройте Terminal и запустите .venv/bin/python scripts/directologist.py --project PROJECT_ID setup. Ключи в чат не отправляйте.")

    def say(self, message):
        print(message, flush=True)

    def ask(self, prompt):
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            raise ContractError("Настройка прервана; завершённые подключения сохранены.") from None

    def secret(self):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                return Credential(getpass.getpass("Ключ (скрытый ввод): "))
        except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
            raise ContractError("Безопасный ввод недоступен или прерван. Ключ не сохранён.") from None

    def choose(self, label, options, multiple=False):
        if not options:
            return []
        self.say(label + ":")
        for index, value in enumerate(options, 1):
            self.say(f"  {index}. {value}")
        value = self.ask("Номера через запятую: " if multiple else "Номер (явный выбор): ")
        if not re.fullmatch(r"[0-9]+(?:,[0-9]+)*", value):
            raise ContractError("Выберите номера из списка; автоматического выбора нет.")
        indexes = [int(v) - 1 for v in value.split(",")]
        if (not multiple and len(indexes) != 1) or len(set(indexes)) != len(indexes) or any(i < 0 or i >= len(options) for i in indexes):
            raise ContractError("Выбор не соответствует списку ресурсов.")
        return [options[i] for i in indexes]


@contextmanager
def setup_lock(context):
    with file_lock(confined(context.directory, ".setup.lock")):
        yield


def publish_profile(context, payload):
    path = confined(context.directory, "profile.json")
    fd, name = tempfile.mkstemp(prefix=".profile-", dir=context.directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def commit_connection(context, record):
    """Caller holds setup_lock. SQLite intent is durable before replacing profile."""
    provider = record["provider"]
    data = context.profile
    previous_binding = data["bindings"].get(provider)
    if record["resources"]:
        data["bindings"][provider] = {"connection_id": record["connection_id"], "resources": record["resources"]}
    else:
        data["bindings"].pop(provider, None)
    if record["connection_id"] is not None or previous_binding != data["bindings"].get(provider):
        data["binding_version"] += 1
    payload = validate_profile(data, context.project_id)
    future = ProjectContext(context.workspace, context.project_id, payload)
    with Store(context, create=True) as store:
        with store.transaction():
            if load_project(context.workspace, context.project_id).profile_json != context.profile_json:
                raise StateError("Профиль изменился во время настройки; повторите после сверки.")
            store.connection.execute("CREATE TABLE IF NOT EXISTS connections(provider TEXT PRIMARY KEY, record TEXT NOT NULL)")
            store.connection.execute("INSERT OR REPLACE INTO connections VALUES (?,?)", (provider, canonical(record)))
            for key, value in (("context_hash", future.context_hash), ("pending_profile", payload),
                               ("previous_profile", context.profile_json)):
                store.connection.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))
        try:
            publish_profile(context, payload)
        except Exception:
            raise PublicationPending() from None
        # Intent remains until next publication. It is harmless when hashes match,
        # and allows deterministic recovery after a crash between DB/file changes.
    return future


def recover_connections(context):
    with setup_lock(context), closing(connect(context.database)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            meta = dict(db.execute("SELECT key,value FROM metadata"))
            if meta.get("project_id") != context.project_id or meta.get("schema_version") != "1":
                raise ContractError("Неподдерживаемая база для восстановления привязок.")
            raw = meta.get("pending_profile")
            if not raw:
                raise ContractError("Незавершённой публикации привязок нет.")
            payload = validate_profile(json.loads(raw), context.project_id)
            future = ProjectContext(context.workspace, context.project_id, payload)
            if future.context_hash != meta.get("context_hash"):
                raise ContractError("Контекст публикации не совпадает с базой.")
            current = load_project(context.workspace, context.project_id)
            if current.profile_json not in {payload, meta.get("previous_profile")}:
                raise ContractError("Профиль изменён вручную; автоматическое восстановление запрещено.")
            publish_profile(context, payload)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return connection_status(future)


def connection_status(context):
    records = {}
    if context.database.exists():
        with Store(context, read_only=True) as store:
            if store.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='connections'").fetchone():
                records = {row[0]: json.loads(row[1]) for row in store.connection.execute("SELECT provider,record FROM connections")}
    result = []
    for provider in ORDER:
        record = records.get(provider, {})
        state = record.get("state", "UNCONFIGURED")
        if state not in STATES:
            raise ContractError("Неизвестная версия состояния подключения.")
        # Do not expose persisted config or raw resource names to the model.
        result.append({"provider": provider, "state": state, "message": STATES[state],
                       "checked_at": record.get("checked_at"), "catalog_available": state == "CHECKED"})
    return {"schema_version": 1, "project_id": context.project_id,
            "binding_version": context.profile["binding_version"], "connections": result, "autonomous_writes": False}


def has_connection(context, provider):
    if not context.database.exists():
        return False
    with Store(context, read_only=True) as store:
        if not store.connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='connections'").fetchone():
            return False
        row = store.connection.execute("SELECT record FROM connections WHERE provider=?", (provider,)).fetchone()
        return bool(row and json.loads(row[0]).get("connection_id"))


def allowed_bridges(context):
    path = confined(context.directory, "allowed-services.json")
    if not path.exists():
        return []
    data = read_json(path)
    if set(data) != {"schema_version", "crm_origins"} or type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ContractError("Неподдерживаемый список разрешённых сервисов.")
    values = data["crm_origins"]
    if not isinstance(values, list) or len(values) > 20 or any(not isinstance(v, str) for v in values):
        raise ContractError("Некорректный список адресов bridge.")
    return [origin(value) for value in values]


def configure(context, provider, credential, config, choose, secrets, adapter):
    if provider not in PROVIDERS or secrets.project_id != context.project_id:
        raise ContractError("Подключение относится к другому проекту.")
    expected = {"direct": {"client_login"}, "metrika": set(),
                "wordstat": {"auth_scheme", "folder_id"}, "crm": {"origin"}}
    if not isinstance(config, dict) or set(config) != expected[provider]:
        raise ContractError("Неподдерживаемая конфигурация подключения.")
    if credential.value in canonical(config):
        raise ContractError("Секрет обнаружен вне защищённого поля.")
    if provider == "crm" and config["origin"] not in allowed_bridges(context):
        raise ContractError("Адрес CRM bridge не согласован для этого проекта.")
    if provider == "wordstat" and (config["auth_scheme"] not in {"Api-Key", "Bearer"}
            or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", config["folder_id"])):
        raise ContractError("Некорректный тип доступа или folderId.")
    connection_id = uuid.uuid4().hex
    try:
        secrets.put(provider, connection_id, credential)
        def safe_choose(label, options, multiple):
            if credential.value in canonical(options):
                raise ProbeError()
            chosen = choose(label, options, multiple)
            if (not isinstance(chosen, list) or len(set(chosen)) != len(chosen)
                    or any(value not in options for value in chosen) or (not multiple and len(chosen) > 1)
                    or (options and not chosen)):
                raise ContractError("Нужен явный выбор из полученного списка.")
            return chosen
        probe = adapter.probe(provider, credential, config, safe_choose)
        if probe.state not in STATES or credential.value in canonical({"config": config, "resources": probe.resources}):
            raise ProbeError()
        record = {"schema_version": 1, "provider": provider, "connection_id": connection_id,
                  "config": config, "resources": probe.resources, "state": probe.state,
                  "checked_at": None if probe.state == "AWAITING_COST_APPROVAL" else now()}
        context = commit_connection(context, record)
        return context, probe.state
    except PublicationPending:
        raise
    except BaseException:
        secrets.delete(provider, connection_id)
        raise


def wizard(context, terminal=None, secret_store=None, adapter_factory=None):
    terminal = terminal or Terminal()  # Must fail before opening Keychain when no TTY.
    secrets = secret_store or SecretStore(context.project_id)
    factory = adapter_factory or (lambda config: Adapter(Transport(config.get("origin"))))
    with setup_lock(context):
        terminal.say("Настройка проекта " + context.project_id + ". Ключи вводите только в скрытое поле.")
        for provider in ORDER:
            context = load_project(context.workspace, context.project_id)
            answer = terminal.ask(provider + ": настроить (да) или пропустить (Enter)? ")
            if answer not in {"да", ""}:
                raise ContractError("Введите «да» или нажмите Enter.")
            if not answer:
                # Preserve successful existing connections when the user skips them.
                if provider not in context.profile["bindings"] and not has_connection(context, provider):
                    context = commit_connection(context, {"schema_version": 1, "provider": provider,
                        "connection_id": None, "config": {}, "resources": {}, "checked_at": None,
                        "state": "SKIPPED_OPTIONAL" if provider in OPTIONAL else "DEFERRED"})
                continue
            config = {}
            if provider == "direct":
                login = terminal.ask("Логин кабинета (Enter — текущий пользователь OAuth): ")
                if login and not re.fullmatch(r"[a-zA-Z0-9@._-]{1,128}", login):
                    raise ContractError("Некорректный логин кабинета.")
                config = {"client_login": login}
            elif provider == "wordstat":
                scheme = terminal.ask("Тип доступа: Api-Key или Bearer: ")
                folder = terminal.ask("folderId проекта Yandex Cloud: ")
                if scheme not in {"Api-Key", "Bearer"} or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", folder):
                    raise ContractError("Укажите тип доступа и folderId.")
                config = {"auth_scheme": scheme, "folder_id": folder}
            elif provider == "crm":
                options = allowed_bridges(context)
                if not options:
                    terminal.say(STATES["NEEDS_ENDPOINT"])
                    context = commit_connection(context, {"schema_version": 1, "provider": provider,
                        "connection_id": None, "config": {}, "resources": {}, "checked_at": None, "state": "NEEDS_ENDPOINT"})
                    continue
                config = {"origin": terminal.choose("Разрешённый CRM bridge", options, False)[0]}
            credential = terminal.secret()
            context, state = configure(context, provider, credential, config, terminal.choose, secrets, factory(config))
            del credential
            terminal.say(provider + ": " + STATES[state])
    return connection_status(context)
