import copy
import json
import unittest
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from unittest.mock import Mock
from analysis_fixtures import setup
from directologist.contracts import ContractError,digest
from directologist.direct_policy import OPERATIONS,register,revoke
from directologist.direct_executor import DirectExecutor
from directologist.adapters.direct_api import DirectFailure

class FakeDirect:
    environment='sandbox';client_login='fixture'
    def __init__(self):
        self.calls=[];self.cost=0;self.objects={'campaigns':{},'adgroups':{},'ads':{},'keywords':{}};self.next_id=10
    def currency(self):return 'RUB'
    def spend(self,*args):return self.cost
    def one(self,service,oid):
        if oid not in self.objects[service]:raise DirectFailure('NOT_FOUND')
        return copy.deepcopy(self.objects[service][oid])
    def get(self,service,*,campaign_ids=None,group_ids=None,ids=None):
        return [self.one(service,i) for i,v in self.objects[service].items() if (ids and i in ids) or (campaign_ids and v.get('CampaignId') in campaign_ids) or (group_ids and v.get('AdGroupId') in group_ids)]
    def mutate(self,service,method,params):
        self.calls.append((service,method,copy.deepcopy(params)))
        if method in {'suspend','resume','moderate'}:
            oid=params['SelectionCriteria']['Ids'][0];o=self.objects[service][oid]
            if method=='moderate':o['Status']='MODERATION'
            else:o['State']='SUSPENDED' if method=='suspend' else 'ON'
            return oid
        key={'campaigns':'Campaigns','adgroups':'AdGroups','ads':'Ads','keywords':'Keywords','keywordbids':'KeywordBids'}[service]
        value=copy.deepcopy(params[key][0]);oid=value.get('Id',value.get('KeywordId'))
        if method=='add':oid=self.next_id;self.next_id+=1
        if service=='keywordbids':self.objects['keywords'][oid]['Bid']=value['SearchBid'];self.objects['keywords'][oid].update({k:v for k,v in value.items() if k=='AutotargetingSearchBidIsAuto'});return oid
        if 'ResponsiveAd' in value:
            ad=value['ResponsiveAd'];ad['Titles']=[{'Title':t} for t in ad['Titles']];ad['Texts']=[{'Text':t} for t in ad['Texts']]
        if method=='add':
            value.update(Id=oid,State='ON',Status='DRAFT')
            if service=='campaigns':value['Type']='UNIFIED_CAMPAIGN'
            if service in {'ads','keywords'}:value['CampaignId']=self.objects['adgroups'][value['AdGroupId']]['CampaignId']
            self.objects[service][oid]=value
            if service=='adgroups':
                auto=self.next_id;self.next_id+=1
                self.objects['keywords'][auto]={'Id':auto,'AdGroupId':oid,'CampaignId':value['CampaignId'],'Keyword':'---autotargeting','Bid':1000000,'AutotargetingSearchBidIsAuto':'YES','State':'ON','Status':'ACCEPTED'}
        else:self.objects[service][oid].update(value)
        return oid


def grant(ctx):
    clock=datetime.now(timezone.utc)
    return dict(schema_version=2,project_id=ctx.project_id,context_hash=ctx.context_hash,version='test-v1',environment='sandbox',
        client_login='fixture',currency='RUB',starts_at=(clock-timedelta(minutes=1)).isoformat(),expires_at=(clock+timedelta(days=10)).isoformat(),
        campaign_ids=[1],allow_create=True,allowed_operations=sorted(OPERATIONS),allowed_domains=['example.org'],region_ids=[225],
        max_campaigns=10,max_operations_per_day=100,max_daily_budget_micros=1000000000,max_total_budget_micros=10000000000,
        max_bid_micros=100000000,spend_buffer_micros=100000000,trust_mode='trusted-local',accept_delayed_spend=True,approval_source='SYNTHETIC TEST ONLY')

def campaign():
    today=datetime.now(ZoneInfo("Europe/Moscow")).date()
    return {'Id':1,'Name':'Synthetic','Type':'UNIFIED_CAMPAIGN','State':'SUSPENDED','Status':'ACCEPTED','StartDate':str(today),
            'EndDate':str(today+timedelta(days=2)),'DailyBudget':{'Amount':100000000,'Mode':'STANDARD'},
            'UnifiedCampaign':{'Settings':[{'Option':'ENABLE_AREA_OF_INTEREST_TARGETING','Value':'NO'}],'BiddingStrategy':{'Search':{'BiddingStrategyType':'HIGHEST_POSITION','PlacementTypes':{'SearchResults':'YES','ProductGallery':'NO','DynamicPlaces':'NO','Maps':'NO','SearchOrganizationList':'NO'}},'Network':{'BiddingStrategyType':'SERVING_OFF'}}}}

class DirectExecutorTests(unittest.TestCase):
    def setUp(self):
        setup(self);self.api=FakeDirect();self.api.objects['campaigns'][1]=campaign()
        self.exe=DirectExecutor(self.ctx,self.api);self.addCleanup(self.exe.__exit__);self.grant=grant(self.ctx)
        register(self.exe.store,self.grant,digest(self.grant))
    def request(self,action='campaign.budget',params=None,oid=1,rid='one'):
        return {'request_id':rid,'action':action,'object_id':oid,'params':params if params is not None else {'daily_budget_micros':200000000}}
    def test_journal_before_send_repeated_plan_never_sends(self):
        original=self.api.mutate
        def apply(*args):
            self.assertEqual(self.exe.status('one')['status'],'STARTED');return original(*args)
        self.api.mutate=apply;plan=self.exe.prepare(self.request())
        self.assertEqual(self.exe.apply(plan)['status'],'VERIFIED')
        self.assertEqual(self.exe.apply(plan)['status'],'VERIFIED');self.assertEqual(len(self.api.calls),1)
    def test_stale_before_grant_and_out_of_scope_rejected(self):
        plan=self.exe.prepare(self.request());self.api.objects['campaigns'][1]['Name']='Externally changed'
        with self.assertRaises(ContractError):self.exe.apply(plan)
        with self.assertRaises(ContractError):self.exe.prepare(self.request(oid=2))
        revoke(self.exe.store)
        with self.assertRaises(ContractError):self.exe.prepare(self.request())
        self.assertFalse(self.api.calls)
    def test_timeout_after_send_reconcile_without_second_write(self):
        original=self.api.mutate
        def lost(*args):original(*args);raise DirectFailure()
        self.api.mutate=lost
        self.assertEqual(self.exe.perform(self.request())['status'],'UNKNOWN')
        self.assertEqual(self.exe.perform(self.request())['status'],'UNKNOWN')
        self.assertEqual(self.exe.reconcile('one',1)['status'],'VERIFIED');self.assertEqual(len(self.api.calls),1)
    def test_unknown_create_blocks_following_create(self):
        self.api.mutate=lambda *a:(_ for _ in ()).throw(DirectFailure())
        self.assertEqual(self.exe.perform(self.request())['status'],'UNKNOWN')
        with self.assertRaises(ContractError):self.exe.prepare(self.request(rid='two'))
    def test_budget_stop_still_allows_suspension(self):
        self.api.objects['campaigns'][1]['State']='ON';self.api.cost=self.grant['max_total_budget_micros']
        with self.assertRaises(ContractError):self.exe.prepare(self.request())
        result=self.exe.guard();self.assertEqual(result['reason'],'BUDGET_STOP');self.assertTrue(result['stopped'])
        self.assertEqual(self.api.objects['campaigns'][1]['State'],'SUSPENDED')
    def test_new_campaign_and_child_ad_persist_real_ids(self):
        today=datetime.now(ZoneInfo("Europe/Moscow")).date()
        r=self.request('campaign.create',{'name':'New synthetic','start_date':str(today),'end_date':str(today+timedelta(days=2)),
            'daily_budget_micros':100000000,'counter_ids':[],'negative_keywords':[]},None,'create')
        cid=self.exe.perform(r)['object_id'];self.assertEqual(cid,10)
        self.exe.perform(self.request('campaign.pause',{},cid,'pause'))
        gid=self.exe.perform(self.request('group.create',{'campaign_id':cid,'name':'Group','region_ids':[225],'negative_keywords':[]},None,'group'))['object_id']
        result=self.exe.perform(self.request('ad.create',{'group_id':gid,'titles':['Title'],'texts':['Text'],'href':'https://example.org/'},None,'ad'))
        self.assertEqual(result['status'],'VERIFIED');self.assertEqual(result['object_id'],13)
    def test_ad_moderation_is_not_resume_and_launch_audits_autotargeting(self):
        self.api.objects['adgroups'][2]={'Id':2,'CampaignId':1,'RegionIds':[225]}
        self.api.objects['ads'][3]={'Id':3,'AdGroupId':2,'CampaignId':1,'State':'ON','Status':'DRAFT','ResponsiveAd':{'Titles':[{'Title':'Title'}],'Texts':[{'Text':'Text'}],'Href':'https://example.org/'}}
        self.api.objects['keywords'][4]={'Id':4,'AdGroupId':2,'CampaignId':1,'Keyword':'---autotargeting','Bid':1000000,'AutotargetingSearchBidIsAuto':'YES','State':'ON','Status':'ACCEPTED'}
        self.assertEqual(self.exe.perform(self.request('ad.moderate',{},3))['status'],'VERIFIED')
        with self.assertRaises(ContractError):self.exe.prepare(self.request('campaign.resume',{},1,'launch'))
        self.api.objects['ads'][3]['Status']='ACCEPTED'
        with self.assertRaises(ContractError):self.exe.prepare(self.request('campaign.resume',{},1,'launch'))
        self.exe.perform(self.request('keyword.bid',{'bid_micros':1000000},4,'auto-bid'))
        self.assertEqual(self.exe.perform(self.request('campaign.resume',{},1,'launch'))['status'],'VERIFIED')
    def test_no_policy_no_confirmation_and_restore_no_write(self):
        with self.assertRaises(ContractError):register(self.exe.store,self.grant,'wrong')
        with self.exe.store.transaction():self.exe.store.connection.execute("UPDATE metadata SET value='1' WHERE key='recovery_required'")
        with self.assertRaises(ContractError):self.exe.prepare(self.request())
        self.assertFalse(self.api.calls)

    def test_workflow_build_wait_then_resume_no_duplicate_objects(self):
        from directologist.direct_workflow import build,cycle
        today=datetime.now(ZoneInfo("Europe/Moscow")).date()
        spec={'request_id':'launch-test','campaign':{'name':'End to end','start_date':str(today),'end_date':str(today+timedelta(days=2)),
              'daily_budget_micros':100000000,'counter_ids':[],'negative_keywords':[]},
              'groups':[{'name':'Demand','region_ids':[225],'negative_keywords':[],
                'keywords':[{'text':'synthetic demand','bid_micros':1000000}],
                'ads':[{'titles':['Synthetic title'],'texts':['Synthetic text'],'href':'https://example.org/'}]}],
              'autotarget_bid_micros':1000000,'autotargeting':{'categories':{'Exact':'YES','Narrow':'YES','Alternative':'NO','Accessory':'NO','Broader':'NO'},
                'brands':{'WithoutBrands':'YES','WithAdvertiserBrand':'YES','WithCompetitorsBrand':'NO'}},'auto_start':True}
        waiting=build(self.exe,spec);self.assertEqual(waiting['status'],'WAIT_MODERATION')
        count=len(self.api.calls)
        self.assertEqual(build(self.exe,spec)['status'],'WAIT_MODERATION');self.assertEqual(len(self.api.calls),count)
        for ad in self.api.objects['ads'].values():ad['Status']='ACCEPTED';ad['State']='OFF'
        self.assertEqual(build(self.exe,spec)['status'],'LAUNCHED')
        self.assertEqual(len(self.api.calls),count+1)
        self.assertEqual(build(self.exe,spec)['status'],'LAUNCHED');self.assertEqual(len(self.api.calls),count+1)
        self.assertEqual(cycle(self.exe)['status'],'OBSERVE')
        self.api.objects['campaigns'][waiting['campaign_id']]['State']='SUSPENDED'
        self.assertEqual(build(self.exe,spec)['status'],'STATE_CHANGED');self.assertEqual(len(self.api.calls),count+1)
        spec['campaign']['name']='Changed'
        with self.assertRaises(ContractError):build(self.exe,spec)

    def test_revoked_grant_can_stop_and_reconcile_but_not_write(self):
        original=self.api.mutate
        def lost(*args):original(*args);raise DirectFailure()
        self.api.mutate=lost
        self.assertEqual(self.exe.perform(self.request())['status'],'UNKNOWN')
        revoke(self.exe.store)
        self.assertEqual(self.exe.reconcile('one',1)['status'],'VERIFIED')
        with self.assertRaises(ContractError):self.exe.prepare(self.request(rid='forbidden'))
        self.api.mutate=original;self.api.objects['campaigns'][1]['State']='ON'
        self.assertTrue(self.exe.guard()['stopped'])

    def test_reconcile_uses_historical_grant_after_rotation(self):
        original=self.api.mutate
        def lost(*args):original(*args);raise DirectFailure()
        self.api.mutate=lost;self.exe.perform(self.request())
        changed=self.grant|{'version':'test-v2'};register(self.exe.store,changed,digest(changed))
        self.assertEqual(self.exe.reconcile('one',1)['status'],'VERIFIED')

    def test_weekly_reservation_and_resume_bid_limit(self):
        changed=self.grant|{'version':'test-small','max_total_budget_micros':1000000000}
        register(self.exe.store,changed,digest(changed))
        with self.assertRaises(ContractError):self.exe.prepare(self.request())
        self.api.objects['keywords'][4]={'Id':4,'AdGroupId':2,'CampaignId':1,'Keyword':'expensive','Bid':999999999,'State':'SUSPENDED'}
        with self.assertRaises(ContractError):self.exe.prepare(self.request('keyword.resume',{},4))
        self.assertFalse(self.api.calls)

    def test_blueprint_invalid_landing_rejected_before_creation(self):
        from directologist.direct_workflow import preflight
        spec={'campaign':{'name':'Synthetic','start_date':campaign()['StartDate'],'end_date':campaign()['EndDate'],
              'daily_budget_micros':100000000,'counter_ids':[],'negative_keywords':[]},'auto_start':False,
              'autotarget_bid_micros':1000000,'autotargeting':{'categories':{'Exact':'YES','Narrow':'NO','Alternative':'NO','Accessory':'NO','Broader':'NO'},
              'brands':{'WithoutBrands':'YES','WithAdvertiserBrand':'NO','WithCompetitorsBrand':'NO'}},
              'groups':[{'name':'Group','region_ids':[225],'negative_keywords':[],'keywords':[],
              'ads':[{'titles':['Title'],'texts':['Text'],'href':'https://foreign.example/'}]}]}
        with self.assertRaises(ContractError):preflight(self.exe,spec,self.grant)
        self.assertFalse(self.api.calls)

    def test_missing_parent_cannot_bypass_campaign_scope(self):
        self.api.objects['keywords'][4]={'Id':4,'AdGroupId':2,'Keyword':'synthetic','Bid':1000000,'State':'ON'}
        with self.assertRaises(ContractError):self.exe.prepare(self.request('keyword.bid',{'bid_micros':2000000},4))
        self.assertFalse(self.api.calls)
