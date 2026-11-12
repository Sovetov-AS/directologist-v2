#!/usr/bin/env python3
"""Thin Direct JSON/Reports client. No campaign policy or legacy engine imports."""
import argparse
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = Path.home() / '.local/share/1pi-directologist4'

class ClientError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def keychain_token(reference):
    """Read an existing OS entry; never migrate or print the credential."""
    try:
        result = subprocess.run(['/usr/bin/security','find-generic-password','-s',reference['service'],
                                 '-a',reference['account'],'-w'],capture_output=True,text=True,timeout=30)
        token = result.stdout.strip()
        if result.returncode or not token or any(c.isspace() for c in token):
            raise ClientError('Keychain credential unavailable')
        return token
    except (OSError, subprocess.SubprocessError, KeyError):
        raise ClientError('Keychain credential unavailable') from None

def encoded(value):
    return json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def private_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump(value,f,ensure_ascii=False,indent=2)

class DirectClient:
    def __init__(self, config, catalog, *, token_getter=None, member_token_getter=None, opener=None, journal=None):
        self.config=config; self.catalog=catalog
        self.token_getter=token_getter or (lambda:keychain_token(config['credential']))
        self.member_token_getter=member_token_getter or (lambda:keychain_token(config['member_credential']))
        self.opener=opener or urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),
                          urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.journal=Path(journal or PRIVATE/'requests.jsonl')

    def prepare(self, service, method, params, *, version='v501'):
        if version not in ('v501','v5') or not isinstance(params,dict):
            raise ClientError('Invalid version or params')
        services=self.catalog['services']
        names={name.lower():name for name in services}
        name=names.get(service.lower())
        if name is None or not re.fullmatch('[A-Za-z]+',name):
            raise ClientError('Service missing from documented catalog')
        info=services[name]['methods'].get(method)
        if not isinstance(info,dict) or info.get('effect') not in ('read','write'):
            raise ClientError('Method missing or effect not classified; review official documentation')
        if self.config.get('environment')!='production':
            raise ClientError('Only production endpoint configured; no implicit environment switch')
        report=name.lower()=='reports'
        body={'params':params} if report else {'method':method,'params':params}
        return {'service':name,'method':method,'effect':info['effect'],
                'url':f'https://api.direct.yandex.com/json/{version}/{name.lower()}',
                'body':body,'report':report}

    def _record(self, record, *, unique=False):
        self.journal.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(self.journal,os.O_RDWR|os.O_CREAT,0o600)
        with os.fdopen(fd,'r+') as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            if unique:
                for line in f:
                    if json.loads(line).get('operation_id')==record['operation_id']:
                        raise ClientError('Operation already recorded; reconcile before any new submission')
            f.seek(0,2); f.write(json.dumps(record,ensure_ascii=False)+'\n'); f.flush(); os.fsync(f.fileno())

    def call(self, service, method, params, *, version='v501', execute_write=False,
             operation_id=None, report_headers=None):
        plan=self.prepare(service,method,params,version=version)
        writing=plan['effect']=='write'
        if writing and (self.config.get('writes_enabled') is not True or not execute_write or not operation_id):
            raise ClientError('Current assignment is read-only; writes require changed authority and deliberate execution')
        if not operation_id: operation_id=uuid.uuid4().hex
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',operation_id): raise ClientError('Invalid operation_id')
        headers={'Accept-Language':'ru','Content-Type':'application/json; charset=utf-8'}
        login=self.config.get('client_login')
        if login and plan['service'].lower()!='agencyclients': headers['Client-Login']=login
        if self.config.get('use_operator_units') is True: headers['Use-Operator-Units']='true'
        if report_headers:
            allowed={'processingMode','returnMoneyInMicros','skipReportHeader','skipColumnHeader','skipReportSummary'}
            if not plan['report'] or set(report_headers)-allowed:
                raise ClientError('Invalid report headers')
            headers.update(report_headers)
        body=encoded(plan['body'])
        token=self.token_getter()
        headers['Authorization']='Bearer '+token
        secrets=[token]
        if plan['service'].lower()=='agencyclients' and method=='addPassportOrganization':
            try: member=self.member_token_getter()
            except KeyError: raise ClientError('Member credential reference required by this provider method') from None
            headers['Member-Authorization']='Bearer '+member
            secrets.append(member)
        base={'operation_id':operation_id,'service':plan['service'],'method':method,
              'request_sha256':hashlib.sha256(body).hexdigest()}
        self._record(dict(base,at=now(),status='STARTED'),unique=writing)
        request=urllib.request.Request(plan['url'],data=body,headers=headers,method='POST')
        result={'operation_id':operation_id,'status':'UNKNOWN','http_status':None,'headers':{}}
        try:
            try:
                response=self.opener.open(request,timeout=self.config.get('timeout_seconds',30))
            except urllib.error.HTTPError as error:
                response=error
            with response:
                status=response.status
                result['http_status']=status
                for key in ('RequestId','Units','retryIn','reportsInQueue','Retry-After'):
                    value=response.headers.get(key)
                    if value is not None: result['headers'][key]=value
                raw=response.read()
            for secret in secrets:
                if secret.encode() in raw:
                    raw=raw.replace(secret.encode(),b'[REDACTED]')
                    result['redacted']=True
            if status in (201,202) and plan['report']:
                result['status']='PENDING'
            else:
                try: data=json.loads(raw)
                except (ValueError,UnicodeError): data=None
                if isinstance(data,dict) and 'error' in data:
                    result['status']='UNKNOWN' if writing and status>=500 else 'API_ERROR'
                    err=data.get('error') or {}
                    result['error_code']=err.get('error_code')
                    result['error']=err  # Stored in private response, omitted from console summary.
                elif status!=200:
                    result['status']='UNKNOWN' if writing and status>=500 else 'HTTP_ERROR'
                elif plan['report']:
                    result.update(status='OK',data=raw.decode('utf-8-sig'))
                elif isinstance(data,dict) and isinstance(data.get('result'),dict):
                    payload=data['result']
                    failed=any(isinstance(v,list) and any(isinstance(x,dict) and x.get('Errors') for x in v)
                               for v in payload.values())
                    result.update(status='PARTIAL' if failed else 'OK',data=payload)
                    if payload.get('LimitedBy') is not None: result['limited_by']=payload['LimitedBy']
                else:
                    result['status']='UNKNOWN' if writing else 'INVALID_RESPONSE'
        except (OSError,urllib.error.URLError,http.client.HTTPException,ValueError,UnicodeError):
            result['status']='UNKNOWN'
        result['response_sha256']=hashlib.sha256(encoded(result)).hexdigest()
        self._record(dict(base,at=now(),status=result['status'],http_status=result['http_status'],
                          provider_request_id=result['headers'].get('RequestId'),response_sha256=result['response_sha256']))
        return result

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['catalog','prepare','call'])
    parser.add_argument('--service'); parser.add_argument('--method',default='get')
    parser.add_argument('--params',type=Path); parser.add_argument('--version',default='v501',choices=['v501','v5'])
    parser.add_argument('--output',type=Path)
    parser.add_argument('--report-headers',type=Path)
    parser.add_argument('--execute-write',action='store_true'); parser.add_argument('--operation-id')
    args=parser.parse_args()
    catalog=json.loads((ROOT/'config/yandex-direct-methods.json').read_text())
    if args.action=='catalog':
        print(json.dumps(catalog,ensure_ascii=False,indent=2)); return
    if not args.service or not args.params: parser.error('--service and --params are required')
    config=json.loads((ROOT/'config/yandex-connection.json').read_text())['direct']
    client=DirectClient(config,catalog)
    params=json.loads(args.params.read_text())
    if args.action=='prepare':
        result=client.prepare(args.service,args.method,params,version=args.version)
        if args.output: private_json(args.output,result)
        else: print(json.dumps(result,ensure_ascii=False,indent=2))
        return
    report_headers=json.loads(args.report_headers.read_text()) if args.report_headers else None
    result=client.call(args.service,args.method,params,version=args.version,
                       execute_write=args.execute_write,operation_id=args.operation_id,report_headers=report_headers)
    output=args.output or PRIVATE/'responses'/(result['operation_id']+'.json')
    private_json(output,result)
    print(json.dumps({k:v for k,v in result.items() if k not in ('data','error')},ensure_ascii=False))
    print('Saved response:',output)
    if result['status'] not in ('OK','PENDING'): raise SystemExit(1)

if __name__=='__main__':
    try: main()
    except (ClientError, OSError, ValueError, KeyError) as error:
        # Avoid raw paths, input bodies or keychain diagnostics in error output.
        print('ERROR:',str(error) if isinstance(error,ClientError) else type(error).__name__)
        raise SystemExit(1)
