import copy
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch
from analysis_fixtures import setup, direct, crm_raw, START, END
from directologist.adapters import ProbeError
from directologist.adapters.reports import (ReadHTTP, ReadPending, direct_payload, parse_direct,
    normalize_metrika, normalize_crm, collect, collect_configured)
from directologist.analytics import summarize, save_bundle, load_bundle, compare
from directologist.contracts import ContractError, canonical
from directologist.secrets import Credential
from directologist.storage import Store

TSV = 'Date\tCampaignId\tImpressions\tClicks\tCost\tConversions_9_AUTO\n2026-09-01\t1\t100\t10\t1234567\t2\n'

class ReadAdapterTests(unittest.TestCase):
    def setUp(self):
        setup(self)
        self.secret = Credential('synthetic-test-secret-123456789')

    def test_direct_request_identity_goal_units_and_campaign_filter(self):
        one = direct_payload(['1'], START, END, '9', 'AUTO', 'excluded')
        self.assertEqual(one, direct_payload(['1'], START, END, '9', 'AUTO', 'excluded'))
        params = one['params']
        self.assertEqual(params['Goals'], ['9'])
        self.assertEqual(params['IncludeVAT'], 'NO')
        self.assertEqual(params['SelectionCriteria']['Filter'][0]['Values'], ['1'])
        self.assertNotEqual(params['ReportName'], direct_payload(['1'], START, END, '9', 'LC', 'excluded')['params']['ReportName'])

    def test_direct_micros_missing_values_and_wrong_headers(self):
        self.assertEqual(parse_direct(TSV, '9', 'AUTO')[0]['cost'], '1.234567')
        self.assertIsNone(parse_direct(TSV.replace('1234567', '--'), '9', 'AUTO')[0]['cost'])
        with self.assertRaises(ContractError): parse_direct(TSV, '8', 'AUTO')
        with self.assertRaises(ContractError): parse_direct(TSV.rstrip() + '\textra\n', '9', 'AUTO')

    def test_collect_uses_verified_currency_and_bound_scope(self):
        http = Mock()
        http.transport.request.return_value = {'result': {'Clients': [{'Currency': 'RUB'}]}}
        http.request.return_value = TSV
        result = collect(self.ctx, 'direct', self.secret, {}, start=START, end=END, goal='9', attribution='AUTO', http=http)
        self.assertEqual(result['units']['currency'], 'RUB')
        self.assertEqual(result['scope'], {'campaign_ids': ['1']})
        http.transport.request.return_value = {'result': {'Clients': []}}
        with self.assertRaises(ContractError):
            collect(self.ctx, 'direct', self.secret, {}, start=START, end=END, goal='9', attribution='AUTO', http=http)

    def test_metrika_totals_sampling_and_context_echo(self):
        raw = {'query': {'metrics': ['ym:s:visits', 'ym:s:goal9reaches'], 'date1': START, 'date2': END, 'ids': [2]},
               'totals': [10, 0], 'sampled': True}
        rows, sampled = normalize_metrika(raw, '9')
        self.assertTrue(sampled); self.assertEqual(rows[0]['goal_reaches'], '0')
        http = Mock(); http.request.return_value = raw
        result = collect(self.ctx, 'metrika', self.secret, {}, start=START, end=END, goal='9', attribution='last', http=http)
        self.assertEqual(result['scope']['traffic'], 'all-site-traffic')
        for key, value in [('date1', '2026-08-01'), ('attribution', 'first'), ('ids', [3])]:
            http.request.return_value = copy.deepcopy(raw)
            http.request.return_value['query'][key] = value
            with self.subTest(key=key), self.assertRaises(ContractError):
                collect(self.ctx, 'metrika', self.secret, {}, start=START, end=END, goal='9', attribution='last', http=http)
        with self.assertRaises(ContractError): normalize_metrika(raw, '8')

    def test_retired_metrika_models_not_sent(self):
        http = Mock()
        for model in ("first", "lastsign"):
            with self.assertRaises(ContractError):
                collect(self.ctx, "metrika", self.secret, {}, start=START, end=END, goal="9", attribution=model, http=http)
        http.request.assert_not_called()

    def test_crm_minimization_currencies_unknown_and_quality(self):
        http = Mock(); http.request.return_value = crm_raw()
        data = collect(self.ctx, 'crm', self.secret, {}, start=START, end=END, http=http)
        result = summarize(data)
        self.assertIsNone(result['totals']['qualified_leads'])
        self.assertEqual(result['totals']['unmatched_leads'], '4')
        self.assertEqual(result['scope']['missing_configuration_count'], 1)
        self.assertEqual(result['revenue_by_currency'], {'RUB': '123.45', 'USD': '2.10'})
        self.assertEqual(result['margin_by_currency'], {'RUB': '-10.25'})
        self.assertTrue(compare([direct(self.ctx), data])['crm_available'])
        saved = save_bundle(self.ctx, data)
        self.assertNotIn('PRIVATE_SENTINEL', canonical(load_bundle(self.ctx, saved['evidence_id'])))
        for path in self.ctx.directory.rglob('*'):
            if path.is_file(): self.assertNotIn(b'PRIVATE_SENTINEL', path.read_bytes())

    def test_crm_wrong_cohort_period_or_mutability_rejected(self):
        for key, value in [('readOnly', False), ('cohort', 'other'), ('dateFrom', END)]:
            data = crm_raw(); data['meta'][key] = value
            with self.subTest(key=key), self.assertRaises(ContractError): normalize_crm(data, START, END)

    def response(self, http, status=200, body=b'{}', headers=None):
        opened = patch.object(http.transport.opener, 'open')
        mock = opened.start(); self.addCleanup(opened.stop)
        result = mock.return_value.__enter__.return_value
        result.status, result.headers = status, headers or {}
        result.read.return_value = body
        return mock

    def test_http_pending_and_fixed_report_headers(self):
        http = ReadHTTP()
        opened = self.response(http, 202, headers={'retryIn': '17'})
        with self.assertRaises(ReadPending) as err:
            http.request('direct', self.secret, body={'params': {}}, client_login='fixture')
        self.assertEqual(err.exception.retry_after, 17)
        req = opened.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.direct.yandex.com/json/v501/reports')
        self.assertEqual(req.method, 'POST')
        self.assertEqual(req.get_header('Skipcolumnheader'), 'false')
        self.assertIsNone(req.get_header('Returnmoneyinmicros'))  # default micros

    def test_http_error_and_secret_reflection_are_sanitized(self):
        http = ReadHTTP()
        opened = self.response(http, body=self.secret.value.encode())
        with self.assertRaises(ProbeError) as err: http.request('metrika', self.secret)
        self.assertNotIn(self.secret.value, str(err.exception))
        opened.side_effect = urllib.error.HTTPError('https://example.invalid', 429, self.secret.value,
            {'Retry-After': '30'}, io.BytesIO(self.secret.value.encode()))
        with self.assertRaises(ReadPending) as err: http.request('metrika', self.secret)
        self.assertEqual(err.exception.retry_after, 30)
        self.assertNotIn(self.secret.value, str(err.exception))

    def test_http_rejects_arbitrary_source_and_write_on_get_adapters(self):
        http = ReadHTTP('https://bridge.example.org')
        with patch.object(http.transport.opener, 'open') as opened:
            for source, args in [('other', {}), ('metrika', {'body': {}}), ('crm', {'body': {}, 'bridge_id': 'fixture'}),
                                 ('crm', {'bridge_id': '../escape'})]:
                with self.subTest(source=source), self.assertRaises(ContractError): http.request(source, self.secret, **args)
            opened.assert_not_called()

    def connection(self, state='CHECKED'):
        binding = self.ctx.profile['bindings']['direct']
        record = dict(state=state, connection_id=binding['connection_id'], resources=binding['resources'], config={})
        with Store(self.ctx, create=True) as store, store.transaction():
            store.connection.execute('CREATE TABLE connections(provider TEXT PRIMARY KEY, record TEXT)')
            store.connection.execute('INSERT INTO connections VALUES (?,?)', ('direct', canonical(record)))

    def test_pending_delay_persisted_before_another_keychain_or_network_read(self):
        self.connection()
        options = dict(source='direct', start=START, end=END, goal='9', attribution='AUTO')
        with patch('directologist.secrets.SecretStore') as secrets, patch('directologist.adapters.reports.collect', side_effect=ReadPending('PENDING', 300)) as fetch:
            secrets.return_value.get.return_value = self.secret
            self.assertEqual(collect_configured(self.ctx, **options)['state'], 'PENDING')
            self.assertEqual(collect_configured(self.ctx, **options)['state'], 'PENDING')
            self.assertEqual(fetch.call_count, 1)
            secrets.assert_called_once_with('fixture')
            secrets.return_value.get.assert_called_once_with('direct', 'synthetic')

    def test_unchecked_connection_rejected_before_secret_access(self):
        self.connection('QUOTA')
        with patch('directologist.secrets.SecretStore') as secrets:
            with self.assertRaises(ContractError): collect_configured(self.ctx, source='direct', start=START, end=END)
            secrets.assert_not_called()
