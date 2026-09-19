import unittest
from unittest.mock import Mock
from analysis_fixtures import setup, project
from directologist.contracts import ContractError
from directologist.wordstat_queue import WordstatQueue, WordstatReader
from directologist.adapters.reports import ReadPending
from directologist.secrets import Credential

REQUEST = {'phrase': 'Тестовый запрос', 'regions': [225], 'devices': ['all'], 'num_phrases': 10}
RESPONSE = {'totalCount': '100', 'results': [{'phrase': 'тестовый запрос', 'count': '90'}], 'associations': []}

class WordstatTests(unittest.TestCase):
    def setUp(self):
        setup(self); self.now = 10000.0
        self.queue = WordstatQueue(self.ctx, clock=lambda: self.now)
        self.addCleanup(self.queue.__exit__)
        self.fetch = Mock(return_value=RESPONSE)

    def enqueue(self, job='one', requests=None, limit=10, ttl=100):
        return self.queue.enqueue(job, requests or [REQUEST], request_limit=limit, ttl_seconds=ttl)

    def step(self, job='one', hour=100):
        return self.queue.step(job, self.fetch, requests_per_second=10, requests_per_hour=hour)

    def test_normalized_dedup_idempotency_and_no_completed_replay(self):
        state = self.enqueue(requests=[REQUEST, REQUEST | {'phrase': '  ТЕСТОВЫЙ   ЗАПРОС  '}])
        self.assertEqual(len(state['items']), 1)
        self.step(); self.step(); self.enqueue(requests=[REQUEST])
        self.assertEqual(self.fetch.call_count, 1)
        with self.assertRaises(ContractError): self.enqueue(limit=11)

    def test_shared_fresh_cache_and_expiry(self):
        self.enqueue(); self.step()
        self.enqueue('two'); self.step('two')
        self.assertEqual(self.fetch.call_count, 1)
        self.now += 101
        self.assertFalse(self.queue.status('one')['items'][0]['cache_fresh'])
        self.enqueue('three'); self.step('three')
        self.assertEqual(self.fetch.call_count, 2)

    def test_cache_respects_new_shorter_ttl_and_request_size(self):
        self.enqueue(); self.step(); self.now += 10
        self.enqueue('short', ttl=5); self.step('short')
        self.enqueue('large', requests=[REQUEST | {'num_phrases': 20}]); self.step('large')
        self.assertEqual(self.fetch.call_count, 3)

    def test_cache_clock_rollback_and_other_project_isolation(self):
        self.enqueue(); self.step(); self.now -= 1
        self.enqueue('earlier'); self.step('earlier')
        self.assertEqual(self.fetch.call_count, 2)
        with WordstatQueue(project(self.root, 'other'), clock=lambda: self.now) as other:
            other.enqueue('one', [REQUEST], request_limit=10, ttl_seconds=100)
            self.assertFalse(other.status('one')['items'][0]['cache_fresh'])
            other.step('one', self.fetch, requests_per_second=10, requests_per_hour=100)
        self.assertEqual(self.fetch.call_count, 3)

    def test_quota_pause_survives_reopen_without_premature_attempt(self):
        self.enqueue(); self.fetch.side_effect = ReadPending('QUOTA', 30)
        result = self.step()
        self.assertEqual(result['items'][0]['state'], 'PAUSED_QUOTA')
        with WordstatQueue(self.ctx, clock=lambda: self.now) as reopened:
            reopened.step('one', self.fetch, requests_per_second=10, requests_per_hour=100)
            self.assertEqual(self.fetch.call_count, 1)
            self.now += 31; self.fetch.side_effect = None
            result = reopened.step('one', self.fetch, requests_per_second=10, requests_per_hour=100)
        self.assertEqual(result['items'][0]['state'], 'DONE')
        self.assertEqual(result['attempts'], 2)

    def test_crash_becomes_unknown_and_requires_explicit_reconciliation(self):
        self.enqueue(); self.fetch.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt): self.step()
        self.fetch.side_effect = None
        with WordstatQueue(self.ctx, clock=lambda: self.now) as reopened:
            result = reopened.step('one', self.fetch, requests_per_second=10, requests_per_hour=100)
            self.assertEqual(result['blocked_reason'], 'UNKNOWN')
            self.assertEqual(self.fetch.call_count, 1)
            reopened.retry_unknown('one', result['items'][0]['key'])
            result = reopened.step('one', self.fetch, requests_per_second=10, requests_per_hour=100)
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['items'][0]['state'], 'DONE')

    def test_exception_does_not_leak_and_request_cap_is_durable(self):
        self.enqueue(limit=1); self.fetch.side_effect = RuntimeError('SECRET_SENTINEL')
        result = self.step()
        self.assertNotIn('SECRET_SENTINEL', str(result))
        self.queue.retry_unknown('one', result['items'][0]['key'])
        self.assertEqual(self.step()['blocked_reason'], 'REQUEST_LIMIT')
        self.assertEqual(self.fetch.call_count, 1)
        for path in self.ctx.directory.rglob('*'):
            if path.is_file(): self.assertNotIn(b'SECRET_SENTINEL', path.read_bytes())

    def test_hourly_limit_counts_across_jobs(self):
        self.enqueue(); self.step(hour=1)
        self.enqueue('two', requests=[REQUEST | {'phrase': 'второй запрос'}])
        result = self.step('two', hour=1)
        self.assertEqual(result['items'][0]['state'], 'PAUSED_QUOTA')
        self.assertEqual(self.fetch.call_count, 1)
        self.now += 3601; self.step('two', hour=1)
        self.assertEqual(self.fetch.call_count, 2)

    def test_worker_lock_prevents_concurrent_callback(self):
        self.enqueue()
        with self.queue._lock(), WordstatQueue(self.ctx) as other:
            with self.assertRaises(ContractError): other.step('one', self.fetch, requests_per_second=10, requests_per_hour=100)
        self.fetch.assert_not_called()

    def test_adapter_shapes_request_and_queue_status_excludes_phrases(self):
        http = Mock(); http.request.return_value = RESPONSE
        reader = WordstatReader(Credential('synthetic-wordstat-secret'), {'folder_id': 'fixture', 'auth_scheme': 'Api-Key'}, http)
        self.assertEqual(reader(REQUEST), RESPONSE)
        body = http.request.call_args.kwargs['body']
        self.assertEqual(body['regions'], ['225']); self.assertEqual(body['devices'], ['DEVICE_ALL'])
        state = self.enqueue()
        self.assertNotIn('Тестовый', str(state)); self.assertFalse(state['live_runner_enabled'])
