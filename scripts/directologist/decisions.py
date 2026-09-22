"""Persist reviewed proposals; never executes model recommendations."""
import hashlib
import json
from .contracts import ContractError, canonical, confined, digest, identifier, read_json
from .analytics import load_bundle
from .storage import Store, now


def text(value, limit=4000):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or any(ord(c)<32 and c not in '\n\t' for c in value):
        raise ContractError('Нужен непустой ограниченный текст.')
    return value


def knowledge(context):
    root=confined(context.workspace,'knowledge','methods')
    files={p.stem: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.glob('*.md')) if not p.is_symlink()}
    if set(files)!={'diagnosis','demand','ads'}:
        raise ContractError('Базовый корпус методик неполон.')
    head=None; cards=[]
    if context.database.exists():
        with Store(context,read_only=True) as store:
            meta=dict(store.connection.execute('SELECT key,value FROM metadata'))
            head=meta.get('knowledge_head')
            if head:
                row=store.connection.execute('SELECT data FROM knowledge_versions WHERE id=?',(head,)).fetchone()
                if not row:raise ContractError('Активная версия знаний повреждена.')
                cards=json.loads(row[0])
    return {'version':digest({'methods':files,'project_lessons':head}),'methods':files,'lessons_version':head,'active_lessons':cards}


def validate(context, data):
    fields={'schema_version','project_id','context_hash','knowledge_version','methods','status','evidence_ids',
            'facts','hypotheses','alternatives','uncertainty','expected_outcome','evaluation_window','next_step'}
    if not isinstance(data,dict) or set(data)!=fields or type(data['schema_version']) is not int or data['schema_version']!=1:
        raise ContractError('Неподдерживаемый контракт решения.')
    if data['project_id']!=context.project_id or data['context_hash']!=context.context_hash:
        raise ContractError('Решение относится к другому контексту.')
    corpus=knowledge(context)
    if data['knowledge_version']!=corpus['version']:
        raise ContractError('Методики изменились; нужна повторная проверка решения.')
    if not isinstance(data['methods'],list) or not data['methods'] or any(v not in corpus['methods'] for v in data['methods']):
        raise ContractError('Методика не найдена.')
    if data['status'] not in {'PROPOSED','INSUFFICIENT_DATA','REVIEW_REQUIRED','OWNER_REQUIRED','OBSERVE'}:
        raise ContractError('Статус решения не является допуском.')
    ids=data['evidence_ids']
    if not isinstance(ids,list) or len(ids)>20 or len(ids)!=len(set(ids)):
        raise ContractError('Некорректные ссылки evidence.')
    for eid in ids: load_bundle(context,eid)
    if not ids and data['status'] not in {'INSUFFICIENT_DATA','OWNER_REQUIRED','REVIEW_REQUIRED'}:
        raise ContractError('Без evidence нельзя делать подтверждённое предложение.')
    if not isinstance(data['facts'],list) or len(data['facts'])>50:
        raise ContractError('Некорректные факты.')
    for fact in data['facts']:
        if not isinstance(fact,dict) or set(fact)!={'text','evidence_id'} or fact['evidence_id'] not in ids:
            raise ContractError('Факту нужна проверенная ссылка evidence.')
        text(fact['text'])
    for key in ('hypotheses','alternatives','uncertainty'):
        if not isinstance(data[key],list) or not data[key] or len(data[key])>20:
            raise ContractError('Нужны гипотезы, альтернативы и неопределённость.')
        for item in data[key]: text(item)
    for key in ('expected_outcome','evaluation_window','next_step'): text(data[key])
    return data


def save(context,data):
    validate(context,data)
    with Store(context,create=True) as store,store.transaction():
        store.connection.execute('CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, data TEXT, created_at TEXT)')
        key=digest(data)
        store.connection.execute('INSERT OR IGNORE INTO decisions VALUES (?,?,?)',(key,canonical(data),now()))
    return {'decision_id':key,'status':data['status'],'autonomous_writes':False}


def recover(context):
    result={'project_id':context.project_id,'context_hash':context.context_hash,'binding_version':context.profile['binding_version'],
            'providers':sorted(context.profile['bindings']), 'knowledge':knowledge(context), 'decisions':[], 'unresolved_operations':[],
            'autonomous_writes':False,'live_write_mode':'OWNER_GRANT_REQUIRED'}
    if context.database.exists():
        with Store(context,read_only=True) as store:
            tables={r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result['recovery_required']=dict(store.connection.execute('SELECT key,value FROM metadata')).get('recovery_required')!='0'
            if 'decisions' in tables:
                result['decisions']=[{'id':r['id'],'created_at':r['created_at'],'proposal':json.loads(r['data'])} for r in store.connection.execute('SELECT * FROM decisions ORDER BY created_at DESC LIMIT 5')]
            if 'executions' in tables:
                result['unresolved_operations']=[dict(r) for r in store.connection.execute("SELECT plan_hash,status FROM executions WHERE status!='CONFIRMED'")]
            if 'direct_steps' in tables:
                result['unresolved_direct_operations']=[dict(r) for r in store.connection.execute("SELECT request_id,status,object_id,error FROM direct_steps WHERE status IN ('STARTED','ACKNOWLEDGED','UNKNOWN','PARTIAL')")]
            if 'direct_grants' in tables:
                from . import direct_policy
                row=store.connection.execute("SELECT g.* FROM direct_grants g JOIN metadata m ON m.key='active_direct_grant' AND m.value=g.version").fetchone()
                if row:
                    result['direct_grant']={'version':row['version'],'sha256':row['hash'],'revoked':bool(row['revoked'])}
                    try:
                        g=direct_policy.validate(context,json.loads(row['data']))
                        enabled=not row['revoked'] and not result['recovery_required'] and not result.get('unresolved_direct_operations')
                        result.update(autonomous_writes=bool(enabled),live_write_mode='TRUSTED_LOCAL' if enabled else 'BLOCKED',environment=g['environment'])
                    except ContractError:result['live_write_mode']='INVALID_OR_EXPIRED_GRANT'
    return result
