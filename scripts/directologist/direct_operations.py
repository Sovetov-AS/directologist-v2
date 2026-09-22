"""Closed high-level operation compiler; provider payloads are never accepted verbatim."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from .contracts import ContractError, canonical, digest, identifier
from .planning import integer
from . import direct_policy
from .adapters.direct_api import positive_id

SERVICE = {'campaign': 'campaigns', 'group': 'adgroups', 'ad': 'ads', 'keyword': 'keywords'}


def exact(data, keys):
    if not isinstance(data, dict) or set(data) != set(keys): raise ContractError('Неподдерживаемые поля операции.')


def text(value, maximum=255):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(x) < 32 for x in value):
        raise ContractError('Некорректный текст операции.')
    return value


def strings(value, maximum=1000):
    if not isinstance(value, list) or len(value) > maximum: raise ContractError('Неверный список строк.')
    for item in value: text(item, 4096)
    if len(value) != len(set(value)): raise ContractError('Повторяющиеся строки.')
    return value


def normalize(row):
    # Status text from supplier is untrusted; only reviewed fields are retained.
    keys = {'Id','Name','Type','State','Status','StatusClarification','StartDate','EndDate','DailyBudget','NegativeKeywords',
            'CampaignId','AdGroupId','RegionIds','Keyword','Bid','AutotargetingSettings','AutotargetingSearchBidIsAuto','UnifiedCampaign','ResponsiveAd'}
    result = {k: v for k, v in row.items() if k in keys}
    if 'NegativeKeywords' in result and result['NegativeKeywords'] is None:result['NegativeKeywords']={'Items':[]}
    if result.get('UnifiedCampaign'):
        unified=dict(result['UnifiedCampaign'])
        if isinstance(unified.get('Settings'),list):unified['Settings']={v['Option']:v['Value'] for v in unified['Settings']}
        result['UnifiedCampaign']=unified
    if 'ResponsiveAd' in result:
        ad = result['ResponsiveAd']
        result['ResponsiveAd'] = {'Titles': [t['Title'] for t in ad.get('Titles', [])],
                                  'Texts': [t['Text'] for t in ad.get('Texts', [])], 'Href': ad.get('Href')}
        result['CreativeModeration']={key:[{k:v for k,v in item.items() if k in {'Title','Text','Status','StatusClarification'}} for item in ad.get(key,[])] for key in ('Titles','Texts')}
    return result


def subset(expected, actual):
    if isinstance(expected, dict): return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k,v in expected.items())
    return expected == actual


def compile_request(context, grant, request, api, managed):
    exact(request, {'request_id','action','object_id','params'})
    identifier(request['request_id']);action=request['action'];p=request['params']
    if not isinstance(p,dict):raise ContractError('Параметры операции должны быть объектом.')
    if action not in direct_policy.OPERATIONS: raise ContractError('Неизвестная операция Direct.')
    if action not in grant['allowed_operations'] and action != 'campaign.pause': raise ContractError('OWNER_REQUIRED: операция вне допуска.')
    kind, verb=action.split('.');service=SERVICE[kind];create=verb=='create'
    if create:
        if request['object_id'] is not None: raise ContractError('Новый объект не имеет ID.')
        before=None;object_id=None
    else:
        object_id=positive_id(request['object_id']);before=normalize(api.one(service,object_id))
    campaign=None;parent=None
    if kind=='campaign' and not create: campaign=object_id
    elif kind=='group':
        campaign=p.get('campaign_id') if create else before.get('CampaignId')
    elif kind in {'ad','keyword'}:
        if create:
            parent=api.one('adgroups',positive_id(p.get('group_id')));campaign=parent['CampaignId']
        else: campaign=before.get('CampaignId')
    if kind!='campaign' and campaign is None:raise ContractError('API не подтвердил кампанию родительского объекта.')
    if campaign is not None:
        positive_id(campaign)
        if campaign not in managed: raise ContractError('OWNER_REQUIRED: кампания вне проекта/допуска.')
        camp=normalize(api.one('campaigns',campaign))
        if camp.get('Type')!='UNIFIED_CAMPAIGN': raise ContractError('Поддержана ЕПК; другие типы требуют отдельного адаптера.')
        if (create or action in {'ad.update','ad.moderate'}) and camp.get('State')!='SUSPENDED':
            raise ContractError('Сначала приостановить кампанию перед сборкой/изменением/модерацией объявлений.')
    method='add' if create else 'update';params=None;expected={}
    if action=='campaign.create':
        exact(p,{'name','start_date','end_date','daily_budget_micros','counter_ids','negative_keywords'})
        if not grant['allow_create']: raise ContractError('OWNER_REQUIRED: создание кампаний не разрешено.')
        validate_budget(p['daily_budget_micros'],grant)
        validate_dates(p['start_date'],p['end_date'],grant)
        if not isinstance(p['counter_ids'],list):raise ContractError('Неверные счётчики.')
        for i in p['counter_ids']:positive_id(i)
        counter=context.profile['bindings'].get('metrika',{}).get('resources',{}).get('counter_id')
        if set(map(str,p['counter_ids']))-({counter} if counter else set()):raise ContractError('Счётчик не выбран в подключении Метрики.')
        payload={'Name':text(p['name']),'StartDate':p['start_date'],'EndDate':p['end_date'],
                 'DailyBudget':{'Amount':p['daily_budget_micros'],'Mode':'STANDARD'},
                 'NegativeKeywords':{'Items':strings(p['negative_keywords'])},
                 'UnifiedCampaign':{'BiddingStrategy':{'Search':{'BiddingStrategyType':'HIGHEST_POSITION',
                    'PlacementTypes':{'SearchResults':'YES','ProductGallery':'NO','DynamicPlaces':'NO','Maps':'NO','SearchOrganizationList':'NO'}},
                    'Network':{'BiddingStrategyType':'SERVING_OFF'}},'AttributionModel':'AUTO',
                    'CounterIds':{'Items':p['counter_ids']},'Settings':[{'Option':'ADD_METRICA_TAG','Value':'YES'},
                    {'Option':'ENABLE_SITE_MONITORING','Value':'YES'},{'Option':'ENABLE_AREA_OF_INTEREST_TARGETING','Value':'NO'}]}}
        expected={k:payload[k] for k in ('Name','StartDate','EndDate','DailyBudget','NegativeKeywords')}
        expected['UnifiedCampaign']={k:payload['UnifiedCampaign'][k] for k in ('BiddingStrategy','CounterIds')}
        expected['UnifiedCampaign']['Settings']={v['Option']:v['Value'] for v in payload['UnifiedCampaign']['Settings']}
    elif action=='group.create':
        exact(p,{'campaign_id','name','region_ids','negative_keywords'})
        if not isinstance(p['region_ids'],list) or not p['region_ids'] or not set(p['region_ids'])<=set(grant['region_ids']):
            raise ContractError('OWNER_REQUIRED: регионы вне допуска.')
        payload={'CampaignId':campaign,'Name':text(p['name']),'RegionIds':p['region_ids'],
                 'NegativeKeywords':{'Items':strings(p['negative_keywords'])},'UnifiedAdGroup':{'OfferRetargeting':'NO'}}
        expected={k:payload[k] for k in ('CampaignId','Name','RegionIds')}
    elif action in {'ad.create','ad.update'}:
        exact(p,({'group_id'} if create else set())|{'titles','texts','href'})
        titles,texts=strings(p['titles'],7),strings(p['texts'],3)
        if not titles or not texts:raise ContractError('Нужны заголовки и тексты.')
        for t in titles:
            text(t,56)
            if any(len(w)>22 for w in t.split()):raise ContractError('Слишком длинное слово заголовка.')
        for t in texts:
            text(t,81)
            if any(len(w)>23 for w in t.split()):raise ContractError('Слишком длинное слово текста.')
        if not direct_policy.href_allowed(text(p['href'],1024),grant):raise ContractError('OWNER_REQUIRED: посадочная вне разрешённых доменов.')
        payload={'ResponsiveAd':{'Titles':titles,'Texts':texts,'Href':p['href']}}
        if create:payload['AdGroupId']=p['group_id']
        else:payload['Id']=object_id
        expected=payload.copy()
    elif action in {'keyword.create','keyword.update'}:
        exact(p,{'group_id','text','bid_micros'} if create else {'text'})
        keyword=text(p['text'],4096)
        if keyword=='---autotargeting':raise ContractError('Автотаргетинг уже создаётся платформой; управляйте его ставкой по ID.')
        payload={'Keyword':keyword}
        if create:
            check_bid(p['bid_micros'],grant);payload.update(AdGroupId=p['group_id'],Bid=p['bid_micros'])
        else:payload['Id']=object_id
        expected=payload.copy()
        if not create:
            expected.pop('Id',None);expected['AdGroupId']=before['AdGroupId']
    elif action=='keyword.autotarget':
        exact(p,{'categories','brands'})
        exact(p['categories'],{'Exact','Narrow','Alternative','Accessory','Broader'})
        exact(p['brands'],{'WithoutBrands','WithAdvertiserBrand','WithCompetitorsBrand'})
        if before.get('Keyword')!='---autotargeting':raise ContractError('Объект не является автотаргетингом.')
        for values in (p['categories'],p['brands']):
            if set(values.values())-{'YES','NO'} or 'YES' not in values.values():raise ContractError('Нужно включить хотя бы одну категорию и брендовый вариант.')
        payload={'Id':object_id,'AutotargetingSettings':{'Categories':p['categories'],'BrandOptions':p['brands']}}
        expected=payload.copy()
    elif action=='keyword.bid':
        exact(p,{'bid_micros'});check_bid(p['bid_micros'],grant)
        method='set';service='keywordbids';payload={'KeywordId':object_id,'SearchBid':p['bid_micros']}
        expected={'Id':object_id,'Bid':p['bid_micros']}
        if before['Keyword']=='---autotargeting':
            payload['AutotargetingSearchBidIsAuto']='NO';expected['AutotargetingSearchBidIsAuto']='NO'
    elif verb in {'pause','resume','moderate'}:
        if action=='keyword.resume':
            check_bid(before.get('Bid'),grant)
            if before.get('Keyword')=='---autotargeting' and before.get('AutotargetingSearchBidIsAuto')!='NO':raise ContractError('Сначала задать ручную ставку автотаргетингу.')
        if action=='ad.resume' and before.get('Status')!='ACCEPTED':raise ContractError('WAIT_MODERATION: объявление ещё не принято.')
        exact(p,set());method={'pause':'suspend','resume':'resume','moderate':'moderate'}[verb]
        params={'SelectionCriteria':{'Ids':[object_id]}}
        if verb=='moderate':
            if before.get('Status')!='DRAFT':raise ContractError('Модерация доступна для DRAFT; перечитайте состояние.')
            expected={'Status':['MODERATION','PREACCEPTED','ACCEPTED','REJECTED']}
        else:expected={'State':'SUSPENDED' if verb=='pause' else 'ON'}
    elif action=='campaign.budget':
        exact(p,{'daily_budget_micros'});validate_budget(p['daily_budget_micros'],grant)
        payload={'Id':object_id,'DailyBudget':{'Amount':p['daily_budget_micros'],'Mode':'STANDARD'}};expected=payload.copy()
    elif verb=='negatives':
        exact(p,{'negative_keywords'});payload={'Id':object_id,'NegativeKeywords':{'Items':strings(p['negative_keywords'])}};expected=payload.copy()
    else:raise ContractError('Нет компилятора операции.')
    if params is None:params={('KeywordBids' if service=='keywordbids' else {'campaign':'Campaigns','group':'AdGroups','ad':'Ads','keyword':'Keywords'}[kind]):[payload]}
    plan={'schema_version':2,'project_id':context.project_id,'context_hash':context.context_hash,
          'grant_hash':digest(grant),'request':request,'campaign_id':campaign,'before':before,
          'service':service,'method':method,'params':params,'expected':expected}
    plan['sha256']=digest(plan)
    return plan


def validate_budget(value,grant):
    integer(value)
    if not 0<value<=grant['max_daily_budget_micros']:raise ContractError('OWNER_REQUIRED: дневной бюджет вне допуска.')


def check_bid(value,grant):
    integer(value)
    if not 0<value<=grant['max_bid_micros']:raise ContractError('OWNER_REQUIRED: ставка вне допуска.')


def validate_dates(start,end,grant):
    try:a,b=date.fromisoformat(start),date.fromisoformat(end)
    except (ValueError,TypeError):raise ContractError('Неверные даты кампании.') from None
    limit=instant_date(grant['expires_at'])
    # Direct EndDate stops at 00:00 Moscow. Avoid claiming project timezone changes it.
    if not datetime.now(ZoneInfo('Europe/Moscow')).date()<=a<b<=limit:
        raise ContractError('Период кампании выходит за срок допуска или уже прошёл.')


def instant_date(value):
    from .analytics import instant
    return instant(value).astimezone(ZoneInfo('Europe/Moscow')).date()
