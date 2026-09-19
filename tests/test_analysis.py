import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from analysis_fixtures import setup, direct, row, project, rehash, START, END
from directologist import cli
from directologist.analytics import number, bundle, validate_bundle, summarize, compare, merge, save_bundle, load_bundle
from directologist.contracts import ContractError, load_project

class AnalysisTests(unittest.TestCase):
    def setUp(self): setup(self)

    def test_unknown_is_not_zero_and_invalid_numbers_fail(self):
        for value in (None, '', '--'): self.assertIsNone(number(value))
        self.assertEqual(number('0'), Decimal(0))
        for value in (True, 0.1, 'NaN', 'Infinity', '-1', '1e2'):
            with self.subTest(value=value), self.assertRaises(ContractError): number(value)

    def test_exact_money_and_weighted_ratios(self):
        data = direct(self.ctx, [row(cost='0.1', clicks='1'), row(END, cost='0.2', clicks='9', impressions='90')])
        result = summarize(data)
        self.assertEqual(result['totals']['cost'], '0.3')
        self.assertEqual(result['ratios']['cpc'], '0.030000')
        self.assertEqual(result['ratios']['ctr_percent'], '10.000000')
        self.assertNotIn('cpl', result['ratios'])

    def test_missing_cell_propagates_and_zero_denominator_null(self):
        result = summarize(direct(self.ctx, [row(cost='--', clicks='0', conversions='0')]))
        self.assertIsNone(result['totals']['cost'])
        self.assertEqual(result['unknown_cells'], 1)
        self.assertIsNone(result['ratios']['cpc'])
        self.assertIsNone(result['ratios']['cost_per_ad_conversion'])
        self.assertEqual(summarize(direct(self.ctx, []))['totals']['clicks'], '0')

    def test_partial_suppresses_totals_and_sampling_is_visible(self):
        result = summarize(direct(self.ctx, completeness='PARTIAL', sampled=True))
        self.assertTrue(result['sampled'])
        self.assertTrue(all(v is None for v in result['totals'].values()))

    def test_unverified_source_timezone_is_not_project_timezone(self):
        result = summarize(direct(self.ctx))
        self.assertEqual(result['timezone'], 'Europe/Moscow')
        self.assertIsNone(result['source_timezone'])
        data = direct(self.ctx)
        data['units']['currency'] = 'NONE'
        with self.assertRaises(ContractError): validate_bundle(rehash(data))
        data = direct(self.ctx)
        data['context_hash'] = 'PRIVATE_SENTINEL'
        with self.assertRaises(ContractError): validate_bundle(rehash(data))

    def test_incompatible_contexts_cannot_merge(self):
        first = direct(self.ctx)
        for key, value in [('goal_id', '8'), ('attribution', 'LC'), ('project_id', 'other'),
                           ('context_hash', 'a' * 64), ('period', {'from': START, 'to': '2026-09-03'}),
                           ('units', first['units'] | {'currency': 'USD'}),
                           ('units', first['units'] | {'vat': 'included'})]:
            other = copy.deepcopy(first)
            other[key] = value
            rehash(other)
            with self.subTest(key=key, value=value), self.assertRaises(ContractError): merge([first, other])

    def test_merge_disjoint_rows_and_reject_overlap(self):
        first, second = direct(self.ctx), direct(self.ctx, [row(END)])
        self.assertEqual(summarize(merge([first, second]))['totals']['cost'], '0.2')
        with self.assertRaises(ContractError): merge([first, first])

    def test_context_freshness_hash_and_schema_guards(self):
        data = direct(self.ctx)
        with self.assertRaises(ContractError): validate_bundle(data, project(self.root, 'other'))
        with self.assertRaises(ContractError): validate_bundle(data, now=datetime.now(timezone.utc) + timedelta(days=2))
        data['rows'][0]['cost'] = '10'
        with self.assertRaises(ContractError): validate_bundle(data)
        rehash(data)
        data['rows'][0]['email'] = 'PRIVATE_SENTINEL'
        with self.assertRaises(ContractError): validate_bundle(rehash(data))

    def test_future_time_and_out_of_scope_rows_rejected(self):
        data = direct(self.ctx)
        data['fetched_at'] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        data['expires_at'] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        with self.assertRaises(ContractError): validate_bundle(rehash(data))
        for change in ({'date': '2026-08-31'}, {'campaign_id': '2'}):
            with self.assertRaises(ContractError): direct(self.ctx, [row() | change])

    def test_artifact_roundtrip_cli_export_and_tamper(self):
        data = direct(self.ctx)
        saved = save_bundle(self.ctx, data)
        self.assertEqual(load_bundle(self.ctx, saved['evidence_id']), data)
        self.assertEqual(save_bundle(self.ctx, data), saved)
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(['--workspace', str(self.root), '--project', 'fixture', 'analyze', '--evidence', saved['evidence_id']])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output.getvalue())['result']['autonomous_writes'])
        from pathlib import Path
        Path(saved['path']).write_text('{}')
        with self.assertRaises(ContractError): load_bundle(self.ctx, saved['evidence_id'])

    def test_old_binding_and_symlink_artifact_rejected(self):
        data = direct(self.ctx)
        saved = save_bundle(self.ctx, data)
        path = self.ctx.directory / 'profile.json'
        profile = self.ctx.profile | {'binding_version': 2}
        path.write_text(json.dumps(profile))
        with self.assertRaises(ContractError): load_bundle(load_project(self.root, 'fixture'), saved['evidence_id'])
        from pathlib import Path
        target = Path(saved['path']); target.unlink(); target.symlink_to(path)
        with self.assertRaises(ContractError): load_bundle(self.ctx, saved['evidence_id'])

    def test_cross_source_comparison_never_claims_crm_attribution(self):
        site = bundle(self.ctx, 'metrika', [{'visits': '50', 'goal_reaches': '7'}], start=START, end=END,
            goal_id='9', attribution='last', scope={'counter_id': '2', 'traffic': 'all-site-traffic'},
            units={'currency': 'NONE', 'money': 'major', 'vat': 'na', 'discount': 'na'})
        result = compare([direct(self.ctx), site])
        self.assertFalse(result['crm_available'])
        self.assertEqual(result['cross_source_attribution'], 'NOT_ESTABLISHED')
        self.assertIsNone(result['crm_cpl']); self.assertIsNone(result['cac'])
        self.assertEqual(result['sources'][1]['scope']['traffic'], 'all-site-traffic')
        site['period']['to'] = '2026-09-03'
        with self.assertRaises(ContractError): compare([direct(self.ctx), rehash(site)])
