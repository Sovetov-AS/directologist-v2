import http.client
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import yandex_data as m


CATALOG = {'endpoint': 'https://api-metrika.yandex.net', 'operations': [
    {'id': 'report', 'method': 'GET', 'path': '/stat/v1/data', 'effect': 'read'},
    {'id': 'counter', 'method': 'GET', 'path': '/management/v1/counter/{counterId}', 'effect': 'read'},
    {'id': 'generate', 'method': 'GET', 'path': '/management/v1/counter/{counterId}/measurement/generate', 'effect': 'write'},
    {'id': 'upload', 'method': 'POST', 'path': '/upload', 'effect': 'write'},
    {'id': 'chats', 'method': 'GET', 'path': '/uploads', 'effect': 'read', 'required_query': {'type': 'CHATS'}},
    {'id': 'measurement', 'method': 'GET', 'path': '/collect', 'effect': 'write',
     'endpoint': 'https://mc.yandex.ru', 'auth': 'measurement_token', 'response_format': 'text'},
    {'id': 'download', 'method': 'GET', 'path': '/download', 'effect': 'read', 'response_format': 'binary'},
]}


class Response:
    def __init__(self, body=b'{"data":[],"sampled":true,"sample_share":0.5,"total_rows":105}', status=200):
        self.body, self.status = body, status
        self.headers = {'X-Request-Id': 'fixture', 'Retry-After': '60'}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return self.body


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response or Response(), error, []
    def open(self, request, timeout):
        self.requests.append(request)
        if self.error: raise self.error
        return self.response


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'writes_enabled': False}
        self.opener = Opener()
        self.token_reads = 0
    def token(self):
        self.token_reads += 1
        return 'TEST_OAUTH_SECRET'
    def client(self, api='metrika', catalog=None):
        return m.DataClient(api, self.config, catalog or CATALOG, token_getter=self.token,
                            measurement_token_getter=lambda: 'MEASURE_SECRET', opener=self.opener,
                            journal=Path(self.temp.name) / 'journal.jsonl')
    def test_report_query_not_capped_or_rewritten(self):
        query = {'date1': '2020-01-01', 'date2': '2026-11-01', 'limit': 100000,
                 'offset': 50001, 'dimensions': 'new:dimension', 'filters': "a=='слово !'"}
        plan = self.client().prepare('report', query=query)
        self.assertEqual(parse_qs(urlsplit(plan['url']).query), {k: [str(v)] for k, v in query.items()})
        self.assertEqual(self.token_reads, 0)
    def test_get_that_generates_token_is_not_read(self):
        with self.assertRaises(m.ClientError):
            self.client().call('generate', path_params={'counterId': 1}, execute_write=True, operation_id='x')
        self.assertEqual(self.token_reads, 0)
        self.assertFalse(self.opener.requests)
    def test_counter_path_cannot_inject_another_endpoint(self):
        for value in ('../generate', '1?foo', '//evil', '..'):
            with self.assertRaises(m.ClientError):
                self.client().prepare('counter', path_params={'counterId': value})
    def test_unknown_operation_never_gets_credentials(self):
        with self.assertRaises(m.ClientError): self.client().call('nonexistent')
        self.assertEqual(self.token_reads, 0)
    def test_sampling_pagination_and_data_are_preserved(self):
        result = self.client().call('report')
        self.assertTrue(result['sampled'])
        self.assertEqual(result['sample_share'], 0.5)
        self.assertEqual(result['total_rows'], 105)
        self.assertEqual(result['status'], 'OK')
        self.assertIn('data', result)
    def test_wordstat_posts_are_reads_and_paid_execution_is_explicit(self):
        catalog = {'endpoint': 'https://searchapi.api.cloud.yandex.net', 'operations': [
            {'id': 'top', 'method': 'POST', 'path': '/v2/wordstat/topRequests', 'effect': 'read', 'paid': True}]}
        c = self.client('wordstat', catalog)
        with self.assertRaises(m.ClientError): c.call('top', body={'phrase': 'слово'})
        self.assertEqual(self.token_reads, 0)
        body = {'phrase': '!внедрение "битрикс24"', 'numPhrases': 2000, 'newField': 'pass through'}
        self.assertEqual(c.call('top', body=body, execute_paid=True, operation_id='paid')['status'], 'OK')
        self.assertEqual(json.loads(self.opener.requests[0].data), body)
        self.assertEqual(self.opener.requests[0].get_header('Authorization'), 'Api-Key TEST_OAUTH_SECRET')
        with self.assertRaises(m.ClientError): c.call('top', execute_paid=True, operation_id='paid')
        self.assertEqual(len(self.opener.requests), 1)
    def test_quota_response_keeps_retry_after_without_retry(self):
        self.opener = Opener(Response(b'{"code":8,"message":"quota TEST_OAUTH_SECRET"}', 429))
        result = self.client().call('report')
        self.assertEqual(result['status'], 'API_ERROR')
        self.assertEqual(result['error_code'], 8)
        self.assertEqual(result['headers']['Retry-After'], '60')
        self.assertEqual(len(self.opener.requests), 1)
        self.assertNotIn('TEST_OAUTH_SECRET', json.dumps(result))
    def test_network_truncation_does_not_retry_or_leak(self):
        self.opener = Opener(error=http.client.IncompleteRead(b'TEST_OAUTH_SECRET'))
        result = self.client().call('report')
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(len(self.opener.requests), 1)
        self.assertNotIn('TEST_OAUTH_SECRET', json.dumps(result))
    def test_write_unknown_cannot_replay_same_id(self):
        self.config['writes_enabled'] = True
        self.opener = Opener(Response(b'broken json'))
        c = self.client()
        self.assertEqual(c.call('upload', body={}, execute_write=True, operation_id='w')['status'], 'UNKNOWN')
        with self.assertRaises(m.ClientError): c.call('upload', body={}, execute_write=True, operation_id='w')
        self.assertEqual(len(self.opener.requests), 1)
    def test_required_query_preserved_and_conflict_rejected(self):
        c = self.client()
        self.assertIn('type=CHATS', c.prepare('chats')['url'])
        with self.assertRaises(m.ClientError): c.prepare('chats', query={'type': 'CALLS'})
    def test_measurement_uses_separate_token_no_oauth(self):
        self.config['writes_enabled'] = True
        self.opener = Opener(Response(b''))
        result = self.client().call('measurement', query={'tid': 1}, execute_write=True, operation_id='mp')
        self.assertEqual(result['status'], 'OK')
        self.assertIn('ms=MEASURE_SECRET', self.opener.requests[0].full_url)
        self.assertIsNone(self.opener.requests[0].get_header('Authorization'))
        self.assertEqual(self.token_reads, 0)
        self.assertNotIn('MEASURE_SECRET', (Path(self.temp.name) / 'journal.jsonl').read_text())
    def test_binary_download_not_corrupted(self):
        data = b'\x1f\x8b\x00\xff'
        self.opener = Opener(Response(data))
        result = self.client().call('download')
        self.assertEqual(m.base64.b64decode(result['data_base64']), data)
    def test_multipart_preserves_source_bytes(self):
        path = Path(self.temp.name) / 'data.csv'
        data = 'ClientId,Target\n123,Тест\n'.encode()
        path.write_bytes(data)
        raw, content_type = m.multipart(path)
        self.assertIn(data, raw)
        self.assertIn('multipart/form-data; boundary=', content_type)
        plan = self.client().prepare('upload', raw_body=raw, content_type=content_type)
        self.assertEqual(plan['payload'], raw)
        self.assertEqual(self.token_reads, 0)
    def test_private_result_permissions(self):
        path = Path(self.temp.name) / 'public.json'
        path.write_text('{}'); path.chmod(0o644)
        m.save_private(path, {'data': 'private'})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
    def test_explicit_unsuccessful_response_is_not_ok(self):
        self.opener = Opener(Response(b'{"success":false}'))
        self.assertEqual(self.client().call('report')['status'], 'API_ERROR')
    def test_delete_measurement_token_not_in_preview_or_journal(self):
        catalog = {'endpoint': 'https://api-metrika.yandex.net', 'operations': [
            {'id': 'delete_token', 'method': 'POST', 'path': '/measurement/delete', 'effect': 'write',
             'secret_query': {'token': 'measurement_credential'}}]}
        c = self.client(catalog=catalog)
        with self.assertRaises(m.ClientError): c.prepare('delete_token', query={'token': 'MEASURE_SECRET'})
        self.assertNotIn('MEASURE_SECRET', str(c.prepare('delete_token')))
        self.config['writes_enabled'] = True
        self.opener.response = Response(b'{"message":"MEASURE_SECRET"}')
        result = c.call('delete_token', execute_write=True, operation_id='delete-token')
        self.assertIn('token=MEASURE_SECRET', self.opener.requests[0].full_url)
        self.assertIsNotNone(self.opener.requests[0].get_header('Authorization'))
        self.assertNotIn('MEASURE_SECRET', json.dumps(result))
        self.assertNotIn('MEASURE_SECRET', (Path(self.temp.name) / 'journal.jsonl').read_text())
    def test_documented_csv_variant(self):
        catalog = {'endpoint': 'https://api-metrika.yandex.net', 'operations': [
            {'id': 'report', 'method': 'GET', 'path': '/stat/v1/data', 'effect': 'read',
             'response_variants': {'csv': {'path': '/stat/v1/data.csv', 'response_format': 'text'}}}]}
        self.opener = Opener(Response('Визиты\n1\n'.encode()))
        c = self.client(catalog=catalog)
        result = c.call('report', variant='csv')
        self.assertEqual(result['status'], 'OK')
        self.assertTrue(self.opener.requests[0].full_url.endswith('.csv'))
        self.assertEqual(result['data'], 'Визиты\n1\n')
    def test_inconsistent_route_not_misrepresented_as_available(self):
        catalog = {'endpoint': 'https://api-metrika.yandex.net', 'operations': [
            {'id': 'bad', 'method': 'GET', 'path': '/wrong', 'effect': 'read',
             'documentation_status': 'inconsistent', 'use_instead': 'good'}]}
        with self.assertRaisesRegex(m.ClientError, 'good'): self.client(catalog=catalog).call('bad')
        self.assertEqual(self.token_reads, 0)


if __name__ == '__main__': unittest.main()
