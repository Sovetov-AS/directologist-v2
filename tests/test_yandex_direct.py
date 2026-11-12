import http.client
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

SPEC = importlib.util.spec_from_file_location('yandex_direct', Path(__file__).parents[1] / 'scripts/yandex_direct.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

CATALOG = {'services': {'Campaigns': {'methods': {'get': {'effect':'read'}, 'add': {'effect':'write'}}}, 'Reports': {'methods': {'get': {'effect':'read'}}}}}

class Response:
    def __init__(self, data=b'{"result":{"Campaigns":[]}}', status=200):
        self.data, self.status = data, status
        self.headers = {'RequestId':'test-request', 'retryIn':'3'}
    def __enter__(self): return self
    def __exit__(self,*a): pass
    def read(self): return self.data

class Opener:
    def __init__(self, response=None, error=None): self.response=response or Response(); self.error=error; self.requests=[]
    def open(self,req,timeout):
        self.requests.append(req)
        if self.error: raise self.error
        return self.response

class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.config={'environment':'production','client_login':'test-login','writes_enabled':False}
        self.opener=Opener(); self.token_calls=0
    def token(self): self.token_calls+=1; return 'TEST_SECRET_TOKEN'
    def client(self): return m.DirectClient(self.config,CATALOG,token_getter=self.token,opener=self.opener,journal=Path(self.tmp.name)/'journal.jsonl')
    def test_prepare_keeps_new_fields_and_large_batches(self):
        params={'Campaigns':[{'NewFutureField':None}]*35}
        p=self.client().prepare('Campaigns','add',params)
        self.assertEqual(p['body']['params'],params); self.assertEqual(self.token_calls,0)
    def test_current_read_only_blocks_before_credential_or_network(self):
        with self.assertRaises(m.ClientError): self.client().call('Campaigns','add',{},execute_write=True,operation_id='w1')
        self.assertEqual(self.token_calls,0); self.assertEqual(self.opener.requests,[])
    def test_transport_passes_fields_without_schema_filter(self):
        result=self.client().call('Campaigns','get',{'FieldNames':['NewField']})
        self.assertEqual(result['status'],'OK')
        self.assertEqual(json.loads(self.opener.requests[0].data)['params']['FieldNames'],['NewField'])
        self.assertNotIn('TEST_SECRET_TOKEN',(Path(self.tmp.name)/'journal.jsonl').read_text())
    def test_unknown_method_not_assumed_read(self):
        with self.assertRaises(m.ClientError): self.client().call('Campaigns','newMethod',{})
        self.assertEqual(self.token_calls,0)
    def test_reports_pending_is_not_empty_success(self):
        self.opener=Opener(Response(b'',202)); result=self.client().call('Reports','get',{'Name':'test'})
        self.assertEqual(result['status'],'PENDING'); self.assertEqual(result['headers']['retryIn'],'3')
    def test_reports_body_uses_params_without_method(self):
        self.opener=Opener(Response(b'CampaignId\tClicks\n1\t0\n'))
        result=self.client().call('Reports','get',{'Name':'test'})
        self.assertEqual(json.loads(self.opener.requests[0].data),{'params':{'Name':'test'}})
        self.assertIn('CampaignId',result['data'])
    def test_network_failure_no_retry_and_no_raw_exception(self):
        self.opener=Opener(error=URLError('TEST_SECRET_TOKEN'))
        result=self.client().call('Campaigns','get',{})
        self.assertEqual(result['status'],'UNKNOWN'); self.assertEqual(len(self.opener.requests),1)
        self.assertNotIn('TEST_SECRET_TOKEN',json.dumps(result))
    def test_write_unknown_operation_cannot_be_replayed(self):
        self.config['writes_enabled']=True; self.opener=Opener(error=URLError('timeout'))
        c=self.client(); self.assertEqual(c.call('Campaigns','add',{},execute_write=True,operation_id='w1')['status'],'UNKNOWN')
        with self.assertRaises(m.ClientError): c.call('Campaigns','add',{},execute_write=True,operation_id='w1')
        self.assertEqual(len(self.opener.requests),1)
    def test_write_requires_deliberate_execution_and_id(self):
        self.config['writes_enabled']=True
        with self.assertRaises(m.ClientError): self.client().call('Campaigns','add',{})
        self.assertEqual(self.token_calls,0)
    def test_provider_error_retains_code_not_false_success(self):
        self.opener=Opener(Response(b'{"error":{"error_code":8800,"error_string":"TEST_SECRET_TOKEN"}}'))
        result=self.client().call('Campaigns','get',{})
        self.assertEqual(result['status'],'API_ERROR'); self.assertEqual(result['error_code'],8800)
        self.assertNotIn('TEST_SECRET_TOKEN',json.dumps(result))
    def test_partial_object_results_preserved(self):
        self.config['writes_enabled']=True
        data={'result':{'AddResults':[{'Id':1},{'Errors':[{'Code':2}]}]}}
        self.opener=Opener(Response(json.dumps(data).encode()))
        result=self.client().call('Campaigns','add',{},execute_write=True,operation_id='w2')
        self.assertEqual(result['status'],'PARTIAL'); self.assertEqual(result['data'],data['result'])
    def test_agency_requests_omit_client_login(self):
        catalog={'services':{'AgencyClients':{'methods':{'get':{'effect':'read'}}}}}
        c=m.DirectClient(self.config,catalog,token_getter=self.token,opener=self.opener,journal=Path(self.tmp.name)/'j')
        c.call('AgencyClients','get',{})
        self.assertNotIn('Client-login',dict(self.opener.requests[0].header_items()))
    def test_truncated_http_is_unknown(self):
        self.opener=Opener(error=http.client.IncompleteRead(b'partial'))
        result=self.client().call('Campaigns','get',{})
        self.assertEqual(result['status'],'UNKNOWN')
        self.assertIn('UNKNOWN',(Path(self.tmp.name)/'journal.jsonl').read_text())
    def test_malformed_success_after_write_is_unknown(self):
        self.config['writes_enabled']=True; self.opener=Opener(Response(b'broken json'))
        result=self.client().call('Campaigns','add',{},execute_write=True,operation_id='broken')
        self.assertEqual(result['status'],'UNKNOWN')
    def test_member_authorization_supported_without_leaking_token(self):
        self.config['writes_enabled']=True
        cat={'services':{'AgencyClients':{'methods':{'addPassportOrganization':{'effect':'write'}}}}}
        c=m.DirectClient(self.config,cat,token_getter=self.token,member_token_getter=lambda:'MEMBER_SECRET',opener=self.opener,journal=Path(self.tmp.name)/'j')
        c.call('AgencyClients','addPassportOrganization',{},execute_write=True,operation_id='member')
        self.assertEqual(self.opener.requests[0].get_header('Member-authorization'),'Bearer MEMBER_SECRET')
        self.assertNotIn('MEMBER_SECRET',(Path(self.tmp.name)/'j').read_text())
    def test_pagination_visible_in_summary(self):
        self.opener=Opener(Response(b'{"result":{"Campaigns":[],"LimitedBy":1000}}'))
        result=self.client().call('Campaigns','get',{})
        self.assertEqual(result['limited_by'],1000)
    def test_versions_and_service_path_cannot_inject_endpoint(self):
        for service,version in [('Campaigns','../../evil'),('https://evil','v501')]:
            with self.assertRaises(m.ClientError): self.client().prepare(service,'get',{},version=version)

if __name__=='__main__': unittest.main()
