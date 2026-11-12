#!/usr/bin/env python3
"""Catalog-driven Metrika / Wordstat transport, without campaign policy or quotas."""
import argparse
import base64
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

from yandex_direct import ClientError, NoRedirect, ROOT, PRIVATE, encoded, keychain_token, now

ORIGINS = {
    'metrika': {'https://api-metrika.yandex.net', 'https://mc.yandex.ru'},
    'wordstat': {'https://searchapi.api.cloud.yandex.net'},
}


def save_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def multipart(file_path, field='file', fields=None):
    """Encode upload bytes unchanged; no data are uploaded by this helper."""
    boundary = 'codex-' + uuid.uuid4().hex
    parts = []
    values = list((fields or {}).items()) + [(field, Path(file_path).read_bytes())]
    for name, value in values:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name):
            raise ClientError('Invalid multipart field name')
        parts.append(('--' + boundary + '\r\n').encode())
        disposition = 'Content-Disposition: form-data; name="' + name + '"'
        if isinstance(value, bytes):
            disposition += '; filename="upload' + Path(file_path).suffix.replace('"', '') + '"'
            if '\r' in disposition or '\n' in disposition:
                raise ClientError('Invalid upload filename')
            parts.append((disposition + '\r\nContent-Type: application/octet-stream\r\n\r\n').encode())
            parts.append(value)
        else:
            parts.append((disposition + '\r\n\r\n' + str(value)).encode())
        parts.append(b'\r\n')
    parts.append(('--' + boundary + '--\r\n').encode())
    return b''.join(parts), 'multipart/form-data; boundary=' + boundary


class DataClient:
    def __init__(self, api, config, catalog, *, token_getter=None,
                 measurement_token_getter=None, opener=None, journal=None):
        if api not in ORIGINS:
            raise ClientError('Unknown API')
        self.api, self.config, self.catalog = api, config, catalog
        self.token_getter = token_getter or (lambda: keychain_token(config['credential']))
        self.measurement_token_getter = measurement_token_getter or (
            lambda: keychain_token(config['measurement_credential']))
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.journal = Path(journal or PRIVATE / 'data-requests.jsonl')

    def prepare(self, operation, *, path_params=None, query=None, body=None,
                raw_body=None, content_type=None, variant=None):
        matches = [op for op in self.catalog['operations'] if op['id'] == operation]
        if len(matches) != 1 or matches[0].get('effect') not in ('read', 'write'):
            raise ClientError('Operation absent or effect not classified; consult official catalog')
        op = dict(matches[0])
        if op.get('documentation_status') == 'inconsistent':
            raise ClientError('Provider documentation inconsistent; use ' + op.get('use_instead', 'verified operation'))
        if variant:
            representation = op.get('response_variants', {}).get(variant)
            if not representation:
                raise ClientError('Response variant not documented in catalog')
            op.update({k: v for k, v in representation.items() if k in ('path', 'response_format')})
        origin = op.get('endpoint', self.catalog.get('endpoint', ''))
        if origin not in ORIGINS[self.api]:
            raise ClientError('Endpoint not supported for this API')
        method = op['method'].upper()
        if method not in ('GET', 'POST', 'PUT', 'DELETE', 'PATCH'):
            raise ClientError('Unsupported HTTP method')
        template = op['path']
        path_params, query = dict(path_params or {}), dict(query or {})
        required = set(re.findall(r'\{([A-Za-z0-9_]+)\}', template))
        if set(path_params) != required:
            raise ClientError('Path parameters must match operation template')
        for key, value in path_params.items():
            value = str(value)
            if not value or value in ('.', '..') or any(c in value for c in '/\\?#\r\n'):
                raise ClientError('Invalid path parameter')
            template = template.replace('{' + key + '}', urllib.parse.quote(value, safe=''))
        if not template.startswith('/') or template.startswith('//') or any(c in template for c in '?#{}'):
            raise ClientError('Invalid path in catalog')
        for key, value in op.get('required_query', {}).items():
            if key in query and str(query[key]) != str(value):
                raise ClientError('Query conflicts with documented operation')
            query[key] = value
        auth = op.get('auth', 'oauth' if self.api == 'metrika' else 'api_key')
        secret_query = op.get('secret_query', {'ms': 'measurement_credential'} if auth == 'measurement_token' else {})
        if any(k in query for k in secret_query):
            raise ClientError('Secret query values must come from Keychain references')
        if body is not None and raw_body is not None:
            raise ClientError('Choose JSON body or raw upload, not both')
        if raw_body is not None and not isinstance(raw_body, bytes):
            raise ClientError('Raw body must be bytes')
        payload = raw_body if raw_body is not None else (encoded(body) if body is not None else None)
        suffix = urllib.parse.urlencode(query, doseq=True)
        return {'api': self.api, 'operation': operation, 'effect': op['effect'],
                'paid': bool(op.get('paid', False)), 'method': method,
                'url': origin + template + ('?' + suffix if suffix else ''),
                'payload': payload, 'auth': auth, 'secret_query': secret_query,
                'content_type': content_type or ('application/json; charset=utf-8' if body is not None else None),
                'response_format': op.get('response_format', 'json')}

    def _record(self, record, unique=False):
        self.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.journal, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            if unique:
                for line in stream:
                    if json.loads(line).get('operation_id') == record['operation_id']:
                        raise ClientError('Operation already recorded; reconcile before repeating')
            stream.seek(0, 2)
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def call(self, operation, *, execute_write=False, execute_paid=False, operation_id=None, **params):
        plan = self.prepare(operation, **params)
        writing, paid = plan['effect'] == 'write', plan['paid']
        if writing and (self.config.get('writes_enabled') is not True or not execute_write or not operation_id):
            raise ClientError('Current assignment does not enable writes')
        if paid and (not execute_paid or not operation_id):
            raise ClientError('Paid request needs deliberate execution and an operation ID')
        operation_id = operation_id or uuid.uuid4().hex
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', operation_id):
            raise ClientError('Invalid operation_id')
        headers = {'Accept': '*/*', 'User-Agent': 'Directologist4/1.0'}
        if plan['content_type']:
            headers['Content-Type'] = plan['content_type']
        url = plan['url']
        secrets = []
        if plan['auth'] == 'measurement_token':
            if not url.startswith('https://mc.yandex.ru/'):
                raise ClientError('Invalid measurement endpoint')
        else:
            if url.startswith('https://mc.yandex.ru/'):
                raise ClientError('OAuth must not be sent to measurement endpoint')
            token = self.token_getter()
            scheme = 'OAuth' if self.api == 'metrika' else self.config.get('auth_scheme', 'Api-Key')
            if scheme not in ('OAuth', 'Api-Key', 'Bearer'):
                raise ClientError('Unknown auth scheme')
            headers['Authorization'] = scheme + ' ' + token
            secrets.append(token)
        for parameter, reference in plan['secret_query'].items():
            if reference != 'measurement_credential':
                raise ClientError('Unknown secret reference in catalog')
            try:
                secret = self.measurement_token_getter()
            except KeyError:
                raise ClientError('Measurement credential not connected') from None
            secrets.append(secret)
            url += ('&' if '?' in url else '?') + urllib.parse.urlencode({parameter: secret})
        fingerprint = encoded([plan['method'], plan['url'], plan['content_type']]) + (plan['payload'] or b'')
        base = {'operation_id': operation_id, 'api': self.api, 'operation': operation,
                'effect': plan['effect'], 'paid': paid,
                'request_sha256': hashlib.sha256(fingerprint).hexdigest()}
        self._record(dict(base, at=now(), status='STARTED'), unique=writing or paid)
        result = dict(base, at=now(), status='UNKNOWN', http_status=None, headers={})
        request = urllib.request.Request(url, data=plan['payload'], headers=headers, method=plan['method'])
        try:
            try:
                response = self.opener.open(request, timeout=self.config.get('timeout_seconds', 60))
            except urllib.error.HTTPError as error:
                response = error
            with response:
                result['http_status'] = response.status
                for key in ('X-Request-Id', 'RequestId', 'X-Trace-Id', 'Retry-After', 'Content-Type'):
                    value = response.headers.get(key)
                    if value is not None:
                        result['headers'][key] = value
                raw = response.read()
            for value in secrets:
                for secret in (value.encode(), urllib.parse.quote(value, safe='').encode()):
                    if secret in raw:
                        raw = raw.replace(secret, b'[REDACTED]')
                        result['redacted'] = True
            status = result['http_status']
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError):
                data = None
            if not 200 <= status < 300:
                result['status'] = 'UNKNOWN' if writing and status >= 500 else 'API_ERROR'
                result['error'] = data if data is not None else raw.decode('utf-8', errors='replace')
                if isinstance(data, dict):
                    result['error_code'] = data.get('code', data.get('error_code'))
                    result['error_types'] = [e.get('error_type') for e in data.get('errors', []) if isinstance(e, dict)]
            elif isinstance(data, dict) and (data.get('errors') or data.get('error') or data.get('success') is False):
                result.update(status='API_ERROR', error=data)
            elif status == 202:
                result.update(status='PENDING', data=data)
            elif plan['response_format'] == 'binary':
                result.update(status='OK', data_base64=base64.b64encode(raw).decode())
            elif plan['response_format'] == 'text':
                result.update(status='OK', data=raw.decode('utf-8-sig'))
            elif data is not None:
                result.update(status='OK', data=data)
                if isinstance(data, dict):
                    for key in ('total_rows', 'total_rows_rounded', 'sampled', 'sample_share', 'sample_size', 'sample_space', 'data_lag'):
                        if key in data:
                            result[key] = data[key]
            elif status == 204:
                result['status'] = 'OK'
            else:
                result['status'] = 'UNKNOWN' if writing else 'INVALID_RESPONSE'
        except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError, UnicodeError):
            result['status'] = 'UNKNOWN'
        result['response_sha256'] = hashlib.sha256(encoded(result)).hexdigest()
        self._record({k: v for k, v in result.items() if k not in ('data', 'data_base64', 'error')})
        return result


def summary(result):
    return {k: v for k, v in result.items() if k not in ('data', 'data_base64', 'error')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('api', choices=tuple(ORIGINS))
    parser.add_argument('action', choices=('catalog', 'prepare', 'call'))
    parser.add_argument('--operation')
    parser.add_argument('--variant')
    for name in ('path-params', 'query', 'body', 'form-params'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--raw-body', type=Path)
    parser.add_argument('--content-type')
    parser.add_argument('--upload-file', type=Path)
    parser.add_argument('--upload-field', default='file')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--execute-write', action='store_true')
    parser.add_argument('--execute-paid', action='store_true')
    parser.add_argument('--operation-id')
    args = parser.parse_args()
    catalog = json.loads((ROOT / ('config/yandex-' + args.api + '-methods.json')).read_text())
    if args.action == 'catalog':
        print(json.dumps(catalog, ensure_ascii=False, indent=2))
        return
    if not args.operation:
        parser.error('--operation is required')
    config = json.loads((ROOT / 'config/yandex-connection.json').read_text())[args.api]
    client = DataClient(args.api, config, catalog)
    def read_json(path):
        return json.loads(path.read_text()) if path else None
    raw = args.raw_body.read_bytes() if args.raw_body else None
    content_type = args.content_type
    if args.upload_file:
        if args.raw_body or args.body:
            parser.error('Choose one body input')
        raw, content_type = multipart(args.upload_file, args.upload_field, read_json(args.form_params))
    params = dict(path_params=read_json(args.path_params), query=read_json(args.query),
                  body=read_json(args.body), raw_body=raw, content_type=content_type, variant=args.variant)
    if args.action == 'prepare':
        result = client.prepare(args.operation, **params)
        payload = result.pop('payload')
        result['payload_bytes'] = len(payload or b'')
        result['payload_sha256'] = hashlib.sha256(payload or b'').hexdigest()
        # Parameters stay in caller-owned files; preparation output carries no upload contents.
        if args.output:
            save_private(args.output, result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    result = client.call(args.operation, execute_write=args.execute_write, execute_paid=args.execute_paid,
                         operation_id=args.operation_id, **params)
    output = args.output or PRIVATE / 'responses' / (result['operation_id'] + '.json')
    save_private(output, {'request': {'operation': args.operation, 'path_params': params['path_params'],
                                    'query': params['query'], 'body': params['body']}, 'response': result})
    print(json.dumps(summary(result), ensure_ascii=False))
    print('Saved response:', output)
    if result['status'] not in ('OK', 'PENDING'):
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except (ClientError, OSError, ValueError, KeyError) as error:
        print('ERROR:', str(error) if isinstance(error, ClientError) else type(error).__name__)
        raise SystemExit(1)
