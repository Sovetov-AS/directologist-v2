"""Journaled real Direct operations under an explicit trusted-local project grant."""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import direct_policy as policy
from .adapters.direct_api import DirectAPI, DirectFailure, positive_id
from .analytics import instant
from .contracts import ContractError, canonical, digest
from .direct_operations import SERVICE, compile_request, normalize, subset, check_bid, instant_date
from .secrets import SecretStore
from .setup import setup_lock
from .storage import Store, now


def configured_api(context, grant, store):
    binding=context.profile['bindings'].get('direct')
    exists=store.connection.execute("SELECT name FROM sqlite_master WHERE name='connections'").fetchone()
    row=store.connection.execute("SELECT record FROM connections WHERE provider='direct'").fetchone() if exists else None
    record=json.loads(row[0]) if row else {}
    if not binding or record.get('state')!='CHECKED' or record.get('connection_id')!=binding['connection_id'] or record.get('resources')!=binding['resources']:
        raise ContractError('Нет актуального проверенного подключения Direct; выполните setup.')
    if record.get('config',{}).get('environment','production')!=grant['environment']:
        raise ContractError('Environment подключения и политики различаются; выполните setup в нужной среде.')
    credential=SecretStore(context.project_id).get('direct',binding['connection_id'])
    if credential is None:raise ContractError('Ключ Direct отсутствует в хранилище ОС.')
    return DirectAPI(credential,grant['client_login'],grant['environment'])


class DirectExecutor:
    def __init__(self,context,api=None):
        self.context=context;self.store=Store(context,create=True);policy.initialize(self.store)
        self._api=api
    def __enter__(self):return self
    def __exit__(self,*args):self.store.__exit__()
    def api(self,grant):
        api=self._api or configured_api(self.context,grant,self.store)
        if api.environment!=grant['environment'] or api.client_login!=grant['client_login']:
            raise ContractError('API-клиент не соответствует допуску.')
        return api
    def status(self,request_id):
        row=self.store.connection.execute('SELECT request_id,status,object_id,error FROM direct_steps WHERE request_id=?',(request_id,)).fetchone()
        return dict(row) if row else None
    def history(self):
        return [dict(r) for r in self.store.connection.execute('SELECT request_id,status,object_id,error FROM direct_steps ORDER BY rowid DESC LIMIT 50')]
    def _set(self,key,status,object_id=None,error=None):
        with self.store.transaction():
            self.store.connection.execute('UPDATE direct_steps SET status=?,object_id=COALESCE(?,object_id),error=? WHERE request_id=?',(status,object_id,error,key))
    def _replayed(self,request):
        row=self.store.connection.execute('SELECT intent FROM direct_steps WHERE request_id=?',(request['request_id'],)).fetchone()
        if row:
            if row['intent']!=digest(request):raise ContractError('request_id уже принадлежит другому действию.')
            return self.status(request['request_id'])
    def _check(self,grant,request,api,plan):
        pause=request['action']=='campaign.pause'
        if not pause:
            meta=dict(self.store.connection.execute('SELECT key,value FROM metadata'))
            if meta.get('recovery_required')!='0':raise ContractError('После восстановления backup нужна сверка; записи заблокированы.')
            if self.store.connection.execute("SELECT 1 FROM direct_steps WHERE status IN ('STARTED','ACKNOWLEDGED','UNKNOWN','PARTIAL')").fetchone():
                raise ContractError('Сначала сверить незавершённое действие; повтор автоматически запрещён.')
            day=datetime.now(timezone.utc).date().isoformat()
            count=self.store.connection.execute('SELECT count(*) FROM direct_steps WHERE created_at>=?',(day,)).fetchone()[0]
            if count>=grant['max_operations_per_day']:raise ContractError('OWNER_REQUIRED: суточный лимит операций.')
            if api.currency()!=grant['currency']:raise ContractError('Валюта Direct не совпадает с допуском.')
            ids=policy.campaigns(self.store,grant)
            if len(ids)+(request['action']=='campaign.create')>grant['max_campaigns']:
                raise ContractError('OWNER_REQUIRED: лимит кампаний.')
            spent=api.spend(ids,instant_date(grant['starts_at']).isoformat(),datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat())
            if spent+grant['spend_buffer_micros']>=grant['max_total_budget_micros']:
                raise ContractError('BUDGET_STOP: выполнить direct guard; лимит с резервом исчерпан.')
            if ids or request['action']=='campaign.create':
                amounts=[];allocation=0
                today=datetime.now(ZoneInfo("Europe/Moscow")).date()
                for cid in ids:
                    campaign=normalize(api.one('campaigns',cid))
                    amount=campaign.get('DailyBudget',{}).get('Amount')
                    if type(amount) is not int or amount<=0:raise ContractError('Нужна проверенная ручная стратегия и дневной бюджет каждой кампании.')
                    if request['action']=='campaign.budget' and cid==request['object_id']:amount=request['params']['daily_budget_micros']
                    from datetime import date
                    end=campaign.get("EndDate")
                    if not end:raise ContractError("Не задана дата остановки кампании.")
                    allocation+=amount*max(7,(date.fromisoformat(end)-today).days)
                    amounts.append(amount)
                if request['action']=='campaign.create':
                    from datetime import date
                    amount=request['params']['daily_budget_micros'];amounts.append(amount)
                    allocation+=amount*max(7,(date.fromisoformat(request['params']['end_date'])-today).days)
                if allocation+spent+grant['spend_buffer_micros']>grant['max_total_budget_micros']:
                    raise ContractError('OWNER_REQUIRED: резерв бюджетов на оставшийся период превышает общий предел.')
                if sum(amounts)>grant['max_daily_budget_micros']:raise ContractError('OWNER_REQUIRED: суммарные дневные бюджеты превышены.')
            cid=plan['campaign_id']
            if request['action']=='campaign.resume' or (cid and api.one('campaigns',cid).get('State')=='ON'):
                self.audit_launch(api,cid,grant)
    def prepare(self,request):
        pause=request.get('action')=='campaign.pause'
        grant=policy.current(self.store,active=not pause,protective=pause);api=self.api(grant)
        plan=compile_request(self.context,grant,request,api,policy.campaigns(self.store,grant))
        self._check(grant,request,api,plan)
        return plan
    def apply(self,plan):
        with setup_lock(self.context):
            if not isinstance(plan,dict) or plan.get('sha256')!=digest({k:v for k,v in plan.items() if k!='sha256'}):
                raise ContractError('Хеш плана изменён.')
            request=plan['request'];existing=self._replayed(request)
            if existing:return existing
            fresh=self.prepare(request)
            if fresh!=plan:raise ContractError('STALE_PLAN: состояние или политика изменились; нужен новый план.')
            pause=request['action']=='campaign.pause'
            grant=policy.current(self.store,active=not pause,protective=pause);api=self.api(grant)
            # Last policy read immediately before the durable intent.
            if digest(grant)!=plan['grant_hash']:raise ContractError('Версия допуска изменена.')
            with self.store.transaction():
                self.store.connection.execute('INSERT INTO direct_steps VALUES (?,?,?,?,?,?,?)',
                    (request['request_id'],digest(request),canonical(plan),'STARTED',None,None,now()))
            key=request['request_id']
            try:
                oid=api.mutate(plan['service'],plan['method'],plan['params'])
                expected_id=request['object_id']
                if expected_id is not None and oid!=expected_id and request['action']!='keyword.update':
                    self._set(key,'UNKNOWN',error='RETURNED_ID_MISMATCH');return self.status(key)
                self._set(key,'ACKNOWLEDGED',oid)
                kind=request['action'].split('.')[0]
                after=normalize(api.one(SERVICE[kind],oid))
                if not self._matches(plan,after):
                    self._set(key,'PARTIAL',error='POSTCONDITION_MISMATCH');return self.status(key)
                with self.store.transaction():
                    if request['action']=='campaign.create':
                        self.store.connection.execute('INSERT OR IGNORE INTO direct_managed VALUES (?,?,1)',(grant['environment'],oid))
                    self.store.connection.execute("UPDATE direct_steps SET status='VERIFIED' WHERE request_id=?",(key,))
                return self.status(key)
            except DirectFailure as exc:
                code=':'+str(exc.code) if exc.code is not None else ''
                self._set(key,'REJECTED' if exc.outcome=='REJECTED' else 'UNKNOWN',error=exc.outcome+code)
                return self.status(key)
            except Exception:
                self._set(key,'UNKNOWN',error='UNEXPECTED_FAILURE');return self.status(key)
    def perform(self,request):
        existing=self._replayed(request)
        if existing:return existing
        return self.apply(self.prepare(request))
    def _matches(self,plan,after):
        if plan['request']['action']=='ad.moderate':return after.get('Status') in plan['expected']['Status']
        if plan['request']['action']=='ad.resume':return after.get('State') in {'ON','OFF'} and after.get('Status')=='ACCEPTED'
        return subset(plan['expected'],after)
    def reconcile(self,request_id,object_id=None):
        with setup_lock(self.context):
            row=self.store.connection.execute('SELECT * FROM direct_steps WHERE request_id=?',(request_id,)).fetchone()
            if not row:raise ContractError('Неизвестный request_id.')
            if row['status'] in {'VERIFIED','REJECTED'}:return self.status(request_id)
            plan=json.loads(row['plan']);grant=policy.historical(self.store,plan['grant_hash'])
            oid=row['object_id'] or object_id
            if oid is None:return self.status(request_id)
            positive_id(oid)
            after=normalize(self.api(grant).one(SERVICE[plan['request']['action'].split('.')[0]],oid))
            if not self._matches(plan,after):return self.status(request_id)
            with self.store.transaction():
                if plan['request']['action']=='campaign.create':
                    self.store.connection.execute('INSERT OR IGNORE INTO direct_managed VALUES (?,?,1)',(grant['environment'],oid))
                self.store.connection.execute("UPDATE direct_steps SET status='VERIFIED',object_id=?,error=NULL WHERE request_id=?",(oid,request_id))
            return self.status(request_id)
    def audit_launch(self,api,campaign_id,grant):
        campaign=normalize(api.one('campaigns',campaign_id))
        unified=campaign.get('UnifiedCampaign',{})
        strategy=unified.get('BiddingStrategy',{})
        if strategy.get('Search',{}).get('BiddingStrategyType')!='HIGHEST_POSITION' or strategy.get('Network',{}).get('BiddingStrategyType')!='SERVING_OFF':
            raise ContractError('Для этой версии допускается ручной поиск; другие стратегии требуют нового адаптера.')
        placements={'SearchResults':'YES','ProductGallery':'NO','DynamicPlaces':'NO','Maps':'NO','SearchOrganizationList':'NO'}
        if not subset(placements,strategy.get('Search',{}).get('PlacementTypes')) or unified.get('Settings',{}).get('ENABLE_AREA_OF_INTEREST_TARGETING')!='NO':
            raise ContractError('Не подтверждены площадки поиска и отключение расширенного геотаргетинга.')
        end=campaign.get('EndDate')
        if not end or end>instant_date(grant['expires_at']).isoformat() or end<=datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat():
            raise ContractError('Дата остановки кампании не соответствует допуску.')
        groups=api.get('adgroups',campaign_ids=[campaign_id]);ads=api.get('ads',campaign_ids=[campaign_id]);keywords=api.get('keywords',campaign_ids=[campaign_id])
        if not groups or not ads or not keywords:raise ContractError('Кампания ещё не собрана.')
        for group in groups:
            if not group.get('RegionIds') or not set(group['RegionIds'])<=set(grant['region_ids']):raise ContractError('Регион группы вне допуска.')
        for ad in ads:
            normalized=normalize(ad)
            if ad.get('State') not in {'SUSPENDED','ARCHIVED'} and ad.get('Status')!='ACCEPTED':raise ContractError('WAIT_MODERATION: не все включённые объявления приняты.')
            if not policy.href_allowed(normalized.get('ResponsiveAd',{}).get('Href',''),grant):raise ContractError('Не проверена посадочная объявления.')
        for keyword in keywords:
            if keyword.get('State')!='SUSPENDED':
                check_bid(keyword.get('Bid'),grant)
                if keyword.get('Keyword')=='---autotargeting' and keyword.get('AutotargetingSearchBidIsAuto')!='NO':
                    raise ContractError('Нужно задать ручную ставку автоматически созданному автотаргетингу.')
        if not any(a.get('State') in {'ON','OFF'} and a.get('Status')=='ACCEPTED' for a in ads):raise ContractError('Нет принятого включённого объявления.')
        return {'campaign_id':campaign_id,'ready':True,'groups':len(groups),'ads':len(ads),'keywords':len(keywords)}
    def guard(self,*,stop=False):
        grant=policy.current(self.store,active=False,protective=True);api=self.api(grant);ids=policy.campaigns(self.store,grant)
        reason='OWNER_STOP' if stop else None
        revoked=self.store.connection.execute('SELECT revoked FROM direct_grants WHERE version=?',(grant['version'],)).fetchone()[0]
        if revoked:reason='REVOKED'
        if datetime.now(timezone.utc)>=instant(grant['expires_at']):reason='EXPIRED'
        spent=None
        if not reason:
            try:
                if api.currency()!=grant['currency']:raise ContractError('Currency mismatch')
                spent=api.spend(ids,instant_date(grant['starts_at']).isoformat(),datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat())
                if spent+grant['spend_buffer_micros']>=grant['max_total_budget_micros']:reason='BUDGET_STOP'
            except (DirectFailure,ContractError):reason='SPEND_UNAVAILABLE'
        if not reason:
            try:
                budgets=0
                for cid in ids:
                    campaign=api.one('campaigns',cid)
                    amount=(campaign.get('DailyBudget') or {}).get('Amount')
                    if type(amount) is not int or amount<=0:raise ContractError('Бюджет не подтверждён.')
                    budgets+=amount
                    if campaign.get('State')=='ON':self.audit_launch(api,cid,grant)
                if budgets>grant['max_daily_budget_micros']:raise ContractError('Бюджеты вне допуска.')
            except (DirectFailure,ContractError):reason='POLICY_DRIFT'
        results=[]
        if reason:
            for cid in ids:
                try:
                    if api.one('campaigns',cid).get('State')!='SUSPENDED':
                        request={'request_id':'guard-'+str(cid)+'-'+digest([reason,now()])[:20], 'action':'campaign.pause','object_id':cid,'params':{}}
                        results.append(self.perform(request))
                except (DirectFailure,ContractError):results.append({'campaign_id':cid,'status':'UNKNOWN'})
        return {'reason':reason,'spent_micros':spent,'results':results,'stopped':bool(reason) and all(r['status']=='VERIFIED' for r in results)}
