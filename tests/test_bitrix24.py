import http.client
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import bitrix24 as m

CAT = {'operations': [{'id': name, 'effect': effect} for name, effect in (
    ('crm.lead.list', 'read'), ('crm.deal.get', 'read'), ('crm.deal.update', 'write'),
    ('crm.item.list', 'read'), ('batch', 'container'))]}

class Response:
    def __init__(self, data=None, status=200):
        self.raw = json.dumps(data if data is not None else {'result': []}).encode()
        self.status, self.headers = status, {'Retry-After': '60'}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return self.raw

class Opener:
    def __init__(self, responses=None, error=None):
        self.responses, self.error, self.requests = list(responses or [Response()]), error, []
    def open(self, request, timeout):
        self.requests.append(request)
        if self.error: raise self.error
        return self.responses.pop(0)

class BitrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg = {'portal_origin': 'https://example.bitrix24.ru', 'auth': 'incoming-webhook', 'writes_enabled': False}
        self.secret_reads = 0; self.opener = Opener()
    def token(self):
        self.secret_reads += 1
        return 'https://example.bitrix24.ru/rest/1/FAKE_WEBHOOK_SECRET/'
    def client(self):
        return m.BitrixClient(self.cfg, CAT, token_getter=self.token, opener=self.opener, journal=Path(self.tmp.name)/'journal')
    def test_no_legacy_date_field_or_response_caps(self):
        fields = {'filter': {'>=DATE_CREATE': '2020-01-01'}, 'select': ['*','UF_*'], 'custom': 'pass'}
        self.assertEqual(self.client().prepare('crm.lead.list', fields)['params'], fields)
        self.assertEqual(self.secret_reads, 0)
        self.opener = Opener([Response({'result': [{'TEXT': 'x'*1100000}]})])
        self.assertEqual(len(self.client().call('crm.lead.list')['data'][0]['TEXT']), 1100000)
    def test_unknown_or_write_blocked_before_credentials(self):
        for method in ('crm.deal.update','crm.deal.notClassified'):
            with self.assertRaises(m.ClientError): self.client().call(method)
        self.assertEqual(self.secret_reads, 0); self.assertFalse(self.opener.requests)
    def test_batch_cannot_hide_write(self):
        with self.assertRaises(m.ClientError):
            self.client().call('batch', {'cmd': {'x':'crm.deal.update?id=1'}})
        self.assertEqual(self.secret_reads, 0)
        with self.assertRaises(m.ClientError): self.client().prepare('batch', {'cmd': {'x':'batch?cmd=a'}})
    def test_more_than_twenty_pages_are_fully_read(self):
        responses = [Response({'result':[{'ID':i}], **({'next':i+1} if i<24 else {})}) for i in range(25)]
        self.opener = Opener(responses)
        result = self.client().all_pages('crm.lead.list')
        self.assertTrue(result['complete']); self.assertEqual(len(result['items']),25)
        self.assertEqual(len(self.opener.requests),25)
    def test_page_error_preserves_partial_results(self):
        self.opener = Opener([Response({'result':[{'ID':1}],'next':50}),Response({'error':'QUERY_LIMIT_EXCEEDED'},429)])
        result = self.client().all_pages('crm.lead.list')
        self.assertFalse(result['complete']); self.assertEqual(result['items'],[{'ID':1}])
        self.assertEqual(result['pages'][-1]['headers']['Retry-After'],'60')
    def test_nested_pagination_supported(self):
        self.opener = Opener([Response({'result':{'items':[{'id':1}],'next':50}}),Response({'result':{'items':[{'id':2}]}})])
        result = self.client().all_pages('crm.item.list',items_key='items')
        self.assertTrue(result['complete']); self.assertEqual(len(result['items']),2)
    def test_repeated_cursor_does_not_loop(self):
        self.opener = Opener([Response({'result':[{'ID':1}],'next':0})])
        result = self.client().all_pages('crm.lead.list')
        self.assertFalse(result['complete']); self.assertEqual(result['reason'],'repeated_cursor')
    def test_start_minus_one_not_claimed_complete(self):
        result = self.client().all_pages('crm.lead.list', {'start':-1})
        self.assertFalse(result['complete'])
    def test_batch_many_splits_provider_packets_not_task(self):
        commands = {'n'+str(i):{'method':'crm.deal.get','params':{'id':i}} for i in range(63)}
        self.opener = Opener([Response({'result':{'result':{k:{} for k in list(commands)[:50]}}}),Response({'result':{'result':{k:{} for k in list(commands)[50:]}}})])
        result = self.client().batch_many(commands)
        self.assertTrue(result['commands_complete']); self.assertEqual(len(self.opener.requests),2)
        self.assertEqual(len(json.loads(self.opener.requests[0].data)['cmd']),50)
    def test_partial_batch_errors_and_missing_commands(self):
        self.opener = Opener([Response({'result':{'result':{'a':{}},'result_error':{'b':{'error':'ACCESS_DENIED'}}}})])
        result = self.client().call('batch', {'cmd':{'a':'crm.deal.get?id=1','b':'crm.deal.get?id=2','c':'crm.deal.get?id=3'}})
        self.assertEqual(result['status'],'PARTIAL'); self.assertEqual(result['missing_commands'],['c'])
    def test_error_zero_is_error_and_secret_redacted(self):
        self.opener = Opener([Response({'error':0,'error_description':'FAKE_WEBHOOK_SECRET'})])
        result = self.client().call('crm.lead.list')
        self.assertEqual(result['status'],'API_ERROR')
        self.assertNotIn('FAKE_WEBHOOK_SECRET',json.dumps(result))
        self.assertNotIn('FAKE_WEBHOOK_SECRET',(Path(self.tmp.name)/'journal').read_text())
    def test_network_unknown_no_retry(self):
        self.opener = Opener(error=http.client.IncompleteRead(b'FAKE_WEBHOOK_SECRET'))
        result = self.client().call('crm.lead.list')
        self.assertEqual(result['status'],'UNKNOWN'); self.assertEqual(len(self.opener.requests),1)
        self.assertNotIn('FAKE_WEBHOOK_SECRET',json.dumps(result))
    def test_secret_origin_mismatch_never_sends(self):
        self.cfg['portal_origin']='https://different.bitrix24.ru'
        with self.assertRaises(m.ClientError):self.client().call('crm.lead.list')
        self.assertFalse(self.opener.requests)
    def test_write_id_not_replayed(self):
        self.cfg['writes_enabled']=True
        self.opener=Opener(error=TimeoutError())
        c=self.client();r=c.call('crm.deal.update',{'id':1},execute_write=True,operation_id='write-test')
        self.assertEqual(r['status'],'UNKNOWN')
        with self.assertRaises(m.ClientError):c.call('crm.deal.update',{'id':1},execute_write=True,operation_id='write-test')
        self.assertEqual(len(self.opener.requests),1)
    def test_batch_unexpected_alias_never_claims_completion(self):
        self.opener=Opener([Response({'result':{'result':{'unexpected':{}}}})])
        r=self.client().batch_many({'a':{'method':'crm.deal.get','params':{'id':1}}})
        self.assertEqual(r['status'],'PARTIAL');self.assertFalse(r['commands_complete'])
    def test_malformed_batch_saved_instead_of_crashing(self):
        for value in ([],False,{'result':['unexpected']}):
            self.opener=Opener([Response({'result':value})])
            r=self.client().batch_many({'a':{'method':'crm.deal.get'}})
            self.assertFalse(r['commands_complete']);self.assertEqual(r['batches'][0]['status'],'INVALID_RESPONSE')
    def test_dependencies_survive_native_batch_boundary(self):
        commands={'n'+str(i):{'method':'crm.deal.get','params':{'id':i}} for i in range(51)}
        commands['n50']['params']['id']='$result[n0][ID]'
        first={name:{'ID':777} for name in list(commands)[:50]}
        self.opener=Opener([Response({'result':{'result':first}}),Response({'result':{'result':{'n50':{}}}})])
        r=self.client().batch_many(commands)
        self.assertTrue(r['commands_complete'])
        command=json.loads(self.opener.requests[1].data)['cmd']['n50']
        self.assertEqual(command,'crm.deal.get?id=777')
    def test_missing_dependency_not_sent_as_wrong_request(self):
        commands={'n'+str(i):{'method':'crm.deal.get','params':{'id':i}} for i in range(51)}
        commands['n50']['params']['id']='$result[n0][ID]'
        self.opener=Opener([Response({'result':{'result':{name:{} for name in list(commands)[1:50]},'result_error':{'n0':{'error':'DENIED'}}}})])
        r=self.client().batch_many(commands)
        self.assertFalse(r['commands_complete']);self.assertEqual(len(self.opener.requests),1)
        self.assertEqual(r['not_sent'],['n50'])
    def test_real_bitrix_empty_error_array_is_valid(self):
        self.opener=Opener([Response({'result':{'result':{'a':[]},'result_error':[],'result_next':[]}})])
        r=self.client().batch_many({'a':{'method':'crm.lead.list'}})
        self.assertTrue(r['commands_complete'])
    def test_real_bitrix_empty_success_array_preserves_errors(self):
        self.opener=Opener([Response({'result':{'result':[],'result_error':{'a':{'error':'DENIED'}}}})])
        r=self.client().batch_many({'a':{'method':'crm.lead.list'}})
        self.assertFalse(r['commands_complete']);self.assertEqual(r['status'],'PARTIAL')

if __name__=='__main__':unittest.main()
