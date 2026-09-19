"""Ограниченные read-only проверки подключений. Ни одного метода рекламной записи."""
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from ..contracts import ContractError, canonical
from ..secrets import Credential


STATES = {
    "UNCONFIGURED": "Не настроено",
    "CHECKED": "Доступ к списку ресурсов проверен",
    "INVALID_CREDENTIAL": "Ключ не принят",
    "FORBIDDEN": "Недостаточно прав или не одобрено API-приложение",
    "QUOTA": "Исчерпана квота; повторить позже вручную",
    "NETWORK": "Сеть или сервис временно недоступны",
    "RESPONSE_ERROR": "Неподдерживаемый или неполный ответ сервиса",
    "NO_RESOURCES": "Доступных ресурсов нет",
    "SKIPPED_OPTIONAL": "Необязательная площадка пропущена",
    "DEFERRED": "Площадка пропущена",
    "AWAITING_COST_APPROVAL": "Ключ сохранён; платная проверка пока запрещена",
    "NEEDS_ENDPOINT": "Сначала задайте разрешённый адрес CRM bridge",
    "CREDENTIAL_MISSING": "Ключ отсутствует в Keychain",
}


class ProbeError(ContractError):
    def __init__(self, state="RESPONSE_ERROR"):
        self.state = state if state in STATES else "RESPONSE_ERROR"
        super().__init__(STATES[self.state])


@dataclass
class Probe:
    state: str
    resources: dict = field(default_factory=dict)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?/?", value)):
        raise ContractError("Адрес bridge должен быть HTTPS origin без пути, credentials и параметров.")
    return value.rstrip("/").lower()


class Transport:
    def __init__(self, bridge_origin=None):
        self.bridge_origin = origin(bridge_origin) if bridge_origin else None
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def request(self, url, credential, *, body=None, client_login=None):
        parts = urllib.parse.urlsplit(url)
        base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        allowed = (
            (base == "https://api.direct.yandex.com" and parts.path in {"/json/v5/clients", "/json/v5/campaigns"}
             and isinstance(body, dict) and body.get("method") == "get")
            or (base == "https://api-metrika.yandex.net" and body is None
                and re.fullmatch(r"/management/v1/(counters|counter/[0-9]+/goals)", parts.path))
            or (self.bridge_origin and base == self.bridge_origin and body is None and parts.path == "/v1/portals")
        )
        if not allowed or parts.username or parts.password or parts.fragment:
            raise ProbeError("FORBIDDEN")
        headers = {"Authorization": "Bearer " + credential.value, "Accept": "application/json",
                   "User-Agent": "directologist/0.1", "Accept-Language": "ru"}
        if base == "https://api-metrika.yandex.net":
            headers["Authorization"] = "OAuth " + credential.value
        if client_login:
            if not re.fullmatch(r"[a-zA-Z0-9@._-]{1,128}", client_login) or credential.value in client_login:
                raise ProbeError()
            headers["Client-Login"] = client_login
        data = canonical(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ProbeError()
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ProbeError() from None
            if not isinstance(result, dict) or credential.value in canonical(result):
                raise ProbeError()
            return result
        except urllib.error.HTTPError as exc:
            # Не читать/логировать body, URL и provider error_message.
            status = exc.code
            exc.close()
            raise ProbeError({401: "INVALID_CREDENTIAL", 403: "FORBIDDEN", 429: "QUOTA"}.get(
                status, "NETWORK" if status >= 500 else "RESPONSE_ERROR")) from None
        except ProbeError:
            raise
        except Exception:
            raise ProbeError("NETWORK") from None


def ids(rows, field, *, numeric=True):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ProbeError()
    result = []
    for row in rows:
        if not isinstance(row, dict) or field not in row or isinstance(row[field], bool):
            raise ProbeError()
        value = str(row[field])
        if not re.fullmatch(r"[0-9]{1,20}" if numeric else r"[a-zA-Z0-9@._-]{1,128}", value):
            raise ProbeError()
        if value in result:
            raise ProbeError()
        result.append(value)
    return result


def direct_result(data):
    if "error" in data:
        error = data["error"]
        code = error.get("error_code") if isinstance(error, dict) else None
        states = {53: "INVALID_CREDENTIAL", 54: "FORBIDDEN", 58: "FORBIDDEN", 513: "FORBIDDEN",
                  3000: "FORBIDDEN", 3001: "FORBIDDEN", 152: "QUOTA", 506: "QUOTA",
                  52: "NETWORK", 1000: "NETWORK", 1001: "NETWORK", 1002: "NETWORK", 1020: "NETWORK"}
        raise ProbeError(states.get(code, "RESPONSE_ERROR"))
    result = data.get("result")
    if not isinstance(result, dict):
        raise ProbeError()
    return result


class Adapter:
    """Explicit selection callbacks receive validated IDs only, never provider names/payloads."""
    def __init__(self, transport=None):
        self.transport = transport or Transport()

    def probe(self, provider, credential, config, choose):
        try:
            if provider == "wordstat":
                # Ни дерева регионов, ни query: возможность тарификации требует отдельного допуска.
                return Probe("AWAITING_COST_APPROVAL", {"folder_id": config["folder_id"]})
            if provider == "direct":
                login = config.get("client_login")
                data = direct_result(self.transport.request("https://api.direct.yandex.com/json/v5/clients",
                    credential, body={"method": "get", "params": {"FieldNames": ["Login"]}}, client_login=login))
                logins = ids(data.get("Clients"), "Login", numeric=False)
                selected = choose("Кабинет Директа", logins, False)
                if not selected:
                    return Probe("NO_RESOURCES")
                campaigns = []
                offset = 0
                for _ in range(20):
                    page = direct_result(self.transport.request("https://api.direct.yandex.com/json/v5/campaigns",
                        credential, client_login=selected[0], body={"method": "get", "params": {
                            "SelectionCriteria": {}, "FieldNames": ["Id"], "Page": {"Limit": 1000, "Offset": offset}}}))
                    campaigns.extend(ids(page.get("Campaigns"), "Id"))
                    if "LimitedBy" not in page:
                        break
                    following = page["LimitedBy"]
                    if type(following) is not int or following <= offset:
                        raise ProbeError()
                    offset = following
                else:
                    raise ProbeError()
                if len(set(campaigns)) != len(campaigns):
                    raise ProbeError()
                chosen = choose("Кампании", campaigns, True)
                return Probe("CHECKED", {"client_login": selected[0], "campaign_ids": chosen}) if chosen else Probe("NO_RESOURCES")
            if provider == "metrika":
                counters = []
                for page in range(20):
                    result = self.transport.request(
                        f"https://api-metrika.yandex.net/management/v1/counters?per_page=1000&offset={page * 1000 + 1}", credential)
                    current = ids(result.get("counters"), "id")
                    counters.extend(current)
                    total = result.get("rows")
                    if type(total) is not int or total < len(counters):
                        raise ProbeError()
                    if len(counters) == total:
                        break
                    if not current:
                        raise ProbeError()
                else:
                    raise ProbeError()
                if len(set(counters)) != len(counters):
                    raise ProbeError()
                counter = choose("Счётчик Метрики", counters, False)
                if not counter:
                    return Probe("NO_RESOURCES")
                result = self.transport.request(f"https://api-metrika.yandex.net/management/v1/counter/{counter[0]}/goals", credential)
                goals = choose("Цели Метрики", ids(result.get("goals"), "id"), True)
                return Probe("CHECKED", {"counter_id": counter[0], "goal_ids": goals}) if goals else Probe("NO_RESOURCES")
            if provider == "crm":
                result = self.transport.request(config["origin"] + "/v1/portals", credential)
                portals = choose("Портал через CRM bridge", ids(result.get("portals"), "member_id", numeric=False), False)
                return Probe("CHECKED", {"bridge_id": portals[0]}) if portals else Probe("NO_RESOURCES")
            raise ProbeError()
        except ProbeError as error:
            return Probe(error.state)
        except ContractError:
            raise
        except (KeyError, TypeError, ValueError):
            return Probe("RESPONSE_ERROR")
