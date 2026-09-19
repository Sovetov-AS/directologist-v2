"""Read-only отчёты. Raw CRM не сохраняется и не экспортируется модели."""
import csv
import io
import json
import re
import urllib.error
import urllib.parse
from decimal import Decimal, localcontext

from . import ProbeError, Transport, direct_result
from ..analytics import bundle, decimal_text, currency_map, period
from ..contracts import ContractError, canonical, digest


class ReadPending(ContractError):
    def __init__(self, state, retry_after=60):
        self.state = state
        self.retry_after = retry_after
        super().__init__("Отчёт не готов или лимит исчерпан; повтор разрешён после указанного интервала.")


def retry_seconds(headers, field="Retry-After", default=60):
    value = headers.get(field, "")
    return min(int(value), 86400) if str(value).isdigit() and int(value) > 0 else default


class ReadHTTP:
    def __init__(self, bridge_origin=None):
        self.transport = Transport(bridge_origin)

    def request(self, source, credential, *, body=None, query=None, client_login=None, bridge_id=None, auth_scheme="Bearer"):
        headers = {"Authorization": auth_scheme + " " + credential.value, "Accept-Language": "ru",
                   "User-Agent": "directologist/0.1", "Content-Type": "application/json"}
        if source == "direct":
            url = "https://api.direct.yandex.com/json/v501/reports"
            if not client_login or not re.fullmatch(r"[a-zA-Z0-9@._-]{1,128}", client_login):
                raise ContractError("Нет проверенного Client-Login.")
            headers.update({"Client-Login": client_login, "processingMode": "auto", "skipReportHeader": "true",
                            "skipColumnHeader": "false", "skipReportSummary": "true"})
        elif source == "metrika":
            url = "https://api-metrika.yandex.net/stat/v1/data"
            headers["Authorization"] = "OAuth " + credential.value
            if body is not None:
                raise ContractError("Метрика допускает только чтение.")
        elif source == "crm":
            if not self.transport.bridge_origin or not re.fullmatch(r"[a-zA-Z0-9._-]{1,128}", bridge_id or "") or body is not None:
                raise ContractError("Не выбран разрешённый CRM bridge.")
            url = self.transport.bridge_origin + "/v1/portals/" + urllib.parse.quote(bridge_id, safe="") + "/decision-data"
        elif source == "wordstat":
            if auth_scheme not in {"Api-Key", "Bearer"}:
                raise ContractError("Неподдерживаемый тип Wordstat доступа.")
            url = "https://searchapi.api.cloud.yandex.net/v2/wordstat/topRequests"
        else:
            raise ContractError("Неизвестный источник отчёта.")
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, data=canonical(body).encode() if body is not None else None, headers=headers,
                                         method="POST" if body is not None else "GET")
        try:
            with self.transport.opener.open(request, timeout=30) as response:
                status = response.status
                if source == "direct" and status in {201, 202}:
                    raise ReadPending("PENDING", retry_seconds(response.headers, "retryIn"))
                raw = response.read(8 * 1024 * 1024 + 1)
            if status != 200 or len(raw) > 8 * 1024 * 1024 or credential.value.encode() in raw:
                raise ProbeError()
            if source == "direct":
                if raw.lstrip().startswith(b"{"):
                    direct_result(json.loads(raw))
                    raise ProbeError()
                return raw.decode("utf-8-sig")
            result = json.loads(raw, parse_float=Decimal)
            if not isinstance(result, dict):
                raise ProbeError()
            return result
        except urllib.error.HTTPError as exc:
            status, retry = exc.code, retry_seconds(exc.headers)
            # Parse only numeric Direct error code; never retain provider message/URL.
            code = None
            if source == "direct":
                try:
                    code = json.loads(exc.read(4096)).get("error", {}).get("error_code")
                except Exception:
                    pass
            exc.close()
            if status == 429 or code in {152, 506}:
                raise ReadPending("QUOTA", retry) from None
            if code is not None:
                direct_result({"error": {"error_code": code}})
            raise ProbeError({401: "INVALID_CREDENTIAL", 403: "FORBIDDEN"}.get(status, "NETWORK" if status >= 500 else "RESPONSE_ERROR")) from None
        except (ReadPending, ProbeError, ContractError):
            raise
        except (ValueError, UnicodeError):
            raise ProbeError() from None
        except Exception:
            raise ProbeError("NETWORK") from None


def direct_payload(campaign_ids, start, end, goal, attribution, vat):
    period(start, end)
    if (not campaign_ids or any(not re.fullmatch(r"[1-9][0-9]{0,19}", str(v)) for v in campaign_ids)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", goal or "") or attribution not in {"AUTO", "FCCD", "LC", "LSCCD"}
            or vat not in {"included", "excluded"}):
        raise ContractError("Укажите кампании, цель, атрибуцию и учёт НДС.")
    params = {"SelectionCriteria": {"DateFrom": start, "DateTo": end,
              "Filter": [{"Field": "CampaignId", "Operator": "IN", "Values": sorted(campaign_ids)}]},
              "Goals": [goal], "AttributionModels": [attribution],
              "FieldNames": ["Date", "CampaignId", "Impressions", "Clicks", "Cost", "Conversions"],
              "ReportType": "CAMPAIGN_PERFORMANCE_REPORT", "DateRangeType": "CUSTOM_DATE", "Format": "TSV",
              "IncludeVAT": "YES" if vat == "included" else "NO", "IncludeDiscount": "NO"}
    params["ReportName"] = "directologist-" + digest(params)[:32]
    return {"params": params}


def parse_direct(raw, goal, attribution):
    conversion = f"Conversions_{goal}_{attribution}"
    fields = {"Date", "CampaignId", "Impressions", "Clicks", "Cost", conversion}
    reader = csv.DictReader(io.StringIO(raw), delimiter="\t")
    if not reader.fieldnames or len(reader.fieldnames) != len(fields) or set(reader.fieldnames) != fields:
        raise ContractError("Колонки Direct не соответствуют заявленной цели и атрибуции.")
    rows = []
    for record in reader:
        if None in record:
            raise ContractError("Повреждённая TSV-строка.")
        cost = decimal_text(record["Cost"])
        with localcontext() as precision:
            precision.prec = 64
            cost = format(Decimal(cost) / Decimal(1000000), "f") if cost is not None else None
        rows.append({"date": record["Date"], "campaign_id": record["CampaignId"],
                     "impressions": decimal_text(record["Impressions"]), "clicks": decimal_text(record["Clicks"]),
                     "cost": cost,
                     "conversions": decimal_text(record[conversion])})
    return rows


def normalize_metrika(raw, goal):
    metrics = ["ym:s:visits", f"ym:s:goal{goal}reaches"]
    if raw.get("query", {}).get("metrics") != metrics or not isinstance(raw.get("totals"), list) or len(raw["totals"]) != 2:
        raise ContractError("Ответ Метрики не подтверждает выбранные метрики.")
    sampled = raw.get("sampled")
    if sampled is not None and type(sampled) is not bool:
        raise ContractError("Неподдерживаемый признак семплирования.")
    return [{"visits": decimal_text(raw["totals"][0]), "goal_reaches": decimal_text(raw["totals"][1])}], sampled


def normalize_crm(raw, start, end):
    meta, summary, quality = raw.get("meta", {}), raw.get("summary", {}), raw.get("dataQuality", {})
    cohort = "lead_or_deal_created_in_period_with_current_entity_state"
    if (meta.get("readOnly") is not True or meta.get("dateFrom") != start or meta.get("dateTo") != end or meta.get("cohort") != cohort):
        raise ContractError("CRM bridge вернул несовместимый период или когорту.")
    missing = quality.get("missingConfiguration")
    if not isinstance(missing, list):
        raise ContractError("Нет данных о полноте конфигурации CRM.")
    row = {"direct_leads": decimal_text(summary.get("directLeads")), "qualified_leads": decimal_text(summary.get("qualifiedDirectLeads")),
           "won_deals": decimal_text(summary.get("wonProjectSalesDeals")), "unmatched_leads": decimal_text(quality.get("leadsNotMatchedToDirect")),
           "unlinked_deals": decimal_text(quality.get("directDealsWithoutLeadLink")),
           "revenue_by_currency": currency_map(summary.get("wonRevenueByCurrency")),
           "margin_by_currency": currency_map(summary.get("wonMarginByCurrency"), signed=True)}
    return [row], {"cohort": cohort, "missing_configuration_count": len(missing)}


def collect(context, source, credential, config, *, start, end, goal=None, attribution=None, vat="excluded", ttl_seconds=3600, http=None):
    period(start, end)
    bindings = context.profile["bindings"]
    if source not in {"direct", "metrika", "crm"} or source not in bindings:
        raise ContractError("Источник не подключён для этого проекта.")
    resources = bindings[source]["resources"]
    http = http or ReadHTTP(config.get("origin"))
    sampled = False
    if source == "direct":
        payload = direct_payload(resources.get("campaign_ids"), start, end, goal, attribution, vat)
        account = direct_result(http.transport.request("https://api.direct.yandex.com/json/v5/clients", credential,
            body={"method": "get", "params": {"FieldNames": ["Currency"]}}, client_login=resources["client_login"]))
        clients = account.get("Clients", [])
        if len(clients) != 1 or not re.fullmatch(r"[A-Z]{3}", clients[0].get("Currency", "")):
            raise ContractError("Не подтверждена валюта кабинета Директа.")
        raw = http.request(source, credential, body=payload, client_login=resources["client_login"])
        rows = parse_direct(raw, goal, attribution)
        scope = {"campaign_ids": sorted(resources["campaign_ids"])}
        units = {"currency": clients[0]["Currency"], "money": "major", "vat": vat, "discount": "excluded"}
    elif source == "metrika":
        if goal not in resources.get("goal_ids", []) or attribution not in {"automatic", "last"}:
            raise ContractError("Цель или атрибуция Метрики не выбрана в профиле.")
        query = {"ids": resources["counter_id"], "date1": start, "date2": end,
                 "metrics": f"ym:s:visits,ym:s:goal{goal}reaches", "accuracy": "full", "attribution": attribution}
        raw = http.request(source, credential, query=query)
        echo = raw.get("query", {})
        for key in ("date1", "date2", "attribution"):
            if key in echo and echo[key] != query[key]:
                raise ContractError("Ответ Метрики относится к другому контексту.")
        if "ids" in echo and [str(v) for v in echo["ids"]] != [resources["counter_id"]]:
            raise ContractError("Ответ Метрики относится к другому счётчику.")
        rows, sampled = normalize_metrika(raw, goal)
        scope = {"counter_id": resources["counter_id"], "traffic": "all-site-traffic"}
        units = {"currency": "NONE", "money": "major", "vat": "na", "discount": "na"}
    else:
        raw = http.request(source, credential, query={"date_from": start, "date_to": end}, bridge_id=resources["bridge_id"])
        rows, scope = normalize_crm(raw, start, end)
        goal, attribution = None, "crm-configured-source"
        units = {"currency": "MULTI", "money": "major", "vat": "unknown", "discount": "unknown"}
    return bundle(context, source, rows, start=start, end=end, goal_id=goal, attribution=attribution,
                  scope=scope, units=units, sampled=sampled, ttl_seconds=ttl_seconds)


def collect_configured(context, **options):
    """Resolve only this project's current connection; pending report delay persists."""
    import time
    from ..analytics import save_bundle
    from ..secrets import SecretStore
    from ..setup import allowed_bridges
    from ..storage import Store
    source = options["source"]
    period(options["start"], options["end"])
    with Store(context) as store:
        with store.transaction():
            if not store.connection.execute("SELECT name FROM sqlite_master WHERE name='connections' AND type='table'").fetchone():
                raise ContractError("Сначала выполните setup.")
            row = store.connection.execute("SELECT record FROM connections WHERE provider=?", (source,)).fetchone()
            record = json.loads(row[0]) if row else {}
            binding = context.profile["bindings"].get(source)
            if not binding or record.get("state") != "CHECKED" or record.get("connection_id") != binding["connection_id"] or record.get("resources") != binding["resources"]:
                raise ContractError("Нет проверенного подключения к выбранному источнику.")
            config = record["config"]
            if source == "crm" and config.get("origin") not in allowed_bridges(context):
                raise ContractError("CRM bridge больше не разрешён.")
            request_key = digest({"context_hash": context.context_hash, "options": options})
            store.connection.execute("CREATE TABLE IF NOT EXISTS report_waits(key TEXT PRIMARY KEY, retry_at REAL, state TEXT)")
            wait = store.connection.execute("SELECT retry_at,state FROM report_waits WHERE key=?", (request_key,)).fetchone()
            if wait and wait[0] > time.time():
                return {"state": wait[1], "retry_after_seconds": max(1, int(wait[0] - time.time())), "autonomous_writes": False}
    secret = SecretStore(context.project_id).get(source, binding["connection_id"])
    if secret is None:
        raise ContractError("Ключ подключения отсутствует в Keychain.")
    try:
        data = collect(context, credential=secret, config=config, **options)
    except ReadPending as waiting:
        with Store(context) as store, store.transaction():
            store.connection.execute("INSERT OR REPLACE INTO report_waits VALUES (?,?,?)", (request_key, time.time() + waiting.retry_after, waiting.state))
        return {"state": waiting.state, "retry_after_seconds": waiting.retry_after, "autonomous_writes": False}
    finally:
        del secret
    result = save_bundle(context, data)
    with Store(context) as store, store.transaction():
        store.connection.execute("DELETE FROM report_waits WHERE key=?", (request_key,))
    return result
