#!/usr/bin/env python3
"""Bitrix24 REST client with explicit effects, native batches and uncapped pagination."""
import argparse
import copy
import hashlib
import http.client
import json
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

from yandex_direct import ROOT, PRIVATE, ClientError, NoRedirect, encoded, keychain_token, now
from yandex_data import save_private, DataClient


def form_pairs(value, prefix=''):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from form_pairs(item, str(key) if not prefix else prefix + '[' + str(key) + ']')
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from form_pairs(item, prefix + '[' + str(i) + ']')
    else:
        yield prefix, ('Y' if value is True else 'N' if value is False else '' if value is None else value)


class BitrixClient:
    def __init__(self, config, catalog, *, token_getter=None, opener=None, journal=None):
        self.config, self.catalog = config, catalog
        self.token_getter = token_getter or (lambda: keychain_token(config['credential']))
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.journal = Path(journal or PRIVATE / 'bitrix24-requests.jsonl')

    _record = DataClient._record

    def prepare(self, method, params=None):
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.]*', method):
            raise ClientError('Invalid REST method name')
        ops = [o for o in self.catalog['operations'] if o['id'] == method]
        if len(ops) != 1 or ops[0].get('effect') not in ('read', 'write', 'container'):
            raise ClientError('Method not classified; verify official documentation and extend catalog')
        params = copy.deepcopy(params if params is not None else {})
        if not isinstance(params, dict) or 'auth' in params:
            raise ClientError('Parameters must be an object without embedded credentials')
        effect = ops[0]['effect']
        if method == 'batch':
            commands = params.get('cmd')
            if not isinstance(commands, dict) or not commands:
                raise ClientError('Batch requires named commands')
            if len(commands) > 50:
                raise ClientError('Provider batch maximum is 50; use batch-many to split the whole task')
            effects = []
            for alias, command in commands.items():
                if not re.fullmatch(r'[a-zA-Z0-9_]+', alias) or not isinstance(command, str):
                    raise ClientError('Invalid batch command')
                name, _, query = command.partition('?')
                if name == 'batch':
                    raise ClientError('Nested provider batches are unsupported')
                args = urllib.parse.parse_qs(query, keep_blank_values=True)
                effects.append(self.prepare(name, args)['effect'])
            effect = 'write' if 'write' in effects else 'read'
        elif effect == 'container':
            raise ClientError('Unknown container effect')
        return {'method': method, 'params': params, 'effect': effect}

    def _endpoint(self):
        webhook = self.token_getter()
        url = urllib.parse.urlsplit(webhook)
        origin = self.config['portal_origin']
        if (self.config.get('auth') != 'incoming-webhook' or url.scheme != 'https'
            or origin != 'https://' + url.netloc or url.username or url.password
            or url.query or url.fragment or not re.fullmatch(r'/rest/[1-9][0-9]*/[A-Za-z0-9_-]+/?', url.path)):
            raise ClientError('Stored webhook does not match configured HTTPS portal')
        return webhook.rstrip('/') + '/'

    def call(self, method, params=None, *, execute_write=False, operation_id=None):
        plan = self.prepare(method, params)
        writing = plan['effect'] == 'write'
        if writing and (self.config.get('writes_enabled') is not True or not execute_write or not operation_id):
            raise ClientError('Current assignment forbids CRM writes')
        operation_id = operation_id or uuid.uuid4().hex
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', operation_id):
            raise ClientError('Invalid operation_id')
        endpoint = self._endpoint()
        secret = endpoint.rstrip('/').rsplit('/', 1)[-1]
        base = {'operation_id': operation_id, 'method': method, 'effect': plan['effect'],
                'request_sha256': hashlib.sha256(encoded(plan)).hexdigest()}
        self._record(dict(base, at=now(), status='STARTED'), unique=writing)
        request = urllib.request.Request(endpoint + method + '.json', data=encoded(plan['params']),
                                         headers={'Content-Type': 'application/json; charset=utf-8'}, method='POST')
        result = dict(base, at=now(), status='UNKNOWN', http_status=None, headers={})
        try:
            try:
                response = self.opener.open(request, timeout=self.config.get('timeout_seconds', 60))
            except urllib.error.HTTPError as error:
                response = error
            with response:
                result['http_status'] = response.status
                for header in ('X-Request-Id', 'X-Bitrix-Rest-Time', 'Retry-After'):
                    value = response.headers.get(header)
                    if value is not None: result['headers'][header] = value
                raw = response.read()
            for value in (endpoint, secret):
                raw = raw.replace(value.encode(), b'[REDACTED]')
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                data = None
            status = result['http_status']
            if isinstance(data, dict) and 'error' in data:
                result.update(status='UNKNOWN' if writing and status >= 500 else 'API_ERROR',
                              error_code=data['error'], error=data.get('error_description'))
            elif not 200 <= status < 300:
                result['status'] = 'UNKNOWN' if writing and status >= 500 else 'HTTP_ERROR'
            elif not isinstance(data, dict) or 'result' not in data:
                result['status'] = 'UNKNOWN' if writing else 'INVALID_RESPONSE'
            else:
                result.update(status='OK', data=data['result'])
                for key in ('next', 'total', 'time'):
                    if key in data: result[key] = data[key]
                if data['result'] is False:
                    result['status'] = 'API_ERROR'
                if method == 'batch':
                    payload = data['result']
                    if (not isinstance(payload, dict)
                        or not (isinstance(payload.get('result'), dict) or payload.get('result') == [])
                        or not (isinstance(payload.get('result_error', {}), dict) or payload.get('result_error') == [])):
                        result['status'] = 'UNKNOWN' if writing else 'INVALID_RESPONSE'
                    else:
                        returned = set(payload['result'] or {}) | set(payload.get('result_error') or {})
                        expected = set(plan['params']['cmd'])
                        if payload.get('result_error') or returned != expected:
                            result['status'] = 'PARTIAL'
                            result['missing_commands'] = sorted(expected - returned)
                            result['unexpected_commands'] = sorted(returned - expected)
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, UnicodeError):
            result['status'] = 'UNKNOWN'
        result['response_sha256'] = hashlib.sha256(encoded(result)).hexdigest()
        self._record({k: v for k, v in result.items() if k not in ('data', 'error')})
        return result

    def all_pages(self, method, params=None, *, items_key=None):
        plan = self.prepare(method, params)
        if plan['effect'] != 'read' or method == 'batch':
            raise ClientError('Pagination is for read list methods only')
        current = copy.deepcopy(plan['params'])
        pages, items, seen = [], [], set()
        while True:
            start = current.get('start', 0)
            if str(start) in seen:
                return {'status': 'PARTIAL', 'complete': False, 'reason': 'repeated_cursor', 'items': items, 'pages': pages}
            seen.add(str(start))
            page = self.call(method, current)
            pages.append(page)
            if page['status'] != 'OK':
                return {'status': 'PARTIAL', 'complete': False, 'reason': 'page_failed', 'items': items, 'pages': pages}
            data = page['data']
            rows = data.get(items_key) if items_key and isinstance(data, dict) else data
            if not isinstance(rows, list):
                return {'status': 'PARTIAL', 'complete': False, 'reason': 'Specify items_key for nested result', 'items': items, 'pages': pages}
            items.extend(rows)
            next_start = page.get('next', data.get('next') if isinstance(data, dict) else None)
            if next_start is None:
                complete = str(current.get('start', '')) != '-1'
                return {'status': 'OK' if complete else 'PARTIAL', 'complete': complete,
                        'reason': None if complete else 'start=-1 disables pagination metadata', 'items': items, 'pages': pages}
            if not isinstance(next_start, int) or next_start < 0:
                return {'status': 'PARTIAL', 'complete': False, 'reason': 'invalid_cursor', 'items': items, 'pages': pages}
            current['start'] = next_start

    def batch_many(self, commands):
        """Read-only multi-batch helper; provider's 50-command packets do not cap the task."""
        if not isinstance(commands, dict): raise ClientError('Named commands expected')
        plans = {}
        for alias, item in commands.items():
            if not re.fullmatch(r'[a-zA-Z0-9_]+', alias): raise ClientError('Invalid command alias')
            plan = self.prepare(item['method'], item.get('params', {}))
            if plan['effect'] != 'read' or item['method'] == 'batch':
                raise ClientError('batch-many currently executes read commands only')
            plans[alias] = plan
        names = list(plans)
        positions = {name: i for i, name in enumerate(names)}
        pattern = re.compile(r'\$result((?:\[[A-Za-z0-9_.-]+\])+)')
        for alias, plan in plans.items():
            for _, value in form_pairs(plan['params']):
                for match in pattern.finditer(str(value)):
                    dependency = re.findall(r'\[([^]]+)\]', match[1])[0]
                    if dependency not in positions or positions[dependency] >= positions[alias]:
                        raise ClientError('Batch references must name a preceding command')
        results, successful = [], {}
        for i in range(0, len(names), 50):
            packet = {}
            def resolve(match):
                keys = re.findall(r'\[([^]]+)\]', match[1])
                if positions[keys[0]] >= i: return match[0]
                value = successful[keys[0]]
                for key in keys[1:]: value = value[int(key)] if isinstance(value, list) else value[key]
                if isinstance(value, (dict, list)): raise ValueError('Non-scalar batch reference')
                return str(value)
            try:
                for name in names[i:i+50]:
                    pairs = [(key, pattern.sub(resolve, str(value))) for key, value in form_pairs(plans[name]['params'])]
                    packet[name] = plans[name]['method'] + '?' + urllib.parse.urlencode(pairs)
            except (KeyError, IndexError, TypeError, ValueError):
                return {'status': 'PARTIAL', 'commands_complete': False, 'batches': results,
                        'reason': 'Prior batch dependency unavailable', 'not_sent': names[i:]}
            result = self.call('batch', {'halt': 0, 'cmd': packet})
            results.append(result)
            if result['status'] not in ('OK', 'PARTIAL'):
                break
            successful.update({k: v for k, v in (result['data']['result'] or {}).items() if k in packet})
        complete = set(successful) == set(names) and all(r['status'] == 'OK' for r in results)
        return {'status': 'OK' if complete else 'PARTIAL', 'commands_complete': complete,
                'batches': results, 'note': 'Command completion does not imply all pages of each list.'}


def public_summary(result):
    out = {k: v for k, v in result.items() if k not in ('data', 'error', 'items', 'pages', 'batches')}
    if 'items' in result: out.update(items_count=len(result['items']), pages_count=len(result['pages']))
    if 'batches' in result: out['batches_count'] = len(result['batches'])
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('catalog', 'prepare', 'call', 'list-all', 'batch-many'))
    parser.add_argument('--method'); parser.add_argument('--params', type=Path)
    parser.add_argument('--items-key'); parser.add_argument('--output', type=Path)
    parser.add_argument('--execute-write', action='store_true'); parser.add_argument('--operation-id')
    args = parser.parse_args()
    catalog = json.loads((ROOT / 'config/bitrix24-methods.json').read_text())
    if args.action == 'catalog':
        print(json.dumps(catalog, ensure_ascii=False, indent=2)); return
    client = BitrixClient(json.loads((ROOT / 'config/bitrix24-connection.json').read_text()), catalog)
    params = json.loads(args.params.read_text()) if args.params else {}
    if args.action == 'batch-many': result = client.batch_many(params)
    else:
        if not args.method: parser.error('--method required')
        if args.action == 'prepare':
            plan = client.prepare(args.method, params)
            # No CRM data, webhook URL or token in preview.
            print(json.dumps({'method': args.method, 'effect': plan['effect'], 'params_sha256': hashlib.sha256(encoded(params)).hexdigest()}, ensure_ascii=False)); return
        if args.action == 'list-all': result = client.all_pages(args.method, params, items_key=args.items_key)
        else: result = client.call(args.method, params, execute_write=args.execute_write, operation_id=args.operation_id)
    output = args.output or PRIVATE / 'responses' / ('bitrix-' + uuid.uuid4().hex + '.json')
    save_private(output, {'request': {'method': args.method, 'params': params}, 'response': result})
    print(json.dumps(public_summary(result), ensure_ascii=False)); print('Saved response:', output)
    if result['status'] != 'OK': raise SystemExit(1)


if __name__ == '__main__':
    try: main()
    except (ClientError, OSError, ValueError, KeyError) as error:
        print('ERROR:', str(error) if isinstance(error, ClientError) else type(error).__name__)
        raise SystemExit(1)
