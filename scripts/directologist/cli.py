"""Локальные команды и явный read-only сбор; рекламная запись отсутствует."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .contracts import ContractError, load_project
from .storage import RUN_KINDS, Store, restore


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # Не повторять сырой аргумент: ошибочно переданный секрет не должен попасть в лог.
        raise ContractError("Некорректные аргументы команды. Используйте --help.")


def parser() -> argparse.ArgumentParser:
    result = Parser(description="Директолог: локальный профиль, журнал, резервные копии. API-запись отсутствует.")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[2],
                        help="Корень проекта; по умолчанию каталог установленного исходника, не cwd.")
    result.add_argument("--project", required=True, help="Явный ID проекта, например my-project.")
    commands = result.add_subparsers(dest="command", required=True)
    from .direct_cli import add_parser
    add_parser(commands)
    project = commands.add_parser("project-create", help="Создать отдельный проект без аккаунтов и разрешений.")
    project.add_argument("--name", required=True)
    project.add_argument("--timezone", required=True)
    lesson = commands.add_parser("lesson", help="Предложить урок или проверить его состояние.")
    lesson.add_argument("operation", choices=("init","propose","status","evaluate","rollback"))
    lesson.add_argument("--input", type=Path)
    lesson.add_argument("--candidate-id")
    lesson.add_argument("--version")
    lesson.add_argument("--expected-head")
    commands.add_parser("capabilities", help="Доступные и отключённые операции.")
    plan = commands.add_parser("plan-validate", help="Проверить точный план без записи.")
    plan.add_argument("--input", type=Path, required=True)
    commands.add_parser("context", help="Восстановить контекст рекламной работы и версии методик.")
    proposal = commands.add_parser("propose", help="Проверить и сохранить предложение без исполнения.")
    proposal.add_argument("--input", type=Path, required=True)
    setup_parser=commands.add_parser("setup", help="Единый мастер подключений; только в пользовательском терминале.")
    setup_parser.add_argument("--direct-environment",choices=("production","sandbox"),default="production")
    connections = commands.add_parser("connections", help="Состояние площадок и восстановление публикации привязок.")
    connections.add_argument("operation", choices=("status", "recover"))
    collection = commands.add_parser("collect", help="Получить read-only отчёт из настроенного источника.")
    collection.add_argument("--source", required=True, choices=("direct", "metrika", "crm"))
    collection.add_argument("--date-from", required=True)
    collection.add_argument("--date-to", required=True)
    collection.add_argument("--goal")
    collection.add_argument("--attribution")
    collection.add_argument("--vat", choices=("included", "excluded"), default="excluded")
    collection.add_argument("--ttl-seconds", type=int, default=3600)
    analysis = commands.add_parser("analyze", help="Проверить evidence и рассчитать минимальный экспорт модели.")
    analysis.add_argument("--evidence", action="append", required=True)
    wordstat = commands.add_parser("wordstat", help="Подготовить/проверить очередь без платных API-вызовов.")
    wordstat.add_argument("operation", choices=("enqueue", "status"))
    wordstat.add_argument("--job-id", required=True)
    wordstat.add_argument("--input", type=Path)
    for name, description in (("validate", "Проверить профиль без изменения файлов."),
                              ("status", "Показать состояние, не создавая БД."),
                              ("init", "Создать локальную БД, не перезаписывая существующую."),
                              ("backup", "Создать копию только локальной БД, без секретов.")):
        commands.add_parser(name, help=description)
    recovery = commands.add_parser("restore", help="Восстановить БД только если текущая отсутствует.")
    recovery.add_argument("--backup-id", required=True)
    run = commands.add_parser("run", help="Управление журналом локальных заданий.")
    operations = run.add_subparsers(dest="operation", required=True)
    create = operations.add_parser("create")
    create.add_argument("--request-id", required=True)
    create.add_argument("--kind", required=True, choices=sorted(RUN_KINDS))
    create.add_argument("--step", action="append", required=True)
    for name in ("show", "start", "finish"):
        operation = operations.add_parser(name)
        operation.add_argument("--run-id", required=True)
    step = commands.add_parser("step", help="Отметить локальный шаг; это не исполнение рекламного действия.")
    step.add_argument("operation", choices=("start", "finish", "unknown"))
    step.add_argument("--run-id", required=True)
    step.add_argument("--step-id", required=True)
    return result


def execute(args: argparse.Namespace) -> dict:
    if args.command == "project-create":
        from .onboarding import create
        return create(args.workspace, args.project, args.name, args.timezone)
    context = load_project(args.workspace, args.project)
    if args.command == "direct":
        from .direct_cli import execute as direct_execute
        return direct_execute(context,args)
    if args.command == "lesson":
        from .learning import Learning
        from .contracts import read_json
        with Learning(context) as learning:
            if args.operation == "init":
                from .decisions import knowledge
                return {"active_version": learning.head(), "knowledge": knowledge(context)}
            if args.operation == "propose":
                if args.input is None: raise ContractError("Для урока нужен --input.")
                return learning.propose(read_json(args.input))
            if args.operation == "rollback":
                if not args.version or not args.expected_head: raise ContractError("Для отката нужны --version и --expected-head.")
                return learning.rollback(args.version, args.expected_head)
            if not args.candidate_id: raise ContractError("Нужен --candidate-id.")
            if args.operation == "evaluate":
                from .learning import evaluate_imported
                if not args.input: raise ContractError("Нужен --input с независимыми ответами.")
                return evaluate_imported(learning, args.candidate_id, read_json(args.input))
            return learning.status(args.candidate_id)
    if args.command == "capabilities":
        from .policy import capabilities
        return capabilities()
    if args.command == "plan-validate":
        from .planning import validate
        from .contracts import read_json
        value = validate(context, read_json(args.input))
        return {"plan_hash": value["sha256"], "valid": True, "execution_authorized": False}
    if args.command == "context":
        from .decisions import recover
        return recover(context)
    if args.command == "propose":
        from .decisions import save
        from .contracts import read_json
        return save(context, read_json(args.input))
    if args.command == "collect":
        from .adapters.reports import collect_configured
        return collect_configured(context, source=args.source, start=args.date_from, end=args.date_to,
                                  goal=args.goal, attribution=args.attribution, vat=args.vat, ttl_seconds=args.ttl_seconds)
    if args.command == "analyze":
        from .analytics import load_bundle, summarize, merge, compare
        data = [load_bundle(context, value) for value in args.evidence]
        if len(data) == 1:
            return summarize(data[0])
        return summarize(merge(data)) if len({item["source"] for item in data}) == 1 else compare(data)
    if args.command == "wordstat":
        from .contracts import read_json
        from .wordstat_queue import WordstatQueue
        if args.operation == "enqueue":
            if args.input is None:
                raise ContractError("Для enqueue нужен --input с несекретным пакетом задач.")
            spec = read_json(args.input)
            if set(spec) != {"schema_version", "requests", "request_limit", "ttl_seconds"} or type(spec["schema_version"]) is not int or spec["schema_version"] != 1:
                raise ContractError("Неподдерживаемый пакет Wordstat.")
        with WordstatQueue(context) as queue:
            if args.operation == "status":
                return queue.status(args.job_id)
            return queue.enqueue(args.job_id, spec["requests"], request_limit=spec["request_limit"], ttl_seconds=spec["ttl_seconds"])
    if args.command in {"setup", "connections"}:
        from .setup import wizard, connection_status, recover_connections
        if args.command == "setup":
            return wizard(context,direct_environment=args.direct_environment)
        return {"status": connection_status, "recover": recover_connections}[args.operation](context)
    if args.command == "validate":
        return {"schema_version": 1, "project_id": context.project_id, "valid": True,
                "binding_version": context.profile["binding_version"], "context_hash": context.context_hash,
                "providers": sorted(context.profile["bindings"]), "autonomous_writes": False}
    if args.command == "status" and not context.database.exists():
        return {"schema_version": 1, "project_id": context.project_id, "initialized": False,
                "autonomous_writes": False, "write_mode": "unavailable-foundation-only", "runs": []}
    if args.command == "restore":
        return restore(context, args.backup_id)
    read_only = args.command == "status" or (args.command == "run" and args.operation == "show")
    with Store(context, create=args.command == "init", read_only=read_only) as store:
        if args.command in {"init", "status"}:
            return store.status()
        if args.command == "backup":
            return store.backup()
        if args.command == "run":
            if args.operation == "create":
                return store.create_run(args.request_id, args.kind, args.step)
            return {"show": store.run, "start": store.start_run, "finish": store.finish_run}[args.operation](args.run_id)
        target = {"start": "STARTED", "finish": "COMPLETE", "unknown": "UNKNOWN"}[args.operation]
        return store.change_step(args.run_id, args.step_id, target)


def main(argv: list[str] | None = None) -> int:
    try:
        result = execute(parser().parse_args(argv))
    except ContractError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        # Не выводить provider payload, файловое содержимое и исключения SQL.
        print(json.dumps({"status": "ERROR", "error": "Ошибка локального хранилища или доступа к файлам."}, ensure_ascii=False), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print(json.dumps({"status": "ERROR", "error": "Действие прервано пользователем."}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        # Last boundary: unexpected credential/backend exceptions must not become tracebacks.
        print(json.dumps({"status": "ERROR", "error": "Не удалось завершить команду; подробности с секретами не выводятся."}, ensure_ascii=False), file=sys.stderr)
        return 3
    print(json.dumps({"status": "OK", "result": result}, ensure_ascii=False))
    return 0
