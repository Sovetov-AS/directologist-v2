"""Offline safety regression tests; no credentials and no network."""
import copy
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import test_direct_executor as executor_tests
from directologist.contracts import ContractError, digest
from directologist.direct_operations import validate_end_date, validate_dates
from directologist.direct_policy import register


class SearchProfileTests(unittest.TestCase):
    setUp = executor_tests.DirectExecutorTests.setUp
    request = executor_tests.DirectExecutorTests.request

    def profile(self):
        today=datetime.now(ZoneInfo('Europe/Moscow')).date()
        return self.request('campaign.search-profile',{
            'start_date':str(today+timedelta(days=2)),
            'end_date':str(today+timedelta(days=8)),
            'daily_budget_micros':50000000})

    def complete_snapshot(self):
        row=self.api.objects['campaigns'][1]
        row['EndDate']=None
        row['NegativeKeywords']={'Items':['synthetic excluded intent']}
        row['UnifiedCampaign'].update(CounterIds={'Items':[10,11]},AttributionModel='AUTO')
        search=row['UnifiedCampaign']['BiddingStrategy']['Search']
        search['HighestPosition']={'WeeklySpendLimit':700000000}
        search['PlacementTypes']['ProductGallery']='YES'
        row['UnifiedCampaign']['Settings']=[{'Option':'ENABLE_AREA_OF_INTEREST_TARGETING','Value':'YES'},
                                           {'Option':'ADD_METRICA_TAG','Value':'YES'}]
        original=self.api.mutate
        def preserving_update(service,method,params):
            old=copy.deepcopy(row['UnifiedCampaign'])
            result=original(service,method,params)
            if service=='campaigns' and method=='update':
                new=params['Campaigns'][0].get('UnifiedCampaign')
                if new is not None:
                    old.update(copy.deepcopy(new));row['UnifiedCampaign']=old
            return result
        self.api.mutate=preserving_update
        return row

    def test_atomic_reduction_uses_prospective_dates_and_budget(self):
        row=self.complete_snapshot()
        changed=self.grant|{'version':'smaller','max_daily_budget_micros':50000000,
                           'max_total_budget_micros':450000000}
        register(self.exe.store,changed,digest(changed))
        plan=self.exe.prepare(self.profile())
        payload=plan['params']['Campaigns'][0]
        self.assertEqual(set(payload),{'Id','StartDate','EndDate','UnifiedCampaign'})
        self.assertEqual(plan['expected']['DailyBudget'],{'Amount':50000000,'Mode':'STANDARD'})
        self.assertEqual(set(payload['UnifiedCampaign']),{'BiddingStrategy'})
        self.assertEqual(payload['UnifiedCampaign']['BiddingStrategy']['Search']['HighestPosition']['WeeklySpendLimit'],350000000)
        self.assertEqual(self.exe.apply(plan)['status'],'VERIFIED')
        self.assertEqual(row['State'],'SUSPENDED')
        self.assertEqual(row['UnifiedCampaign']['CounterIds'],{'Items':[10,11]})
        self.assertEqual(self.exe.apply(plan)['status'],'VERIFIED')
        self.assertEqual(len(self.api.calls),1)

    def test_old_grant_cannot_use_new_capability(self):
        self.complete_snapshot()
        old=self.grant|{'version':'old','allowed_operations':[op for op in self.grant['allowed_operations'] if op!='campaign.search-profile']}
        register(self.exe.store,old,digest(old))
        with self.assertRaisesRegex(ContractError,'вне допуска'):self.exe.prepare(self.profile())
        self.assertFalse(self.api.calls)

    def test_missing_provider_daily_conversion_stays_partial(self):
        row=self.complete_snapshot();original=self.api.mutate
        old_daily=copy.deepcopy(row['DailyBudget'])
        def no_conversion(*args):
            result=original(*args);row['DailyBudget']=old_daily;return result
        self.api.mutate=no_conversion
        self.assertEqual(self.exe.perform(self.profile())['status'],'PARTIAL')
        self.assertEqual(len(self.api.calls),1)

    def test_old_dual_budget_plan_is_stale_before_send(self):
        self.complete_snapshot();plan=self.exe.prepare(self.profile())
        plan['params']['Campaigns'][0]['DailyBudget']=copy.deepcopy(plan['expected']['DailyBudget'])
        plan['sha256']=digest({k:v for k,v in plan.items() if k!='sha256'})
        with self.assertRaisesRegex(ContractError,'STALE_PLAN'):self.exe.apply(plan)
        self.assertFalse(self.api.calls)

    def test_rejection_diagnostics_are_durable_without_replay(self):
        from directologist.adapters.direct_api import DirectFailure
        self.complete_snapshot();calls=[]
        def reject(*args):
            calls.append(args)
            raise DirectFailure('REJECTED',4004,fields=('WeeklySpendLimit','DailyBudget','PRIVATE_SENTINEL'))
        self.api.mutate=reject
        result=self.exe.perform(self.profile())
        self.assertEqual(result['status'],'REJECTED')
        self.assertEqual(result['error'],'REJECTED:4004;fields=DailyBudget,WeeklySpendLimit')
        self.assertEqual(self.exe.perform(self.profile()),result)
        self.assertEqual(len(calls),1)

    def test_unknown_fields_active_campaign_or_strategy_fail_closed(self):
        row=self.complete_snapshot();request=self.profile()
        request['params']['raw_payload']={}
        with self.assertRaises(ContractError):self.exe.prepare(request)
        row['State']='ON'
        with self.assertRaises(ContractError):self.exe.prepare(self.profile())
        row['State']='SUSPENDED'
        row['UnifiedCampaign']['BiddingStrategy']['Search']['BiddingStrategyType']='WB_MAXIMUM_CLICKS'
        with self.assertRaises(ContractError):self.exe.prepare(self.profile())
        self.assertFalse(self.api.calls)

    def test_both_budget_increases_and_missing_snapshot_are_rejected(self):
        row=self.complete_snapshot();original=copy.deepcopy(row)
        for daily,weekly in [(40000000,700000000),(100000000,300000000),(100000000,None)]:
            with self.subTest(daily=daily,weekly=weekly):
                row['DailyBudget']['Amount']=daily
                row['UnifiedCampaign']['BiddingStrategy']['Search']['HighestPosition']={'WeeklySpendLimit':weekly}
                with self.assertRaises(ContractError):self.exe.prepare(self.profile())
        row.clear();row.update(original);del row['UnifiedCampaign']['CounterIds']
        with self.assertRaises(ContractError):self.exe.prepare(self.profile())
        self.assertFalse(self.api.calls)

    def test_unknown_strategy_fields_or_budget_mode_are_rejected(self):
        row=self.complete_snapshot()
        row['UnifiedCampaign']['BiddingStrategy']['UnknownSetting']={}
        with self.assertRaises(ContractError):self.exe.prepare(self.profile())
        del row['UnifiedCampaign']['BiddingStrategy']['UnknownSetting']
        row['DailyBudget']['Mode']='DISTRIBUTED'
        with self.assertRaises(ContractError):self.exe.prepare(self.profile())
        self.assertFalse(self.api.calls)

    def test_larger_weekly_limit_cannot_hide_behind_daily_budget(self):
        row=self.complete_snapshot();self.exe.perform(self.profile())
        changed=self.grant|{'version':'weekly-test','max_daily_budget_micros':50000000}
        register(self.exe.store,changed,digest(changed))
        row['UnifiedCampaign']['BiddingStrategy']['Search']['HighestPosition']['WeeklySpendLimit']=700000000
        with self.assertRaises(ContractError):self.exe.prepare(self.request('campaign.budget',{'daily_budget_micros':50000000},rid='budget'))
        with self.assertRaisesRegex(ContractError,'Недельный бюджет'):self.exe.audit_launch(self.api,1,changed)

    def test_inclusive_days_and_total_budget_are_enforced(self):
        self.complete_snapshot()
        changed=self.grant|{'version':'small','max_total_budget_micros':450000000}
        register(self.exe.store,changed,digest(changed))
        req=self.profile()
        req['params']['start_date']=str(datetime.now(ZoneInfo('Europe/Moscow')).date()+timedelta(days=1))
        with self.assertRaisesRegex(ContractError,'резерв бюджетов'):self.exe.prepare(req)
        self.assertFalse(self.api.calls)

    def test_stale_weekly_budget_prevents_write(self):
        row=self.complete_snapshot();plan=self.exe.prepare(self.profile())
        row['UnifiedCampaign']['BiddingStrategy']['Search']['HighestPosition']['WeeklySpendLimit']=600000000
        with self.assertRaisesRegex(ContractError,'STALE_PLAN'):self.exe.apply(plan)
        self.assertFalse(self.api.calls)

    def test_unexpected_changes_or_incomplete_reread_are_partial(self):
        row=self.complete_snapshot();plan=self.exe.prepare(self.profile());expected=copy.deepcopy(plan['expected'])
        mutations=[lambda r:r['UnifiedCampaign']['CounterIds']['Items'].append(12),
                   lambda r:r['UnifiedCampaign'].update(AttributionModel='LC'),
                   lambda r:r['DailyBudget'].update(Amount=99999999),
                   lambda r:r['UnifiedCampaign']['Settings'].update(UNKNOWN_OPTION='YES'),
                   lambda r:r.pop('NegativeKeywords')]
        for mutate in mutations:
            after=copy.deepcopy(expected);mutate(after)
            self.assertFalse(self.exe._matches(plan,after))
        original=self.api.mutate
        def drift(*args):
            oid=original(*args);row['UnifiedCampaign']['CounterIds']['Items'].append(12);return oid
        self.api.mutate=drift
        self.assertEqual(self.exe.apply(plan)['status'],'PARTIAL')
        with self.assertRaises(ContractError):self.exe.prepare(self.profile()|{'request_id':'next'})

    def test_lost_response_reconciles_without_second_write(self):
        from directologist.adapters.direct_api import DirectFailure
        self.complete_snapshot();original=self.api.mutate
        def lost(*args):original(*args);raise DirectFailure()
        self.api.mutate=lost
        self.assertEqual(self.exe.perform(self.profile())['status'],'UNKNOWN')
        self.assertEqual(self.exe.reconcile('one',1)['status'],'VERIFIED')
        self.assertEqual(len(self.api.calls),1)

    def test_end_date_is_inclusive_moscow_day(self):
        today=datetime.now(ZoneInfo('Europe/Moscow')).date()
        end=today+timedelta(days=2);stop=datetime.combine(end+timedelta(days=1),datetime.min.time(),ZoneInfo('Europe/Moscow'))
        policy={'expires_at':stop.isoformat()}
        self.assertEqual(validate_end_date(str(end),policy),end)
        policy['expires_at']=(stop-timedelta(seconds=1)).isoformat()
        with self.assertRaises(ContractError):validate_end_date(str(end),policy)
        policy['expires_at']=stop.astimezone(ZoneInfo('UTC')).isoformat()
        self.assertEqual(validate_end_date(str(end),policy),end)
        validate_dates(str(end),str(end),policy)

    def test_explicit_vat_remaining_reserve_does_not_count_spend_twice(self):
        self.complete_snapshot()
        today=datetime.now(ZoneInfo('Europe/Moscow')).date()
        self.api.spend_rows=lambda ids,start,end:[{'Date':str(today),'CampaignId':1,'Cost':120000000}]
        self.api.spend=lambda *args:(_ for _ in ()).throw(AssertionError('Must use one coherent daily snapshot'))
        changed=self.grant|{'version':'gross','budget_vat_basis_points':2200,
                           'max_daily_budget_micros':50000000,'max_total_budget_micros':500000000,'spend_buffer_micros':73000000}
        register(self.exe.store,changed,digest(changed))
        req=self.profile();req['params'].update(start_date=str(today),end_date=str(today+timedelta(days=6-today.weekday())))
        plan=self.exe.prepare(req)
        self.assertEqual(self.exe.apply(plan)['status'],'VERIFIED')

    def test_gross_reserve_calendar_weeks_and_rounding(self):
        from datetime import date
        from directologist.direct_executor import DirectExecutor
        monday=date(2026,10,5);sunday=date(2026,10,11);wednesday=date(2026,10,7)
        rows=[{'Date':'2026-10-06','CampaignId':1,'Cost':1200000000},
              {'Date':'2026-10-06','CampaignId':2,'Cost':9900000000},
              {'Date':'2026-09-30','CampaignId':1,'Cost':9900000000}]
        remaining=DirectExecutor._remaining_weekly
        tax={'budget_vat_basis_points':2200}
        self.assertEqual(remaining(rows,1,3500000000,wednesday,sunday,wednesday,tax),3070000000)
        self.assertEqual(remaining(rows,1,3500000000,wednesday,sunday+timedelta(days=1),wednesday,tax),7340000000)
        self.assertEqual(remaining(rows,1,3500000000,monday+timedelta(days=7),sunday+timedelta(days=7),wednesday,tax),4270000000)
        self.assertEqual(remaining([],None,1,monday,sunday,monday,tax),2)
        overspent=[{'Date':str(monday),'CampaignId':1,'Cost':5000000000}]
        self.assertEqual(remaining(overspent,1,3500000000,monday,sunday,monday,tax),0)

    def test_vat_rate_is_explicit_validated_and_hashed(self):
        from directologist.direct_policy import validate
        for rate in (True,-1,10001,'2200',None):
            with self.subTest(rate=rate),self.assertRaises(ContractError):validate(self.ctx,self.grant|{'budget_vat_basis_points':rate})
        for rate in (0,2200,10000):validate(self.ctx,self.grant|{'budget_vat_basis_points':rate})
        self.assertNotEqual(digest(self.grant),digest(self.grant|{'budget_vat_basis_points':2200}))
        with self.assertRaises(ContractError):validate(self.ctx,self.grant|{'unknown_field':1})

    def test_pending_spend_snapshot_never_allows_write(self):
        from directologist.adapters.direct_api import DirectFailure
        self.complete_snapshot()
        changed=self.grant|{'version':'gross','budget_vat_basis_points':2200}
        register(self.exe.store,changed,digest(changed))
        self.api.spend_rows=lambda *args:(_ for _ in ()).throw(DirectFailure('PENDING',retry_after=1))
        with self.assertRaises(DirectFailure):self.exe.prepare(self.profile())
        self.assertFalse(self.api.calls)

    def test_deprecated_geo_flag_not_required_but_regions_still_checked(self):
        row=self.complete_snapshot();self.exe.perform(self.profile())
        self.api.objects['adgroups'][2]={'Id':2,'CampaignId':1,'RegionIds':[225]}
        self.api.objects['ads'][3]={'Id':3,'CampaignId':1,'State':'OFF','Status':'ACCEPTED',
                                  'ResponsiveAd':{'Href':'https://example.org/'}}
        self.api.objects['keywords'][4]={'Id':4,'CampaignId':1,'Keyword':'synthetic','Bid':1000000,'State':'ON'}
        self.assertTrue(self.exe.audit_launch(self.api,1,self.grant)['ready'])
        row['UnifiedCampaign']['Settings']=[]
        self.assertTrue(self.exe.audit_launch(self.api,1,self.grant)['ready'])
        self.api.objects['adgroups'][2]['RegionIds']=[999]
        with self.assertRaises(ContractError):self.exe.audit_launch(self.api,1,self.grant)
