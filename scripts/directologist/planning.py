"""Exact campaign plans. Provider-specific writes are not enabled by this schema."""
from datetime import datetime, timezone
from .contracts import ContractError, digest, identifier
from .analytics import instant

CAPABILITIES={'campaign.pause','campaign.resume','campaign.budget','campaign.create_off'}

def integer(value):
    if type(value) is not int or not 0<=value<=10**15:
        raise ContractError('Ожидались неотрицательные целые микроденьги в допустимом диапазоне.')
    return value

def snapshot(data):
    if not isinstance(data,dict) or set(data)!={'name','state','daily_budget_micros'}:
        raise ContractError('Неподдерживаемый снимок кампании.')
    if not isinstance(data['name'],str) or not data['name'].strip() or len(data['name'])>200:
        raise ContractError('Некорректное имя кампании.')
    if data['state'] not in {'ON','OFF'}:raise ContractError('Неподдерживаемое состояние.')
    integer(data['daily_budget_micros'])
    return data

def validate(context,plan,*,clock=None):
    fields={'schema_version','project_id','context_hash','policy_version','expires_at','operations','sha256'}
    if not isinstance(plan,dict) or set(plan)!=fields or type(plan['schema_version']) is not int or plan['schema_version']!=1:
        raise ContractError('Неподдерживаемый ChangePlan.')
    if plan['project_id']!=context.project_id or plan['context_hash']!=context.context_hash:
        raise ContractError('План относится к другому проекту или привязкам.')
    if plan['sha256']!=digest({k:v for k,v in plan.items() if k!='sha256'}):raise ContractError('Хеш плана не совпадает.')
    identifier(plan['policy_version'])
    if instant(plan['expires_at']) <= (clock or datetime.now(timezone.utc)):raise ContractError('План просрочен.')
    ops=plan['operations']
    if not isinstance(ops,list) or not 1<=len(ops)<=100:raise ContractError('Пустой или слишком большой план.')
    seen,objects=set(),set()
    for op in ops:
        if not isinstance(op,dict) or set(op)!={'id','capability','object_id','before','after','must_not_change','depends_on'}:
            raise ContractError('Неподдерживаемая операция.')
        identifier(op['id']);identifier(op['object_id'])
        if op['id'] in seen or op['object_id'] in objects:raise ContractError('Объект или шаг повторяется; подготовьте один итоговый снимок.')
        if not isinstance(op['depends_on'],list) or any(v not in seen for v in op['depends_on']):raise ContractError('Неверные зависимости.')
        cap=op['capability']
        if cap not in CAPABILITIES:raise ContractError('Capability не поддерживается.')
        before,after=op['before'],snapshot(op['after'])
        if cap=='campaign.create_off':
            if before is not None or after['state']!='OFF':raise ContractError('Новая кампания допускается только OFF.')
        else:
            snapshot(before)
            changed={k for k in after if before[k]!=after[k]}
            allowed={'daily_budget_micros'} if cap=='campaign.budget' else {'state'}
            if not changed or changed-allowed:raise ContractError('Операция меняет неразрешённые поля.')
            if cap=='campaign.pause' and after['state']!='OFF':raise ContractError('Pause должен выключать показы.')
            if cap=='campaign.resume' and after['state']!='ON':raise ContractError('Resume должен включать показы.')
        keep=op['must_not_change']
        if not isinstance(keep,dict) or any(k not in after or after[k]!=v or before is None or before.get(k)!=v for k,v in keep.items()):
            raise ContractError('Нарушено must_not_change.')
        seen.add(op['id']);objects.add(op['object_id'])
    return plan

def exposure(plan):
    # Conservative reservation, not a guarantee of actual supplier spend.
    return sum(op['after']['daily_budget_micros'] for op in plan['operations'] if op['capability']!='campaign.pause')
