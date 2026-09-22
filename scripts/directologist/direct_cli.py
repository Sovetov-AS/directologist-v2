"""Commands used by Codex; owners approve grants once, not each permitted write."""
from pathlib import Path
from .contracts import ContractError, canonical, digest, read_json
from .storage import Store
from .setup import setup_lock
from . import direct_policy as policy
from .direct_executor import DirectExecutor
from .direct_operations import SERVICE, normalize
from .direct_workflow import build, cycle


def add_parser(commands):
    parser=commands.add_parser('direct',help='Создавать и вести реальную рекламу по DirectGrant v2.')
    parser.add_argument('operation',choices=('grant-check','grant-activate','grant-status','revoke','prepare','apply','status','reconcile','snapshot','performance','queries','build','cycle','guard'))
    parser.add_argument('--date-from')
    parser.add_argument('--date-to')
    parser.add_argument('--goal')
    parser.add_argument('--input',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--confirmation')
    parser.add_argument('--request-id')
    parser.add_argument('--object-id',type=int)
    parser.add_argument('--service',choices=('campaigns','adgroups','ads','keywords'))
    parser.add_argument('--ids',type=int,nargs='+')
    parser.add_argument('--stop',action='store_true')


def execute(context,args):
    def data():
        if not args.input:raise ContractError('Нужен --input с JSON.')
        return read_json(args.input)
    op=args.operation
    if op=='grant-check':
        value=policy.validate(context,data(),active=False)
        return {'sha256':digest(value),'grant':value,'activated':False,'next_step':'Показать владельцу конкретные правила; после согласования grant-activate с этим SHA.'}
    if op in {'grant-activate','grant-status','revoke'}:
        with Store(context,create=True) as store:
            if op=='grant-status':
                g=policy.current(store,active=False)
                return {'grant':g,'sha256':digest(g),'managed_campaign_ids':policy.campaigns(store,g)}
            with setup_lock(context):
                if op=='revoke':return policy.revoke(store)
                return policy.register(store,data(),args.confirmation)
    with DirectExecutor(context) as executor:
        if op=='status':result=executor.status(args.request_id) if args.request_id else executor.history()
        elif op=='reconcile':
            if not args.request_id:raise ContractError('Нужен --request-id.')
            result=executor.reconcile(args.request_id,args.object_id)
        elif op=='prepare':result=executor.prepare(data())
        elif op=='apply':result=executor.apply(data())
        elif op=='build':result=build(executor,data())
        elif op=='cycle':result=cycle(executor)
        elif op=='guard':result=executor.guard(stop=args.stop)
        elif op in {'performance','queries'}:
            if not args.date_from or not args.date_to:raise ContractError('Нужны --date-from и --date-to.')
            g=policy.current(executor.store,active=False)
            api=executor.api(g)
            if api.currency()!=g['currency']:raise ContractError('Валюта отчёта не совпадает с допуском.')
            result=api.performance(policy.campaigns(executor.store,g),args.date_from,args.date_to,queries=op=='queries',goal=args.goal)
            result.update(project_id=context.project_id,context_hash=context.context_hash,grant_hash=digest(g),currency=g['currency'])
        elif op=='snapshot':
            if not args.ids or not args.service:raise ContractError('Нужны --service и --ids.')
            g=policy.current(executor.store,active=False);api=executor.api(g);allowed=policy.campaigns(executor.store,g)
            rows=api.get(args.service,ids=args.ids)
            if any((r['Id'] if args.service=='campaigns' else r.get('CampaignId')) not in allowed for r in rows):
                raise ContractError('Объект находится вне допуска.')
            result=[normalize(r) for r in rows]
        else:raise ContractError('Неизвестная команда.')
    if args.output:
        # New file only: do not overwrite an owner's blueprint or previous plan.
        with args.output.open('x',encoding='utf-8') as f:f.write(canonical(result)+'\n')
    return result
