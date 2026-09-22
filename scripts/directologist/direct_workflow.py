"""Resumable campaign assembly and one monitoring iteration, driven by Codex."""
from . import direct_policy as policy
from .contracts import ContractError, digest, identifier
from .direct_operations import exact, check_bid, compile_request


def preflight(executor, blueprint, grant):
    """Validate all requested content with the same compiler before creating anything."""
    class Parents:
        def one(self,service,oid):
            return {'Id':oid,'CampaignId':1,'AdGroupId':2,'Type':'UNIFIED_CAMPAIGN','State':'SUSPENDED',
                    'Status':'DRAFT','Keyword':'---autotargeting','Bid':blueprint['autotarget_bid_micros']}
    def check(action,params,oid=None):
        compile_request(executor.context,grant,{'request_id':'preflight','action':action,'object_id':oid,'params':params},Parents(),[1])
    check('campaign.create',blueprint['campaign'])
    if blueprint['auto_start']:check('campaign.resume',{},1)
    for group in blueprint['groups']:
        check('group.create',{k:group[k] for k in ('name','region_ids','negative_keywords')}|{'campaign_id':1})
        check('keyword.autotarget',blueprint['autotargeting'],3)
        check('keyword.bid',{'bid_micros':blueprint['autotarget_bid_micros']},3)
        for keyword in group['keywords']:check('keyword.create',keyword|{'group_id':2})
        for ad in group['ads']:check('ad.create',ad|{'group_id':2});check('ad.moderate',{},3)


def build(executor, blueprint):
    exact(blueprint,{'request_id','campaign','groups','autotarget_bid_micros','autotargeting','auto_start'})
    identifier(blueprint['request_id'])
    if type(blueprint['auto_start']) is not bool or not isinstance(blueprint['groups'],list) or not 1<=len(blueprint['groups'])<=20:
        raise ContractError('Неверная структура сборки кампании.')
    grant=policy.current(executor.store);api=executor.api(grant)
    check_bid(blueprint['autotarget_bid_micros'],grant)
    for group in blueprint['groups']:
        exact(group,{'name','region_ids','negative_keywords','keywords','ads'})
        if not isinstance(group['keywords'],list) or not 1<=len(group['keywords'])<=100 or not isinstance(group['ads'],list) or not 1<=len(group['ads'])<=3:
            raise ContractError('В группе нужны 1–100 ключей и 1–3 объявления.')
        for keyword in group['keywords']:exact(keyword,{'text','bid_micros'})
        for ad in group['ads']:exact(ad,{'titles','texts','href'})
    with executor.store.transaction():
        executor.store.connection.execute('CREATE TABLE IF NOT EXISTS direct_builds(request_id TEXT PRIMARY KEY,blueprint_hash TEXT)')
        row=executor.store.connection.execute('SELECT blueprint_hash FROM direct_builds WHERE request_id=?',(blueprint['request_id'],)).fetchone()
        if row and row[0]!=digest(blueprint):raise ContractError('Сборка с этим request_id уже имеет другие параметры.')
        if not row:preflight(executor,blueprint,grant)
        executor.store.connection.execute('INSERT OR IGNORE INTO direct_builds VALUES (?,?)',(blueprint['request_id'],digest(blueprint)))
    def step(label,action,oid,params):
        rid='build-'+digest([blueprint['request_id'],label])[:40]
        result=executor.perform({'request_id':rid,'action':action,'object_id':oid,'params':params})
        if result['status']!='VERIFIED':raise ContractError('BUILD_PAUSED: '+rid+' '+result['status']+'; сначала сверка, не повтор API.')
        return result['object_id']
    cid=step('campaign','campaign.create',None,blueprint['campaign'])
    # New campaigns have no ads. Suspend and reread before any ad can be moderated.
    pause_key='build-'+digest([blueprint['request_id'],'pause'])[:40]
    already_built=executor.status(pause_key)
    if not already_built or already_built['status']!='VERIFIED':step('pause','campaign.pause',cid,{})
    group_ids=[];ad_ids=[]
    for i,group in enumerate(blueprint['groups']):
        gid=step(f'g-{i}','group.create',None,{k:group[k] for k in ('name','region_ids','negative_keywords')}|{'campaign_id':cid});group_ids.append(gid)
        auto=[k for k in api.get('keywords',group_ids=[gid]) if k.get('Keyword')=='---autotargeting']
        if len(auto)!=1:raise ContractError('Не найден однозначный автотаргетинг группы; требуется сверка API.')
        step(f'auto-settings-{i}','keyword.autotarget',auto[0]['Id'],blueprint['autotargeting'])
        step(f'auto-bid-{i}','keyword.bid',auto[0]['Id'],{'bid_micros':blueprint['autotarget_bid_micros']})
        for j,keyword in enumerate(group['keywords']):step(f'kw-{i}-{j}','keyword.create',None,keyword|{'group_id':gid})
        for j,ad in enumerate(group['ads']):
            aid=step(f'ad-{i}-{j}','ad.create',None,ad|{'group_id':gid});ad_ids.append(aid)
            current=api.one('ads',aid)
            if current.get('Status')=='DRAFT':step(f'moderate-{i}-{j}','ad.moderate',aid,{})
    ads=[api.one('ads',a) for a in ad_ids]
    statuses={a['Id']:a.get('Status') for a in ads}
    if any(s=='REJECTED' for s in statuses.values()):return {'campaign_id':cid,'status':'MODERATION_REJECTED','ads':statuses}
    if any(s!='ACCEPTED' for s in statuses.values()):return {'campaign_id':cid,'status':'WAIT_MODERATION','ads':statuses,'resume':'Повторить эту же сборку; выполненные записи не повторяются.'}
    if blueprint['auto_start']:
        step('start','campaign.resume',cid,{})
        state=api.one('campaigns',cid).get('State')
        return {'campaign_id':cid,'status':'LAUNCHED' if state=='ON' else 'STATE_CHANGED','state':state,'groups':group_ids,'ads':statuses}
    state=api.one('campaigns',cid).get('State')
    return {'campaign_id':cid,'status':'READY_SUSPENDED' if state=='SUSPENDED' else 'STATE_CHANGED','state':state,'groups':group_ids,'ads':statuses}


def cycle(executor):
    guard=executor.guard()
    if guard['reason']:return {'status':'STOPPED' if guard['stopped'] else 'STOP_UNCONFIRMED','guard':guard}
    grant=policy.current(executor.store);api=executor.api(grant)
    campaigns=[]
    for cid in policy.campaigns(executor.store,grant):
        campaign=api.one('campaigns',cid)
        ads=api.get('ads',campaign_ids=[cid])
        campaigns.append({'id':cid,'name':campaign.get('Name'),'state':campaign.get('State'),'status':campaign.get('Status'),
                          'daily_budget':campaign.get('DailyBudget'),'ads':[{'id':a['Id'],'status':a.get('Status'),'state':a.get('State')} for a in ads]})
    return {'status':'OBSERVE','guard':guard,'campaigns':campaigns,
            'next_step':'Codex: сверить отчёты/CRM и запросы, подготовить обоснованные изменения; direct prepare/apply в пределах grant.',
            'automatic_optimization_rule':None}
