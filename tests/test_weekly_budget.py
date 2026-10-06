"""Synthetic weekly-only safety checks. No provider rounding assertion or network."""
import copy
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import test_direct_executor as fixtures
from directologist.contracts import ContractError, digest
from directologist.direct_policy import register, validate, revoke
from directologist.direct_operations import normalize
from directologist.adapters.direct_api import DirectFailure


class WeeklyBudgetTests(unittest.TestCase):
    def setUp(self):
        fixtures.DirectExecutorTests.setUp(self)
        today=datetime.now(ZoneInfo('Europe/Moscow')).date()
        self.monday=today-timedelta(days=today.weekday())
        self.row=self.api.objects['campaigns'][1]
        self.row.update(StartDate=str(self.monday),EndDate=str(self.monday+timedelta(days=6)),
                        NegativeKeywords={'Items':['synthetic minus']})
        self.row['UnifiedCampaign'].update(CounterIds={'Items':[10]},AttributionModel='AUTO')
        self.cost=0
        self.api.spend_rows=lambda *args:[{'CampaignId':1,'Date':str(today),'Cost':self.cost}]
        original=self.api.mutate
        def preserving_update(service,method,params):
            old=copy.deepcopy(self.row['UnifiedCampaign'])
            result=original(service,method,params)
            if service=='campaigns' and method=='update':
                new=params['Campaigns'][0].get('UnifiedCampaign')
                if new is not None:old.update(copy.deepcopy(new));self.row['UnifiedCampaign']=old
            return result
        self.api.mutate=preserving_update

    def enable(self,total=6830000000):
        self.weekly_grant=self.grant|{'version':'weekly-approved','allow_create':False,
            'max_campaigns':1,
            'allowed_operations':['campaign.weekly-budget','campaign.pause','campaign.resume','campaign.negatives'],
            'max_weekly_budget_micros':5000000000,'max_daily_budget_micros':715000000,
            'max_total_budget_micros':total,'spend_buffer_micros':730000000,'budget_vat_basis_points':2200}
        register(self.exe.store,self.weekly_grant,digest(self.weekly_grant))

    def request(self,weekly=5000000000,rid='weekly'):
        return {'request_id':rid,'action':'campaign.weekly-budget','object_id':1,
                'params':{'weekly_budget_micros':weekly}}

    def test_old_grant_never_enables_capability(self):
        with self.assertRaisesRegex(ContractError,'вне допуска'):self.exe.prepare(self.request())
        changed=self.grant|{'allowed_operations':['campaign.weekly-budget']}
        with self.assertRaises(ContractError):validate(self.ctx,changed)
        self.assertFalse(self.api.calls)

    def test_weekly_grant_rejects_ambiguous_budget_paths_and_missing_vat(self):
        self.enable()
        for changed in (self.weekly_grant|{'allow_create':True},
                        self.weekly_grant|{'allowed_operations':['campaign.weekly-budget','campaign.budget']},
                        self.weekly_grant|{'allowed_operations':['campaign.weekly-budget','campaign.search-profile']},
                        self.weekly_grant|{'max_weekly_budget_micros':True},
                        self.weekly_grant|{'max_campaigns':2},
                        {k:v for k,v in self.weekly_grant.items() if k!='budget_vat_basis_points'}):
            with self.subTest(changed=changed),self.assertRaises(ContractError):validate(self.ctx,changed)

    def test_exact_weekly_payload_and_complete_preservation(self):
        self.enable();before=normalize(copy.deepcopy(self.row));plan=self.exe.prepare(self.request())
        self.assertEqual(set(plan['params']['Campaigns'][0]),{'Id','UnifiedCampaign'})
        self.assertEqual(set(plan['params']['Campaigns'][0]['UnifiedCampaign']),{'BiddingStrategy'})
        self.assertEqual(plan['budget_restart']['spent_before_micros'],0)
        self.assertEqual(self.exe.apply(plan)['status'],'VERIFIED')
        after=normalize(copy.deepcopy(self.row))
        before['UnifiedCampaign']['BiddingStrategy']['Search']['HighestPosition']['WeeklySpendLimit']=5000000000
        before['DailyBudget']['Amount']=after['DailyBudget']['Amount']
        self.assertEqual(before,after)
        self.assertEqual(self.exe.apply(plan)['status'],'VERIFIED');self.assertEqual(len(self.api.calls),1)

    def test_bounded_daily_alias_is_not_a_guessed_division_rule(self):
        self.enable();original=self.api.mutate
        def derived(*args):
            oid=original(*args);self.row['DailyBudget']['Amount']=714290000;return oid
        self.api.mutate=derived
        self.assertEqual(self.exe.perform(self.request())['status'],'VERIFIED')

    def test_daily_alias_and_unrelated_drift_fail_closed(self):
        self.enable();plan=self.exe.prepare(self.request());after=copy.deepcopy(plan['expected'])
        for amount in (0,-1,True,715000001,'714290000'):
            after['DailyBudget']['Amount']=amount
            with self.subTest(amount=amount):self.assertFalse(self.exe._matches(plan,after))
        after['DailyBudget']['Amount']=714290000
        for field,value in [('Name','Changed'),('EndDate',str(self.monday+timedelta(days=5))),('State','OFF')]:
            changed=copy.deepcopy(after);changed[field]=value
            with self.subTest(field=field):self.assertFalse(self.exe._matches(plan,changed))
        for change in ('counter','mode','extra','weekly'):
            changed=copy.deepcopy(after)
            if change=='counter':changed['UnifiedCampaign']['CounterIds']['Items']=[11]
            elif change=='mode':changed['DailyBudget']['Mode']='DISTRIBUTED'
            elif change=='extra':changed['DailyBudget']['Extra']=1
            else:changed['UnifiedCampaign']['BiddingStrategy']['Search']['HighestPosition']['WeeklySpendLimit']=5000000001
            with self.subTest(change=change):self.assertFalse(self.exe._matches(plan,changed))

    def test_closed_params_and_financial_caps(self):
        self.enable()
        for weekly in (True,0,299999999,5000000001,5000.0):
            with self.subTest(weekly=weekly),self.assertRaises(ContractError):self.exe.prepare(self.request(weekly))
        request=self.request();request['params']['daily_budget_micros']=1
        with self.assertRaises(ContractError):self.exe.prepare(request)
        self.assertFalse(self.api.calls)

    def test_budget_restart_never_credits_pre_edit_cost(self):
        self.enable();self.cost=1000000
        with self.assertRaisesRegex(ContractError,'резерв бюджетов'):self.exe.prepare(self.request())
        self.assertFalse(self.api.calls)

    def test_persisted_restart_baseline_survives_reconcile_and_rotation(self):
        self.cost=100000000;self.enable(total=7000000000)
        original=self.api.mutate
        def lost(*args):original(*args);raise DirectFailure()
        self.api.mutate=lost
        self.assertEqual(self.exe.perform(self.request())['status'],'UNKNOWN')
        with self.assertRaises(ContractError):self.exe.prepare(self.request(rid='another'))
        revoke(self.exe.store)
        self.assertEqual(self.exe.reconcile('weekly',1)['status'],'VERIFIED')
        self.assertEqual(self.exe._weekly_restart_baseline(1,str(self.monday),self.weekly_grant),100000000)
        self.assertEqual(self.exe._weekly_restart_baseline(1,str(self.monday),self.weekly_grant|{'environment':'production'}),0)
        self.assertEqual(len(self.api.calls),1)
        rotated=self.weekly_grant|{'version':'rotated'};register(self.exe.store,rotated,digest(rotated))
        self.cost=200000000
        # Current weekly gross reserve 6100 - (200-100), plus total cost200,
        # plus buffer730 =6930. Crediting all200 would be an unsafe6830.
        too_small=rotated|{'version':'too-small','max_total_budget_micros':6900000000}
        register(self.exe.store,too_small,digest(too_small))
        with self.assertRaisesRegex(ContractError,'резерв бюджетов'):
            self.exe.prepare({'request_id':'negative','action':'campaign.negatives','object_id':1,'params':{'negative_keywords':[]}})

    def test_stale_spend_and_missing_report_never_send(self):
        self.enable(total=7000000000);plan=self.exe.prepare(self.request());self.cost=1
        with self.assertRaisesRegex(ContractError,'STALE_PLAN'):self.exe.apply(plan)
        self.api.spend_rows=lambda *args:(_ for _ in ()).throw(DirectFailure('PENDING'))
        with self.assertRaises(DirectFailure):self.exe.prepare(self.request())
        self.assertFalse(self.api.calls)

    def test_prior_or_future_week_carryover_is_unsupported(self):
        self.enable()
        for field,value in [('StartDate',str(self.monday-timedelta(days=1))),('EndDate',str(self.monday+timedelta(days=7)))]:
            before=copy.deepcopy(self.row);self.row[field]=value
            with self.subTest(field=field),self.assertRaises(ContractError):self.exe.prepare(self.request())
            self.api.objects['campaigns'][1]=before;self.row=before
        self.assertFalse(self.api.calls)

    def test_guard_checks_exact_weekly_cap_on_stopped_campaign(self):
        self.enable();self.row['UnifiedCampaign']['BiddingStrategy']['Search']['HighestPosition']['WeeklySpendLimit']=5000000001
        self.assertEqual(self.exe.guard()['reason'],'POLICY_DRIFT')

    def test_nonmanual_search_and_unknown_fields_rejected(self):
        self.enable()
        strategy=self.row['UnifiedCampaign']['BiddingStrategy']
        for mutate in (lambda s:s['Search'].update(Extra=1),
                       lambda s:s['Search'].update(BiddingStrategyType='AVERAGE_CPC'),
                       lambda s:s['Network'].update(BiddingStrategyType='NETWORK_DEFAULT'),
                       lambda s:s['Search']['PlacementTypes'].update(ProductGallery='YES')):
            original=copy.deepcopy(strategy);mutate(strategy)
            with self.assertRaises(ContractError):self.exe.prepare(self.request())
            strategy.clear();strategy.update(original)
        self.assertFalse(self.api.calls)


if __name__=='__main__':unittest.main()
