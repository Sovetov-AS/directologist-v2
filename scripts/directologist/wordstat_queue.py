"""Адаптация идей v1: durable dedup/cache/quotas; неизвестный платный исход без автоповтора."""
import json
import os
import re
import time
from contextlib import contextmanager

from .analytics import decimal_text
from .contracts import ContractError, canonical, confined, digest, identifier
from .storage import Store
from .locks import file_lock
from .adapters.reports import ReadPending


def normalize_request(data):
    if not isinstance(data, dict) or set(data) != {"phrase", "regions", "devices", "num_phrases"}:
        raise ContractError("Неподдерживаемая задача Wordstat.")
    phrase = data["phrase"]
    if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 400 or any(ord(c) < 32 for c in phrase):
        raise ContractError("Некорректная фраза Wordstat.")
    regions, devices, size = data["regions"], data["devices"], data["num_phrases"]
    if (not isinstance(regions, list) or not regions or len(regions) > 100 or any(type(v) is not int or v <= 0 for v in regions)
            or not isinstance(devices, list) or not devices or any(v not in {"all", "desktop", "phone", "tablet"} for v in devices)
            or ("all" in devices and len(set(devices)) != 1) or type(size) is not int or not 1 <= size <= 2000):
        raise ContractError("Некорректные регионы, устройства или размер Wordstat.")
    return {"phrase": re.sub(r"\s+", " ", phrase.casefold().replace("ё", "е")).strip(),
            "regions": sorted(set(regions)), "devices": sorted(set(devices)), "num_phrases": size}


def clean_response(raw):
    if not isinstance(raw, dict) or not all(k in raw for k in ("totalCount", "results", "associations")):
        raise ContractError("Неподдерживаемый ответ Wordstat.")
    result = {"totalCount": decimal_text(raw["totalCount"])}
    for key in ("results", "associations"):
        if not isinstance(raw[key], list) or len(raw[key]) > 2000:
            raise ContractError("Некорректный список Wordstat.")
        result[key] = []
        for item in raw[key]:
            if not isinstance(item, dict) or not isinstance(item.get("phrase"), str) or len(item["phrase"]) > 400 or any(ord(c) < 32 for c in item["phrase"]):
                raise ContractError("Некорректная фраза в ответе Wordstat.")
            result[key].append({"phrase": item["phrase"], "count": decimal_text(item.get("count"))})
    if len(canonical(result).encode()) > 1024 * 1024:
        raise ContractError("Ответ Wordstat превышает размер кеша.")
    return result


class WordstatReader:
    """Технический callback для будущего допущенного runner; сам не выдаёт допуск."""
    def __init__(self, credential, config, http):
        self.credential, self.config, self.http = credential, config, http

    def __call__(self, request):
        spec = normalize_request(request)
        body = {"phrase": spec["phrase"], "numPhrases": spec["num_phrases"],
                "regions": [str(v) for v in spec["regions"]], "devices": ["DEVICE_" + v.upper() for v in spec["devices"]],
                "folderId": self.config["folder_id"]}
        return clean_response(self.http.request("wordstat", self.credential, body=body, auth_scheme=self.config["auth_scheme"]))


class WordstatQueue:
    def __init__(self, context, clock=time.time):
        self.context, self.clock = context, clock
        self.store = Store(context, create=True)
        try:
            with self.store.transaction():
                for sql in (
                    "CREATE TABLE IF NOT EXISTS ws_jobs(job_id TEXT PRIMARY KEY, context_hash TEXT, spec TEXT, request_limit INTEGER, attempts INTEGER NOT NULL DEFAULT 0)",
                    "CREATE TABLE IF NOT EXISTS ws_items(job_id TEXT, key TEXT, request TEXT, state TEXT, retry_at REAL, PRIMARY KEY(job_id,key))",
                    "CREATE TABLE IF NOT EXISTS ws_cache(key TEXT PRIMARY KEY, data TEXT, hash TEXT, fetched REAL, expires REAL)",
                    "CREATE TABLE IF NOT EXISTS ws_requests(at REAL)",
                ):
                    self.store.connection.execute(sql)
        except BaseException:
            self.store.connection.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.store.connection.close()

    def enqueue(self, job_id, requests, *, request_limit, ttl_seconds):
        identifier(job_id)
        if type(request_limit) is not int or not 1 <= request_limit <= 10000 or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ContractError("Нужны явные лимит запросов и срок кеша.")
        if not isinstance(requests, list) or not requests or len(requests) > 2000:
            raise ContractError("Некорректный пакет Wordstat.")
        normalized = {digest(normalize_request(item)): normalize_request(item) for item in requests}
        spec = canonical({"requests": [normalized[k] for k in sorted(normalized)], "ttl_seconds": ttl_seconds})
        with self.store.transaction():
            old = self.store.connection.execute("SELECT * FROM ws_jobs WHERE job_id=?", (job_id,)).fetchone()
            if old:
                if old["spec"] != spec or old["request_limit"] != request_limit or old["context_hash"] != self.context.context_hash:
                    raise ContractError("job_id уже связан с другим пакетом/контекстом.")
            else:
                self.store.connection.execute("INSERT INTO ws_jobs(job_id,context_hash,spec,request_limit) VALUES (?,?,?,?)",
                    (job_id, self.context.context_hash, spec, request_limit))
                for item in normalized.values():
                    key = digest({"context_hash": self.context.context_hash, "request": item})
                    self.store.connection.execute("INSERT INTO ws_items VALUES (?,?,?,?,?)", (job_id, key, canonical(item), "PENDING", 0))
        return self.status(job_id)

    def _job(self, job_id):
        identifier(job_id)
        job = self.store.connection.execute("SELECT * FROM ws_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job or job["context_hash"] != self.context.context_hash:
            raise ContractError("Пакет Wordstat отсутствует или относится к прежним привязкам.")
        return job

    def _cached(self, key, ttl):
        row = self.store.connection.execute("SELECT * FROM ws_cache WHERE key=?", (key,)).fetchone()
        now = self.clock()
        if not row or row["fetched"] > now or now >= min(row["expires"], row["fetched"] + ttl):
            return None
        data = json.loads(row["data"])
        if digest(data) != row["hash"] or clean_response(data) != data:
            raise ContractError("Кеш Wordstat повреждён.")
        return data

    def status(self, job_id):
        job = self._job(job_id)
        ttl = json.loads(job["spec"])["ttl_seconds"]
        items = []
        for row in self.store.connection.execute("SELECT key,state,retry_at FROM ws_items WHERE job_id=? ORDER BY key", (job_id,)):
            items.append({"key": row["key"], "state": row["state"], "retry_at": row["retry_at"],
                          "cache_fresh": self._cached(row["key"], ttl) is not None})
        return {"schema_version": 1, "project_id": self.context.project_id, "job_id": job_id,
                "attempts": job["attempts"], "request_limit": job["request_limit"], "items": items,
                "blocked_reason": "UNKNOWN" if any(i["state"] == "UNKNOWN" for i in items) else
                    ("REQUEST_LIMIT" if job["attempts"] >= job["request_limit"] and any(i["state"] != "DONE" for i in items) else None),
                "live_runner_enabled": False}

    @contextmanager
    def _lock(self):
        with file_lock(confined(self.context.directory, ".wordstat.lock")):
            yield

    def step(self, job_id, fetch, *, requests_per_second, requests_per_hour):
        """One callback attempt. Production caller must first obtain a paid-read grant."""
        if type(requests_per_second) is not int or not 1 <= requests_per_second <= 10 or type(requests_per_hour) is not int or requests_per_hour < 1:
            raise ContractError("Некорректный эксплуатационный лимит Wordstat.")
        with self._lock():
            with self.store.transaction():
                job = self._job(job_id)
                ttl = json.loads(job["spec"])["ttl_seconds"]
                # Worker lock is held: any RUNNING item belongs to an interrupted predecessor.
                self.store.connection.execute("UPDATE ws_items SET state='UNKNOWN' WHERE job_id=? AND state='RUNNING'", (job_id,))
                if self.store.connection.execute("SELECT 1 FROM ws_items WHERE job_id=? AND state='UNKNOWN'", (job_id,)).fetchone():
                    return self.status(job_id)
                items = self.store.connection.execute("SELECT * FROM ws_items WHERE job_id=? AND state IN ('PENDING','PAUSED_QUOTA') ORDER BY key", (job_id,)).fetchall()
                item = None
                for candidate in items:
                    if self._cached(candidate["key"], ttl) is not None:
                        self.store.connection.execute("UPDATE ws_items SET state='DONE' WHERE job_id=? AND key=?", (job_id, candidate["key"]))
                    elif item is None and candidate["retry_at"] <= self.clock():
                        item = candidate
                if item is None or job["attempts"] >= job["request_limit"]:
                    return self.status(job_id)
                now = self.clock()
                for seconds, limit in ((1, requests_per_second), (3600, requests_per_hour)):
                    times = [r[0] for r in self.store.connection.execute("SELECT at FROM ws_requests WHERE at>? ORDER BY at", (now - seconds,))]
                    if len(times) >= limit:
                        self.store.connection.execute("UPDATE ws_items SET state='PAUSED_QUOTA',retry_at=? WHERE job_id=? AND key=?",
                            (times[-limit] + seconds, job_id, item["key"]))
                        return self.status(job_id)
                self.store.connection.execute("INSERT INTO ws_requests VALUES (?)", (now,))
                self.store.connection.execute("UPDATE ws_jobs SET attempts=attempts+1 WHERE job_id=?", (job_id,))
                self.store.connection.execute("UPDATE ws_items SET state='RUNNING' WHERE job_id=? AND key=?", (job_id, item["key"]))
            try:
                data = clean_response(fetch(json.loads(item["request"])))
            except ReadPending as error:
                with self.store.transaction():
                    self.store.connection.execute("UPDATE ws_items SET state='PAUSED_QUOTA',retry_at=? WHERE job_id=? AND key=?",
                        (self.clock() + error.retry_after, job_id, item["key"]))
                return self.status(job_id)
            except Exception:
                with self.store.transaction():
                    self.store.connection.execute("UPDATE ws_items SET state='UNKNOWN' WHERE job_id=? AND key=?", (job_id, item["key"]))
                return self.status(job_id)
            with self.store.transaction():
                fetched = self.clock()
                self.store.connection.execute("INSERT OR REPLACE INTO ws_cache VALUES (?,?,?,?,?)",
                    (item["key"], canonical(data), digest(data), fetched, fetched + ttl))
                self.store.connection.execute("UPDATE ws_items SET state='DONE',retry_at=0 WHERE job_id=? AND key=?", (job_id, item["key"]))
            return self.status(job_id)

    def retry_unknown(self, job_id, key):
        """Explicit reconciliation decision; never invoked by step or CLI automatically."""
        with self._lock(), self.store.transaction():
            self._job(job_id)
            self.store.connection.execute("UPDATE ws_items SET state='PENDING',retry_at=0 WHERE job_id=? AND key=? AND state='UNKNOWN'", (job_id, key))
