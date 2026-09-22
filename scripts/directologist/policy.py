"""Simulation grants and fail-closed live boundary. No user budget is activated."""
import json
from datetime import datetime,timezone
from .contracts import ContractError,canonical,digest,identifier
from .analytics import instant
from .planning import CAPABILITIES,integer,exposure

FIELDS={'schema_version','mode','project_id','context_hash','version','resources','capabilities','max_reserved_micros','max_operations','valid_from','expires_at','approval_source'}

def validate(context,grant,clock=None):
    if not isinstance(grant,dict) or set(grant)!=FIELDS or type(grant['schema_version']) is not int or grant['schema_version']!=1:
        raise ContractError('Неподдерживаемая политика.')
    if grant['mode']!='SIMULATION':raise ContractError('Контракт v1 предназначен для симуляции; реальный допуск — DirectGrant v2.')
    if grant['project_id']!=context.project_id or grant['context_hash']!=context.context_hash:raise ContractError('Чужая политика.')
    identifier(grant['version'])
    for key in ('resources','capabilities'):
        if not isinstance(grant[key],list) or not grant[key] or len(grant[key])!=len(set(grant[key])):raise ContractError('Неполная область допуска.')
    for r in grant['resources']:identifier(r)
    if set(grant['capabilities'])-CAPABILITIES:raise ContractError('Неизвестная capability.')
    integer(grant['max_reserved_micros'])
    if type(grant['max_operations']) is not int or not 1<=grant['max_operations']<=1000:raise ContractError('Неверный лимит операций.')
    start,end=instant(grant['valid_from']),instant(grant['expires_at'])
    if not start <= (clock or datetime.now(timezone.utc)) < end or (end-start).total_seconds()>86400:
        raise ContractError('Политика не действует; срок ограничен сутками.')
    if not isinstance(grant['approval_source'],str) or not grant['approval_source'].strip() or len(grant['approval_source'])>500:
        raise ContractError('Нет основания допуска.')
    return grant

def initialize(store):
    store.connection.execute('CREATE TABLE IF NOT EXISTS policy_grants(version TEXT PRIMARY KEY,data TEXT,revoked INTEGER NOT NULL DEFAULT 0)')

def register(store,grant):
    validate(store.context,grant)
    with store.transaction():
        initialize(store)
        old=store.connection.execute('SELECT data FROM policy_grants WHERE version=?',(grant['version'],)).fetchone()
        if old and old[0]!=canonical(grant):raise ContractError('Версия политики неизменяема.')
        store.connection.execute('INSERT OR IGNORE INTO policy_grants(version,data) VALUES (?,?)',(grant['version'],canonical(grant)))
        store.connection.execute("INSERT OR REPLACE INTO metadata VALUES ('active_policy',?)",(grant['version'],))

def revoke(store,version):
    with store.transaction():
        store.connection.execute('UPDATE policy_grants SET revoked=1 WHERE version=?',(version,))

def check(store,plan):
    metadata=dict(store.connection.execute('SELECT key,value FROM metadata'))
    if metadata.get('recovery_required')!='0':raise ContractError('После backup требуется сверка; исполнение запрещено.')
    if metadata.get('active_policy')!=plan['policy_version']:raise ContractError('Версия политики изменилась.')
    row=store.connection.execute('SELECT * FROM policy_grants WHERE version=?',(plan['policy_version'],)).fetchone()
    if not row or row['revoked']:raise ContractError('Допуск отсутствует или отозван.')
    grant=validate(store.context,json.loads(row['data']))
    if instant(plan['expires_at'])>instant(grant['expires_at']):raise ContractError('План переживает срок допуска.')
    if any(op['object_id'] not in grant['resources'] or op['capability'] not in grant['capabilities'] for op in plan['operations']):
        raise ContractError('Ресурс или операция вне допуска.')
    rows=store.connection.execute('SELECT reserved_micros,operation_count FROM executions WHERE policy_version=? AND plan_hash!=?',(grant['version'],plan['sha256'])).fetchall()
    if sum(r[0] for r in rows)+exposure(plan)>grant['max_reserved_micros'] or sum(r[1] for r in rows)+len(plan['operations'])>grant['max_operations']:
        raise ContractError('Совокупный лимит допуска превышен.')
    return grant

def capabilities():
    from .direct_policy import OPERATIONS
    return {'live_write_enabled':False,'reason':'PROJECT_GRANT_REQUIRED_CHECK_CONTEXT',
            'operations':{k:{'simulation':'LOCAL_TESTED','live':'LEGACY_SIMULATION_ONLY'} for k in sorted(CAPABILITIES)},
            'direct_v2':{'entrypoint':'direct','mode':'TRUSTED_LOCAL','status':'IMPLEMENTED_REQUIRES_PROJECT_GRANT',
                         'operations':sorted(OPERATIONS),'api_acceptance':'NOT_VERIFIED_ON_LIVE_ACCOUNT'}}
