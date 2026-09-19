"""Транзакционный журнал локальных заданий. Не исполняет рекламу."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .contracts import ContractError, ProjectContext, canonical, confined, identifier, read_json

DB_VERSION = "1"
RUN_KINDS = {"diagnostic", "analysis", "planning", "learning-evaluation"}


class StateError(ContractError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    for suffix in ("", "-journal", "-wal", "-shm"):
        if Path(str(path) + suffix).is_symlink():
            raise StateError("Ссылки вместо файлов SQLite запрещены.")
    connection = sqlite3.connect(path.as_uri() + ("?mode=ro" if read_only else "?mode=rw"),
                                 uri=True, isolation_level=None, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA trusted_schema=OFF")
    return connection


class Store:
    def __init__(self, context: ProjectContext, *, create: bool = False, read_only: bool = False):
        self.context = context
        self.read_only = read_only
        self._initialized = False
        path = context.database
        created = False
        if not path.exists():
            if not create or read_only:
                raise StateError("Состояние не создано. Сначала выполните init.")
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                created = True
            except FileExistsError:
                # Другой процесс мог начать init. Никогда не перезаписывать файл.
                raise StateError("Другой процесс создаёт состояние; повторите проверку позже.")
        self.connection = connect(path, read_only=read_only)
        try:
            if read_only:
                self.connection.execute("BEGIN")
            if created:
                self._initialize()
            self._validate(self.connection, context)
            self._initialized = True
        except Exception:
            self.connection.close()
            raise

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self):
        if self.read_only:
            raise StateError("Хранилище открыто только на чтение.")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if self._initialized:
                self._validate(self.connection, self.context)
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def _initialize(self) -> None:
        with self.transaction():
            for sql in (
                "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                """CREATE TABLE runs(
                    run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('PENDING','RUNNING','COMPLETE')),
                    context_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                """CREATE TABLE steps(
                    run_id TEXT NOT NULL REFERENCES runs(run_id), step_id TEXT NOT NULL,
                    position INTEGER NOT NULL, status TEXT NOT NULL
                    CHECK(status IN ('PENDING','STARTED','COMPLETE','UNKNOWN')),
                    updated_at TEXT NOT NULL, PRIMARY KEY(run_id, step_id), UNIQUE(run_id,position))""",
                """CREATE TABLE events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(run_id),
                    step_id TEXT, action TEXT NOT NULL, created_at TEXT NOT NULL)""",
            ):
                self.connection.execute(sql)
            self.connection.executemany("INSERT INTO metadata VALUES (?,?)", [
                ("schema_version", DB_VERSION), ("project_id", self.context.project_id),
                ("context_hash", self.context.context_hash), ("recovery_required", "0"),
            ])

    @staticmethod
    def _validate(connection: sqlite3.Connection, context: ProjectContext) -> None:
        try:
            meta = dict(connection.execute("SELECT key,value FROM metadata"))
        except sqlite3.DatabaseError as exc:
            raise StateError("Файл не является поддерживаемой базой Директолога.") from exc
        if meta.get("schema_version") != DB_VERSION:
            raise StateError("Версия базы не поддерживается; автоматической миграции нет.")
        if meta.get("project_id") != context.project_id:
            raise StateError("База принадлежит другому проекту.")
        if meta.get("context_hash") != context.context_hash:
            raise StateError("Контекст или привязки изменились; требуется явная сверка, а не переиспользование базы.")

    def _event(self, run_id: str, action: str, step_id: str | None = None) -> None:
        self.connection.execute("INSERT INTO events(run_id,step_id,action,created_at) VALUES (?,?,?,?)",
                                (run_id, step_id, action, now()))

    def create_run(self, request_id: str, kind: str, steps: list[str]) -> dict:
        identifier(request_id)
        if kind not in RUN_KINDS:
            raise StateError("Неподдерживаемый тип локального задания.")
        if not isinstance(steps, list) or not steps or len(steps) > 100:
            raise StateError("Нужен непустой список уникальных шагов, не более 100.")
        for step in steps:
            identifier(step)
        if len(set(steps)) != len(steps):
            raise StateError("Повторяющиеся шаги запрещены.")
        with self.transaction():
            existing = self.connection.execute("SELECT run_id,kind,context_hash FROM runs WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["context_hash"] != self.context.context_hash:
                    raise StateError("Задание относится к прежним привязкам. Создайте новый request_id после сверки.")
                old_steps = [r[0] for r in self.connection.execute(
                    "SELECT step_id FROM steps WHERE run_id=? ORDER BY position", (existing["run_id"],))]
                if existing["kind"] != kind or old_steps != steps:
                    raise StateError("Этот request_id уже связан с другим заданием.")
                run_id = existing["run_id"]
            else:
                run_id = uuid.uuid4().hex
                timestamp = now()
                self.connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                                        (run_id, request_id, kind, "PENDING", self.context.context_hash, timestamp, timestamp))
                self.connection.executemany("INSERT INTO steps VALUES (?,?,?,?,?)",
                                            [(run_id, step, i, "PENDING", timestamp) for i, step in enumerate(steps)])
                self._event(run_id, "CREATED")
        return self.run(run_id)

    def run(self, run_id: str) -> dict:
        identifier(run_id)
        row = self.connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise StateError("Задание в выбранном проекте не найдено.")
        result = dict(row)
        result["steps"] = [dict(r) for r in self.connection.execute(
            "SELECT step_id,position,status,updated_at FROM steps WHERE run_id=? ORDER BY position", (run_id,))]
        result["requires_reconciliation"] = any(s["status"] in {"STARTED", "UNKNOWN"} for s in result["steps"])
        result["project_id"] = self.context.project_id
        result["context_stale"] = result["context_hash"] != self.context.context_hash
        return result

    def _current_run(self, run_id: str) -> dict:
        run = self.run(run_id)
        if run["context_stale"]:
            raise StateError("Привязки изменились; старое задание требует нового плана.")
        return run

    def start_run(self, run_id: str) -> dict:
        with self.transaction():
            current = self._current_run(run_id)
            if current["status"] == "PENDING":
                self.connection.execute("UPDATE runs SET status='RUNNING',updated_at=? WHERE run_id=?", (now(), run_id))
                self._event(run_id, "STARTED")
            elif current["status"] != "RUNNING":
                raise StateError("Завершённое задание нельзя запустить повторно.")
        return self.run(run_id)

    def change_step(self, run_id: str, step_id: str, target: str) -> dict:
        identifier(step_id)
        if target not in {"STARTED", "COMPLETE", "UNKNOWN"}:
            raise StateError("Неподдерживаемый переход шага.")
        with self.transaction():
            run = self._current_run(run_id)
            step = next((s for s in run["steps"] if s["step_id"] == step_id), None)
            if step is None:
                raise StateError("Шаг в выбранном задании не найден.")
            if step["status"] == target:
                return run  # Идемпотентно: не исполняет работу и не пишет повторное событие.
            if run["status"] != "RUNNING":
                raise StateError("Изменять шаги можно только в работающем задании.")
            allowed = {"STARTED": {"PENDING"}, "COMPLETE": {"STARTED"}, "UNKNOWN": {"STARTED"}}
            if step["status"] not in allowed[target]:
                raise StateError("Переход запрещён; неопределённый шаг требует отдельной сверки.")
            if any(s["position"] < step["position"] and s["status"] != "COMPLETE" for s in run["steps"]):
                raise StateError("Предыдущие шаги ещё не завершены.")
            timestamp = now()
            self.connection.execute("UPDATE steps SET status=?,updated_at=? WHERE run_id=? AND step_id=?",
                                    (target, timestamp, run_id, step_id))
            self.connection.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (timestamp, run_id))
            self._event(run_id, target, step_id)
        return self.run(run_id)

    def finish_run(self, run_id: str) -> dict:
        with self.transaction():
            current = self._current_run(run_id)
            if current["status"] == "COMPLETE":
                return current
            if current["status"] != "RUNNING" or any(s["status"] != "COMPLETE" for s in current["steps"]):
                raise StateError("Задание имеет незавершённые шаги.")
            self.connection.execute("UPDATE runs SET status='COMPLETE',updated_at=? WHERE run_id=?", (now(), run_id))
            self._event(run_id, "COMPLETE")
        return self.run(run_id)

    def status(self) -> dict:
        recovery = self.connection.execute("SELECT value FROM metadata WHERE key='recovery_required'").fetchone()
        return {
            "schema_version": 1, "project_id": self.context.project_id, "initialized": True,
            "autonomous_writes": False, "write_mode": "unavailable-foundation-only",
            "restored_requires_review": recovery is None or recovery[0] != "0",
            "runs": [dict(r) for r in self.connection.execute(
                "SELECT run_id,request_id,kind,status,updated_at FROM runs ORDER BY created_at")],
        }

    def backup(self) -> dict:
        if self.read_only:
            raise StateError("Резервная копия требует отдельного явного вызова backup.")
        parent = confined(self.context.directory, "backups")
        parent.mkdir(mode=0o700, exist_ok=True)
        backup_id = "backup-" + uuid.uuid4().hex
        pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=parent))
        destination = pending / "state.sqlite3"
        try:
            # Freeze context validation and snapshot together; setup may rotate bindings.
            self.connection.execute("BEGIN")
            try:
                self._validate(self.connection, self.context)
                with closing(sqlite3.connect(destination)) as connection:
                    self.connection.backup(connection)
            finally:
                self.connection.execute("ROLLBACK")
            os.chmod(destination, 0o600)
            manifest = {
                "schema_version": 1, "project_id": self.context.project_id,
                "context_hash": self.context.context_hash, "created_at": now(),
                "scope": "local-state-only", "database_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
            (pending / "manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
            os.chmod(pending / "manifest.json", 0o600)
            pending.rename(parent / backup_id)
        except BaseException:
            # Только наши два временных файла, никогда чужая backup-папка.
            for name in ("state.sqlite3", "manifest.json"):
                (pending / name).unlink(missing_ok=True)
            pending.rmdir()
            raise
        return {"backup_id": backup_id, **manifest}


def restore(context: ProjectContext, backup_id: str) -> dict:
    identifier(backup_id)
    if context.database.exists():
        raise StateError("Восстановление не перезаписывает существующую базу.")
    source = confined(context.directory, "backups", backup_id)
    manifest = read_json(confined(source, "manifest.json"))
    expected = {"schema_version", "project_id", "context_hash", "created_at", "scope", "database_sha256"}
    if set(manifest) != expected or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise StateError("Неподдерживаемый manifest резервной копии.")
    if manifest["project_id"] != context.project_id or manifest["context_hash"] != context.context_hash:
        raise StateError("Резервная копия принадлежит другому проекту или контексту.")
    if manifest["scope"] != "local-state-only":
        raise StateError("Неподдерживаемый состав резервной копии.")
    database = confined(source, "state.sqlite3")
    if hashlib.sha256(database.read_bytes()).hexdigest() != manifest["database_sha256"]:
        raise StateError("Контрольная сумма резервной копии не совпадает.")
    fd, temporary_name = tempfile.mkstemp(prefix=".restore-", suffix=".sqlite3", dir=context.directory)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(connect(database, read_only=True)) as original:
            Store._validate(original, context)
            if original.execute("PRAGMA quick_check").fetchone()[0] != "ok" or original.execute("PRAGMA foreign_key_check").fetchone():
                raise StateError("Целостность резервной копии не подтверждена.")
            with closing(sqlite3.connect(temporary)) as restored:
                original.backup(restored)
                restored.execute("INSERT OR REPLACE INTO metadata VALUES ('recovery_required','1')")
                restored.commit()
        os.link(temporary, context.database)  # Атомарная публикация, не заменяет появившийся файл.
    finally:
        temporary.unlink(missing_ok=True)
    with Store(context, read_only=True) as store:
        return store.status()
