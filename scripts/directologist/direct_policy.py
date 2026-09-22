"""Owner-approved trusted-local grant for real Direct operations; simulation v1 stays separate."""
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .analytics import instant
from .contracts import ContractError, canonical, digest, identifier
from .planning import integer

OPERATIONS = {'campaign.create', 'campaign.pause', 'campaign.resume', 'campaign.budget', 'campaign.negatives',
              'group.create', 'group.negatives', 'ad.create', 'ad.update', 'ad.moderate', 'ad.pause', 'ad.resume',
              'keyword.create', 'keyword.update', 'keyword.autotarget', 'keyword.bid', 'keyword.pause', 'keyword.resume'}
FIELDS = {'schema_version', 'project_id', 'context_hash', 'version', 'environment', 'client_login', 'currency',
          'starts_at', 'expires_at', 'campaign_ids', 'allow_create', 'allowed_operations', 'allowed_domains',
          'region_ids', 'max_campaigns', 'max_operations_per_day', 'max_daily_budget_micros',
          'max_total_budget_micros', 'max_bid_micros', 'spend_buffer_micros', 'trust_mode',
          'accept_delayed_spend', 'approval_source'}


def validate(context, value, *, active=True):
    if not isinstance(value, dict) or set(value) != FIELDS or type(value.get('schema_version')) is not int or value.get('schema_version') != 2:
        raise ContractError('Неподдерживаемый DirectGrant v2.')
    if value['project_id'] != context.project_id or value['context_hash'] != context.context_hash:
        raise ContractError('Допуск относится к другому проекту/подключению.')
    identifier(value['version'])
    if value['environment'] not in {'sandbox', 'production'} or value['trust_mode'] != 'trusted-local':
        raise ContractError('Нужно явно выбрать environment и trusted-local.')
    resources = context.profile['bindings'].get('direct', {}).get('resources', {})
    if value['client_login'] != resources.get('client_login'):
        raise ContractError('Кабинет не совпадает с подключением проекта.')
    if value['accept_delayed_spend'] is not True:
        raise ContractError('Владелец должен принять задержку отчётов и отсутствие гарантии мгновенного hard cap.')
    if not isinstance(value['currency'], str) or not re.fullmatch('[A-Z]{3}', value['currency']):
        raise ContractError('Не задана валюта кабинета.')
    for field in ('max_daily_budget_micros', 'max_total_budget_micros', 'max_bid_micros', 'spend_buffer_micros'):
        integer(value[field])
        if value[field] <= 0: raise ContractError('Лимиты и резерв должны быть положительными.')
    if value['spend_buffer_micros'] >= value['max_total_budget_micros']:
        raise ContractError('Резерв должен быть меньше общего бюджета.')
    for field, limit in (('max_campaigns', 100), ('max_operations_per_day', 1000)):
        if type(value[field]) is not int or not 1 <= value[field] <= limit: raise ContractError('Неверный лимит объектов/операций.')
    for field in ('campaign_ids', 'allowed_operations', 'allowed_domains', 'region_ids'):
        if not isinstance(value[field], list) or len(value[field]) != len(set(value[field])):
            raise ContractError('Неверная или дублирующаяся область допуска.')
    if any(type(i) is not int or i <= 0 for i in value['campaign_ids'] + value['region_ids']):
        raise ContractError('IDs в допуске должны быть положительными целыми.')
    if not set(map(str, value['campaign_ids'])) <= set(resources.get('campaign_ids', [])):
        raise ContractError('Существующие кампании не выбраны в setup.')
    if type(value['allow_create']) is not bool or (not value['campaign_ids'] and not value['allow_create']):
        raise ContractError('Не выбраны кампании или создание новых.')
    if not value['allowed_operations'] or set(value['allowed_operations']) - OPERATIONS:
        raise ContractError('Неизвестные разрешённые операции.')
    if not value['allowed_domains'] or not value['region_ids']:
        raise ContractError('Нужны разрешённые домены и регионы.')
    for domain in value['allowed_domains']:
        if not isinstance(domain, str) or not re.fullmatch(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?', domain) or '.' not in domain:
            raise ContractError('Домен указывается точно, без схемы, wildcard, пути или порта; IDN через punycode.')
    start, end = instant(value['starts_at']), instant(value['expires_at'])
    if not 0 < (end - start).total_seconds() <= 31 * 86400:
        raise ContractError('Период допуска: от одного момента до 31 суток.')
    if active and not start <= datetime.now(timezone.utc) < end: raise ContractError('Допуск ещё не действует или истёк.')
    if not isinstance(value['approval_source'], str) or not value['approval_source'].strip() or len(value['approval_source']) > 1000:
        raise ContractError('Нужна ссылка/запись явного согласования владельцем.')
    return value


def initialize(store):
    with store.transaction():
        store.connection.execute('CREATE TABLE IF NOT EXISTS direct_grants(version TEXT PRIMARY KEY,data TEXT,hash TEXT,revoked INTEGER DEFAULT 0)')
        store.connection.execute('CREATE TABLE IF NOT EXISTS direct_managed(environment TEXT,campaign_id INTEGER,created INTEGER,PRIMARY KEY(environment,campaign_id))')
        store.connection.execute('CREATE TABLE IF NOT EXISTS direct_steps(request_id TEXT PRIMARY KEY,intent TEXT,plan TEXT,status TEXT,object_id INTEGER,error TEXT,created_at TEXT)')


def register(store, value, confirmation):
    validate(store.context, value)
    if confirmation != digest(value): raise ContractError('Подтверждение не совпадает с SHA-256 конкретной политики.')
    initialize(store)
    with store.transaction():
        row = store.connection.execute('SELECT hash,revoked FROM direct_grants WHERE version=?', (value['version'],)).fetchone()
        if row and (row['hash'] != confirmation or row['revoked']): raise ContractError('Версия уже изменена/отозвана; нужна новая.')
        store.connection.execute('INSERT OR IGNORE INTO direct_grants(version,data,hash) VALUES (?,?,?)', (value['version'], canonical(value), confirmation))
        store.connection.execute("INSERT OR REPLACE INTO metadata VALUES ('active_direct_grant',?)", (value['version'],))
        for campaign in value['campaign_ids']:
            store.connection.execute('INSERT OR IGNORE INTO direct_managed VALUES (?,?,0)', (value['environment'], campaign))
    return {'version': value['version'], 'sha256': confirmation, 'environment': value['environment'], 'registered': True}


def current(store, *, active=True, protective=False):
    initialize(store)
    row = store.connection.execute("SELECT g.* FROM direct_grants g JOIN metadata m ON m.key='active_direct_grant' AND m.value=g.version").fetchone()
    if not row or (row['revoked'] and not protective): raise ContractError('Нет действующего DirectGrant: требуется согласование владельца.')
    return validate(store.context, json.loads(row['data']), active=active)


def historical(store, grant_hash):
    """Read-only reconciliation remains possible after expiry, replacement or revocation."""
    row=store.connection.execute('SELECT data FROM direct_grants WHERE hash=?',(grant_hash,)).fetchone()
    if not row:raise ContractError('Исходный допуск отсутствует в журнале.')
    return validate(store.context,json.loads(row['data']),active=False)


def campaigns(store, grant):
    rows = store.connection.execute('SELECT campaign_id,created FROM direct_managed WHERE environment=?', (grant['environment'],)).fetchall()
    return sorted(set(grant['campaign_ids']) | {r['campaign_id'] for r in rows if r['created'] and grant['allow_create']})


def revoke(store):
    grant = current(store, active=False)
    with store.transaction():store.connection.execute('UPDATE direct_grants SET revoked=1 WHERE version=?', (grant['version'],))
    return {'revoked': grant['version'], 'note': 'Показы в Direct этим не остановлены; сначала direct guard --stop.'}


def href_allowed(href, grant):
    p = urlsplit(href)
    return p.scheme == 'https' and p.hostname in grant['allowed_domains'] and not p.username and not p.password and p.port in {None, 443}
