import json
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import Mock, MagicMock
from analysis_fixtures import setup
from directologist.adapters.direct_api import DirectAPI, DirectFailure
from directologist.contracts import ContractError
from directologist.secrets import Credential

class DirectAPITests(unittest.TestCase):
    def setUp(self):
        setup(self);self.opener=MagicMock();self.api=DirectAPI(Credential('SYNTHETIC_TOKEN_VALUE'), 'fixture', 'sandbox', opener=self.opener)
    def reply(self, data, status=200):
        response=Mock();response.status=status;response.read.return_value=json.dumps(data).encode() if isinstance(data,dict) else data.encode()
        self.opener.open.return_value.__enter__.return_value=response
    def test_fixed_v501_sandbox_and_no_arbitrary_methods(self):
        self.reply({'result':{'Campaigns':[]}})
        self.assertEqual(self.api.get('campaigns',ids=[1]),[])
        self.assertEqual(self.opener.open.call_args.args[0].full_url,'https://api-sandbox.direct.yandex.com/json/v501/campaigns')
        with self.assertRaises(ContractError):self.api.call('https://example.org','get',{})
        with self.assertRaises(ContractError):self.api.call('campaigns','delete',{})
    def test_successful_single_write_and_numeric_error_only(self):
        self.reply({'result':{'AddResults':[{'Id':10}]}})
        self.assertEqual(self.api.mutate('campaigns','add',{'Campaigns':[{'Name':'Synthetic'}]}),10)
        self.reply({'result':{'AddResults':[{'Errors':[{'Code':42,'Message':'PRIVATE_SENTINEL'}]}]}})
        with self.assertRaises(DirectFailure) as e:self.api.mutate('campaigns','add',{'Campaigns':[{}]})
        self.assertEqual(e.exception.outcome,'REJECTED');self.assertNotIn('PRIVATE',str(e.exception))
    def test_lost_response_never_retried(self):
        self.opener.open.side_effect=TimeoutError('PRIVATE_SENTINEL')
        with self.assertRaises(DirectFailure) as e:self.api.mutate('campaigns','suspend',{'SelectionCriteria':{'Ids':[1]}})
        self.assertEqual(e.exception.outcome,'UNKNOWN');self.assertEqual(self.opener.open.call_count,1)
        self.assertNotIn('PRIVATE',str(e.exception))
    def test_wrong_cardinality_or_foreign_read_rejected(self):
        self.reply({'result':{'AddResults':[{'Id':1},{'Id':2}]}})
        with self.assertRaises(DirectFailure):self.api.mutate('campaigns','add',{'Campaigns':[{}]})
        self.reply({'result':{'Campaigns':[{'Id':2}]}})
        with self.assertRaises(DirectFailure):self.api.one('campaigns',1)
    def test_spend_is_micros_with_vat_and_scope_checks(self):
        self.reply('Date\tCampaignId\tCost\n2026-09-20\t1\t123000000\n')
        self.assertEqual(self.api.spend([1],'2026-09-20','2026-09-20'),123000000)
        request=self.opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Returnmoneyinmicros'),'true')
        self.assertEqual(json.loads(request.data)['params']['IncludeVAT'],'YES')
        self.reply('Date\tCampaignId\tCost\n2026-09-20\t2\t123\n')
        with self.assertRaises(DirectFailure):self.api.spend([1],'2026-09-20','2026-09-20')
    def test_pending_and_secret_echo_not_returned(self):
        self.reply('',202)
        with self.assertRaises(DirectFailure) as e:self.api.spend([1],'2026-09-20','2026-09-20')
        self.assertEqual(e.exception.outcome,'PENDING')
        self.reply({'result':{'Campaigns':[{'Id':1,'Name':'SYNTHETIC_TOKEN_VALUE'}]}})
        with self.assertRaises(DirectFailure):self.api.one('campaigns',1)

    def test_detail_reports_preserve_scope_and_exact_cost(self):
        header='Date\tCampaignId\tAdGroupId\tCriterionId\tCriterion\tQuery\tImpressions\tClicks\tCost\n'
        self.reply(header+'2026-09-20\t1\t2\t3\tsynthetic keyword\tsynthetic query\t10\t2\t123000001\n')
        result=self.api.performance([1],'2026-09-20','2026-09-20',queries=True)
        self.assertEqual(result['rows'][0]['Cost'],123000001)
        spec=json.loads(self.opener.open.call_args.args[0].data)['params']
        self.assertEqual(spec['ReportType'],'SEARCH_QUERY_PERFORMANCE_REPORT');self.assertIn('Query',spec['FieldNames'])
        self.reply(header+'2026-09-20\t999\t2\t3\tk\tq\t10\t2\t1\n')
        with self.assertRaises(DirectFailure):self.api.performance([1],'2026-09-20','2026-09-20',queries=True)
        with self.assertRaises(ContractError):self.api.performance([1],'2026-01-01','2026-09-20')

    def test_detail_goal_is_explicit_and_missing_is_not_zero(self):
        self.reply('Date\tCampaignId\tAdGroupId\tCriterionId\tCriterion\tImpressions\tClicks\tCost\tConversions_9_AUTO\n2026-09-20\t1\t2\t3\tk\t10\t2\t123\t--\n')
        result=self.api.performance([1],'2026-09-20','2026-09-20',goal='9')
        self.assertIsNone(result['rows'][0]['Conversions_9_AUTO'])
        spec=json.loads(self.opener.open.call_args.args[0].data)['params']
        self.assertEqual(spec['Goals'],['9']);self.assertEqual(spec['AttributionModels'],['AUTO'])
        self.assertEqual(result['goal_id'],'9')

class DirectCompilerTests(unittest.TestCase):
    def setUp(self):
        setup(self)
        from directologist.direct_policy import OPERATIONS
        self.grant={'allowed_operations':sorted(OPERATIONS),'allow_create':True,'max_daily_budget_micros':1000000000,
                    'max_bid_micros':10000000,'region_ids':[225],'allowed_domains':['example.org'],'expires_at':'2099-01-01T00:00:00Z'}
        self.api=Mock()
        self.api.one.side_effect=lambda service,oid: {'Id':oid,'CampaignId':1,'Type':'UNIFIED_CAMPAIGN','State':'SUSPENDED'}
    def compile(self,action,params):
        from directologist.direct_operations import compile_request
        return compile_request(self.ctx,self.grant,{'request_id':'synthetic','action':action,'object_id':None,'params':params},self.api,[1])
    def test_new_campaign_is_native_unified_search_and_bounded(self):
        from datetime import datetime,timedelta,timezone
        start=datetime.now(ZoneInfo("Europe/Moscow")).date()
        p=self.compile('campaign.create',{'name':'Synthetic','start_date':str(start),'end_date':str(start+timedelta(days=2)),
             'daily_budget_micros':100000000,'counter_ids':[],'negative_keywords':[]})
        settings=p['params']['Campaigns'][0]['UnifiedCampaign']
        self.assertEqual(settings['BiddingStrategy']['Network']['BiddingStrategyType'],'SERVING_OFF')
        self.assertNotIn('TextCampaign',p['params']['Campaigns'][0])
    def test_new_ad_uses_responsive_schema(self):
        p=self.compile('ad.create',{'group_id':2,'titles':['Synthetic title'],'texts':['Synthetic text'],'href':'https://example.org/'})
        self.assertEqual(p['params']['Ads'][0]['ResponsiveAd']['Titles'],['Synthetic title'])
    def test_extra_fields_and_foreign_landing_rejected(self):
        p={'group_id':2,'titles':['Title'],'texts':['Text'],'href':'https://other.example/'}
        with self.assertRaises(ContractError):self.compile('ad.create',p)
        with self.assertRaises(ContractError):self.compile('ad.create',p|{'raw_payload':{}})

class DirectOnboardingTests(unittest.TestCase):
    def setUp(self):setup(self)
    def test_empty_account_can_be_checked_for_campaign_creation(self):
        from directologist.adapters import Adapter
        from directologist.contracts import load_project
        transport=Mock();transport.request.side_effect=[{'result':{'Clients':[{'Login':'fixture'}]}},{'result':{'Campaigns':[]}}]
        result=Adapter(transport).probe('direct',Credential('SYNTHETIC_TOKEN_VALUE'),{},lambda label,choices,multiple:choices)
        self.assertEqual(result.state,'CHECKED');self.assertEqual(result.resources['campaign_ids'],[])
        data=self.ctx.profile;data['bindings']['direct']['resources']=result.resources
        (self.ctx.directory/'profile.json').write_text(json.dumps(data))
        self.assertEqual(load_project(self.root,'fixture').profile['bindings']['direct']['resources']['campaign_ids'],[])
    def test_sandbox_probe_never_uses_production_origin(self):
        from directologist.adapters import Adapter
        transport=Mock();transport.request.side_effect=[{'result':{'Clients':[{'Login':'fixture'}]}},{'result':{'Campaigns':[]}}]
        Adapter(transport).probe('direct',Credential('SYNTHETIC_TOKEN_VALUE'),{'environment':'sandbox'},lambda l,c,m:c)
        self.assertTrue(all(c.args[0].startswith('https://api-sandbox.direct.yandex.com/') for c in transport.request.call_args_list))
