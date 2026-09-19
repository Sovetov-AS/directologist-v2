"""Версионированные evidence и детерминированные расчёты без бизнес-догадок."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import os
import re
import tempfile
from pathlib import Path

from .contracts import ContractError, canonical, digest, confined, identifier, read_json
from .storage import Store

METRICS = {
    "direct": {"impressions", "clicks", "cost", "conversions"},
    "metrika": {"visits", "goal_reaches"},
    "crm": {"direct_leads", "qualified_leads", "won_deals", "unmatched_leads", "unlinked_deals"},
}
FIELDS = {"schema_version", "project_id", "context_hash", "source", "period", "goal_id", "attribution", "source_timezone",
          "timezone", "scope", "units", "fetched_at", "expires_at", "completeness", "sampled", "rows", "sha256"}


def number(value, *, signed=False):
    if value is None or value in ("", "--"):
        return None
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ContractError("Число должно быть десятичной строкой или целым; float запрещён.")
    text = str(value)
    if not re.fullmatch(r"-?[0-9]{1,24}(?:\.[0-9]{1,12})?", text):
        raise ContractError("Некорректное десятичное число.")
    result = Decimal(text)
    if not signed and result < 0:
        raise ContractError("Отрицательный счётчик или расход недопустим.")
    return result


def decimal_text(value, *, signed=False):
    result = number(value, signed=signed)
    return format(result, "f") if result is not None else None


def period(start, end):
    try:
        if not all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in (start, end)):
            raise ValueError()
        if date.fromisoformat(start) > date.fromisoformat(end):
            raise ValueError()
    except ValueError:
        raise ContractError("Некорректный период отчёта.") from None
    return {"from": start, "to": end}


def instant(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except (TypeError, ValueError):
        raise ContractError("Время evidence должно содержать часовой пояс.") from None


def currency_map(value, *, signed=False):
    if value is None:
        return None
    if not isinstance(value, dict) or any(not re.fullmatch(r"[A-Z]{3}", k) for k in value):
        raise ContractError("Некорректный разрез валют.")
    return {k: decimal_text(v, signed=signed) for k, v in value.items()}


def validate_bundle(bundle, context=None, *, now=None, require_fresh=True):
    if not isinstance(bundle, dict) or set(bundle) != FIELDS or type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ContractError("Неподдерживаемый EvidenceBundle.")
    if digest({k: v for k, v in bundle.items() if k != "sha256"}) != bundle["sha256"]:
        raise ContractError("Хеш evidence не совпадает.")
    identifier(bundle["project_id"])
    if not isinstance(bundle["context_hash"], str) or not re.fullmatch(r"[0-9a-f]{64}", bundle["context_hash"]):
        raise ContractError("Некорректный хеш контекста.")
    if context and (bundle["project_id"] != context.project_id or bundle["context_hash"] != context.context_hash):
        raise ContractError("Evidence относится к другому проекту или версии привязок.")
    if not isinstance(bundle["period"], dict) or set(bundle["period"]) != {"from", "to"}:
        raise ContractError("Нет контекста периода.")
    period(bundle["period"]["from"], bundle["period"]["to"])
    source = bundle["source"]
    if source not in METRICS or bundle["completeness"] not in {"COMPLETE", "PARTIAL", "UNKNOWN"}:
        raise ContractError("Неизвестный источник или полнота.")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        ZoneInfo(bundle["timezone"])
        if bundle["source_timezone"] is not None:
            ZoneInfo(bundle["source_timezone"])
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise ContractError("Некорректный часовой пояс evidence.") from None
    if source in {"direct", "metrika"}:
        if not isinstance(bundle["goal_id"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", bundle["goal_id"]):
            raise ContractError("Требуется явная цель evidence.")
        supported = {"AUTO", "FCCD", "LC", "LSCCD"} if source == "direct" else {"automatic", "lastsign", "last", "first"}
        if bundle["attribution"] not in supported:
            raise ContractError("Неподдерживаемая атрибуция.")
    elif bundle["goal_id"] is not None or bundle["attribution"] != "crm-configured-source":
        raise ContractError("CRM не должна подменяться рекламной целью.")
    if bundle["sampled"] is not None and type(bundle["sampled"]) is not bool:
        raise ContractError("Неизвестное семплирование.")
    units = bundle["units"]
    if not isinstance(units, dict) or set(units) != {"currency", "money", "vat", "discount"}:
        raise ContractError("Единицы evidence обязательны.")
    if (units["money"] != "major" or not isinstance(units["currency"], str)
            or not re.fullmatch(r"[A-Z]{3}|NONE|MULTI", units["currency"])
            or units["vat"] not in {"included", "excluded", "unknown", "na"}
            or units["discount"] not in {"included", "excluded", "unknown", "na"}):
        raise ContractError("Неподдерживаемые единицы evidence.")
    if ((source == "direct" and (not re.fullmatch(r"[A-Z]{3}", units["currency"]) or units["vat"] not in {"included", "excluded"} or units["discount"] != "excluded"))
            or (source == "metrika" and units != {"currency": "NONE", "money": "major", "vat": "na", "discount": "na"})
            or (source == "crm" and units != {"currency": "MULTI", "money": "major", "vat": "unknown", "discount": "unknown"})):
        raise ContractError("Единицы не соответствуют источнику.")
    fetched, expires = instant(bundle["fetched_at"]), instant(bundle["expires_at"])
    clock = now or datetime.now(timezone.utc)
    if expires <= fetched or fetched > clock + timedelta(seconds=60):
        raise ContractError("Некорректное время evidence.")
    if require_fresh and expires <= clock:
        raise ContractError("Evidence устарел; повторите сбор.")
    if not isinstance(bundle["scope"], dict) or len(canonical(bundle["scope"])) > 10000:
        raise ContractError("Некорректная область отчёта.")
    scope = bundle["scope"]
    if source == "direct":
        if set(scope) != {"campaign_ids"} or not isinstance(scope["campaign_ids"], list) or not scope["campaign_ids"] or any(not isinstance(v,str) or not re.fullmatch(r"[1-9][0-9]{0,19}",v) for v in scope["campaign_ids"]):
            raise ContractError("Область кампаний обязательна.")
    elif source == "metrika":
        if set(scope) != {"counter_id", "traffic"} or not re.fullmatch(r"[1-9][0-9]{0,19}",str(scope["counter_id"])) or scope["traffic"] != "all-site-traffic":
            raise ContractError("Область Метрики обязательна.")
    elif (set(scope) != {"cohort", "missing_configuration_count"} or scope["cohort"] != "lead_or_deal_created_in_period_with_current_entity_state"
          or type(scope["missing_configuration_count"]) is not int or scope["missing_configuration_count"] < 0):
        raise ContractError("Контекст CRM обязателен.")
    if not isinstance(bundle["rows"], list) or len(bundle["rows"]) > 100000:
        raise ContractError("Некорректный набор строк.")
    if source != "direct" and len(bundle["rows"]) != 1:
        raise ContractError("Ожидался один агрегат источника.")
    seen = set()
    for row in bundle["rows"]:
        extra = {"date", "campaign_id"} if source == "direct" else ({"revenue_by_currency", "margin_by_currency"} if source == "crm" else set())
        if not isinstance(row, dict) or set(row) != METRICS[source] | extra:
            raise ContractError("Неподдерживаемые поля строки; персональные поля запрещены.")
        for metric in METRICS[source]:
            number(row[metric])
        if source == "direct":
            day = row["date"]
            period(day, day)
            if not bundle["period"]["from"] <= day <= bundle["period"]["to"] or not re.fullmatch(r"[0-9]{1,20}", row["campaign_id"]):
                raise ContractError("Строка вне периода или кампании.")
            if row["campaign_id"] not in scope["campaign_ids"]:
                raise ContractError("Строка вне выбранных кампаний.")
            identity = (day, row["campaign_id"])
            if identity in seen:
                raise ContractError("Повторяющиеся строки отчёта.")
            seen.add(identity)
        if source == "crm":
            currency_map(row["revenue_by_currency"])
            currency_map(row["margin_by_currency"], signed=True)
    return bundle


def bundle(context, source, rows, *, start, end, goal_id, attribution, scope, units,
           completeness="COMPLETE", sampled=False, ttl_seconds=3600, now=None, source_timezone=None):
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
        raise ContractError("TTL evidence должен быть от 1 до 86400 секунд.")
    clock = now or datetime.now(timezone.utc)
    result = {"schema_version": 1, "project_id": context.project_id, "context_hash": context.context_hash,
              "source": source, "period": period(start, end), "goal_id": goal_id, "attribution": attribution,
              "timezone": context.profile["timezone"], "source_timezone": source_timezone, "scope": scope, "units": units,
              "fetched_at": clock.isoformat(), "expires_at": (clock + timedelta(seconds=ttl_seconds)).isoformat(),
              "completeness": completeness, "sampled": sampled, "rows": rows}
    result["sha256"] = digest(result)
    return validate_bundle(result, context, now=clock)


def save_bundle(context, data):
    validate_bundle(data, context)
    if len(canonical(data).encode()) > 1024 * 1024 - 1:
        raise ContractError("Evidence превышает 1 MiB; сузьте период/область сбора.")
    parent = confined(context.directory, "artifacts", "evidence")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = confined(parent, data["sha256"] + ".json")
    with Store(context, create=True) as store:
        with store.transaction():
            fd, name = tempfile.mkstemp(prefix=".pending-", dir=parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(canonical(data) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(name, target)
                except FileExistsError:
                    if read_json(target) != data:
                        raise ContractError("Артефакт с этим ID повреждён.") from None
            finally:
                Path(name).unlink(missing_ok=True)
            store.connection.execute("CREATE TABLE IF NOT EXISTS evidence_index(id TEXT PRIMARY KEY, source TEXT, fetched_at TEXT)")
            store.connection.execute("INSERT OR IGNORE INTO evidence_index VALUES (?,?,?)", (data["sha256"], data["source"], data["fetched_at"]))
    return {"evidence_id": data["sha256"], "path": str(target), "source": data["source"], "completeness": data["completeness"]}


def load_bundle(context, evidence_id):
    if not isinstance(evidence_id, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_id):
        raise ContractError("Некорректный evidence ID.")
    data = read_json(confined(context.directory, "artifacts", "evidence", evidence_id + ".json"))
    if data.get("sha256") != evidence_id:
        raise ContractError("Evidence ID не совпадает с содержимым.")
    return validate_bundle(data, context)


def merge(bundles):
    if not bundles:
        raise ContractError("Нет evidence для объединения.")
    for data in bundles:
        validate_bundle(data)
    keys = FIELDS - {"rows", "sha256", "fetched_at", "expires_at"}
    first = bundles[0]
    if first["source"] != "direct" or any(any(data[k] != first[k] for k in keys) for data in bundles):
        raise ContractError("Несовместимые контексты evidence нельзя объединять.")
    result = {**first, "rows": [row for data in bundles for row in data["rows"]],
              "fetched_at": min((data["fetched_at"] for data in bundles), key=instant),
              "expires_at": min((data["expires_at"] for data in bundles), key=instant)}
    result["sha256"] = digest({k: v for k, v in result.items() if k != "sha256"})
    return validate_bundle(result)


def summarize(data):
    validate_bundle(data)
    with localcontext() as precision:
        precision.prec = 64
        totals = {}
        for metric in sorted(METRICS[data["source"]]):
            values = [number(row[metric]) for row in data["rows"]]
            totals[metric] = None if (any(v is None for v in values) or data["completeness"] != "COMPLETE") else sum(values, Decimal(0))
        def ratio(numerator, denominator, factor=1):
            top, bottom = totals[numerator], totals[denominator]
            return None if top is None or bottom is None or bottom == 0 else format((top / bottom * factor).quantize(Decimal("0.000001")), "f")
        ratios = {}
        if data["source"] == "direct":
            ratios = {"ctr_percent": ratio("clicks", "impressions", 100), "cpc": ratio("cost", "clicks"),
                      "conversion_rate_percent": ratio("conversions", "clicks", 100),
                      "cost_per_ad_conversion": ratio("cost", "conversions")}
        result = {"schema_version": 1, "evidence_id": data["sha256"], "source": data["source"],
                  "project_id": data["project_id"], "period": data["period"], "goal_id": data["goal_id"],
                  "attribution": data["attribution"], "units": data["units"], "completeness": data["completeness"],
                  "scope": data["scope"], "timezone": data["timezone"], "source_timezone": data["source_timezone"],
                  "fetched_at": data["fetched_at"], "expires_at": data["expires_at"],
                  "sampled": data["sampled"], "totals": {k: format(v, "f") if v is not None else None for k, v in totals.items()},
                  "ratios": ratios, "unknown_cells": sum(number(row[m]) is None for row in data["rows"] for m in METRICS[data["source"]]),
                  "autonomous_writes": False}
        if data["source"] == "crm":
            result["revenue_by_currency"] = data["rows"][0]["revenue_by_currency"] if data["completeness"] == "COMPLETE" else None
            result["margin_by_currency"] = data["rows"][0]["margin_by_currency"] if data["completeness"] == "COMPLETE" else None
        return result


def compare(bundles):
    if not bundles:
        raise ContractError("Нет evidence.")
    summaries = [summarize(data) for data in bundles]
    first = bundles[0]
    if len({b["source"] for b in bundles}) != len(bundles):
        raise ContractError("Для сравнения нужен один evidence на источник.")
    if any(any(b[k] != first[k] for k in ("project_id", "context_hash", "period", "timezone")) for b in bundles):
        raise ContractError("Проект, привязки или периоды не совпадают.")
    # No cross-source division: all-site goals and CRM creation cohorts are not campaign clicks.
    return {"schema_version": 1, "sources": summaries, "crm_available": any(b["source"] == "crm" for b in bundles),
            "cross_source_attribution": "NOT_ESTABLISHED", "crm_cpl": None, "cac": None,
            "limitations": ["Конверсии рекламы, цели сайта и CRM-лиды не эквивалентны.",
                            "Часовой пояс проекта не подтверждает границы суток источника; source_timezone=null означает неизвестный пояс источника.",
                            "Связь кампаний, событий и CRM-когорты не доказана; суммы между источниками не объединены."],
            "autonomous_writes": False}
