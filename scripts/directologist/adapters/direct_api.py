"""Fixed Direct v501 transport. Called only by the trusted, journaled executor.
No retries: a lost response to a write has an unknown outcome.
"""
import csv
import io
import json
import re
import ssl
import urllib.error
import urllib.request
from decimal import Decimal

from . import NoRedirect
from ..contracts import ContractError, canonical, digest

SERVICES = {'campaigns': 'Campaigns', 'adgroups': 'AdGroups', 'ads': 'Ads', 'keywords': 'Keywords'}
METHODS = {
    'campaigns': {'get', 'add', 'update', 'suspend', 'resume'},
    'adgroups': {'get', 'add', 'update'},
    'ads': {'get', 'add', 'update', 'moderate', 'suspend', 'resume'},
    'keywords': {'get', 'add', 'update', 'suspend', 'resume'},
    'keywordbids': {'get', 'set'}, 'clients': {'get'},
}
FIELDS = {
    'campaigns': ['Id', 'Name', 'Type', 'State', 'Status', 'StartDate', 'EndDate', 'DailyBudget', 'NegativeKeywords'],
    'adgroups': ['Id', 'Name', 'CampaignId', 'RegionIds', 'NegativeKeywords', 'Type'],
    'ads': ['Id', 'AdGroupId', 'CampaignId', 'State', 'Status', 'StatusClarification','Type'],
    'keywords': ['Id', 'AdGroupId', 'CampaignId', 'Keyword', 'State', 'Status', 'Bid', 'AutotargetingSearchBidIsAuto'],
}


class DirectFailure(ContractError):
    def __init__(self, outcome='UNKNOWN', code=None):
        self.outcome = outcome
        self.code = code if type(code) is int else None
        super().__init__('Direct API: ' + outcome + (f' (code {self.code})' if self.code is not None else ''))


def positive_id(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ContractError('Нужен положительный числовой ID Direct.')
    return value


class DirectAPI:
    def __init__(self, credential, client_login, environment, *, opener=None):
        if environment not in {'production', 'sandbox'}:
            raise ContractError('Нужно явно выбрать production или sandbox.')
        if not isinstance(client_login, str) or not re.fullmatch(r'[a-zA-Z0-9@._-]{1,128}', client_login):
            raise ContractError('Некорректный Client-Login.')
        self.credential, self.client_login, self.environment = credential, client_login, environment
        host = 'api.direct.yandex.com' if environment == 'production' else 'api-sandbox.direct.yandex.com'
        self.base = 'https://' + host + '/json/v501/'
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                            urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def _request(self, service, body, *, report=False):
        headers = {'Authorization': 'Bearer ' + self.credential.value, 'Client-Login': self.client_login,
                   'Accept-Language': 'en', 'Content-Type': 'application/json; charset=utf-8'}
        if report:
            headers.update({'processingMode': 'online', 'returnMoneyInMicros': 'true',
                            'skipReportHeader': 'true', 'skipColumnHeader': 'false', 'skipReportSummary': 'true'})
        req = urllib.request.Request(self.base + service, data=canonical(body).encode(), headers=headers, method='POST')
        try:
            with self.opener.open(req, timeout=30) as response:
                status = response.status
                raw = response.read(8 * 1024 * 1024 + 1)
            if status in {201, 202}:
                raise DirectFailure('PENDING')
            if status != 200 or len(raw) > 8 * 1024 * 1024 or self.credential.value.encode() in raw:
                raise DirectFailure()
            if report and not raw.lstrip().startswith(b'{'):
                return raw.decode('utf-8-sig')
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise DirectFailure()
            if 'error' in data:
                code = data['error'].get('error_code') if isinstance(data['error'], dict) else None
                raise DirectFailure('REJECTED', code)
            if report or not isinstance(data.get('result'), dict):
                raise DirectFailure()
            return data['result']
        except DirectFailure:
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise DirectFailure('REJECTED' if status in {401, 403, 429} else 'UNKNOWN', status) from None
        except Exception:
            raise DirectFailure() from None

    def call(self, service, method, params):
        if service not in METHODS or method not in METHODS[service] or not isinstance(params, dict):
            raise ContractError('Метод API не разрешён адаптером.')
        return self._request(service, {'method': method, 'params': params})

    def get(self, service, *, ids=None, campaign_ids=None, group_ids=None):
        if service not in SERVICES or sum(x is not None for x in (ids, campaign_ids, group_ids)) != 1:
            raise ContractError('Требуется одна явная область чтения.')
        values = ids if ids is not None else campaign_ids if campaign_ids is not None else group_ids
        if not isinstance(values, list) or not values or len(values) > 1000:
            raise ContractError('Неверный список объектов.')
        for value in values: positive_id(value)
        criterion = 'Ids' if ids is not None else 'CampaignIds' if campaign_ids is not None else 'AdGroupIds'
        params = {'SelectionCriteria': {criterion: values}, 'FieldNames': FIELDS[service]}
        if service == 'campaigns':
            params['UnifiedCampaignFieldNames'] = ['BiddingStrategy', 'CounterIds', 'AttributionModel','Settings']
            params['UnifiedCampaignSearchStrategyPlacementTypesFieldNames']=['SearchResults','ProductGallery','DynamicPlaces','Maps','SearchOrganizationList']
        if service == 'keywords':
            params['AutotargetingSettingsCategoriesFieldNames'] = ['Exact','Narrow','Alternative','Accessory','Broader']
            params['AutotargetingSettingsBrandOptionsFieldNames'] = ['WithoutBrands','WithAdvertiserBrand','WithCompetitorsBrand']
        if service == 'ads': params['ResponsiveAdFieldNames'] = ['Titles', 'Texts', 'Href']
        rows, offset = [], 0
        for _ in range(100):
            result = self.call(service, 'get', params | {'Page': {'Limit': 1000, 'Offset': offset}})
            items = result.get(SERVICES[service])
            if not isinstance(items, list): raise DirectFailure()
            rows.extend(items)
            following = result.get('LimitedBy')
            if following is None: break
            if type(following) is not int or following <= offset: raise DirectFailure()
            offset = following
        else: raise DirectFailure()
        found = []
        for row in rows:
            if not isinstance(row, dict): raise DirectFailure()
            found.append(positive_id(row.get('Id')))
            if criterion == 'Ids' and row['Id'] not in values: raise DirectFailure()
            if criterion != 'Ids' and row.get('CampaignId' if campaign_ids is not None else 'AdGroupId') not in values:
                raise DirectFailure()
        if len(found) != len(set(found)): raise DirectFailure()
        return rows

    def one(self, service, object_id):
        rows = self.get(service, ids=[positive_id(object_id)])
        if len(rows) != 1: raise DirectFailure('NOT_FOUND')
        return rows[0]

    def mutate(self, service, method, params):
        """Exactly one object per request; executor must journal before calling."""
        if method == 'get': raise ContractError('Это не write-операция.')
        key = 'SelectionCriteria' if method in {'suspend', 'resume', 'moderate'} else (
              'KeywordBids' if service == 'keywordbids' else SERVICES.get(service))
        values = params.get(key) if isinstance(params, dict) else None
        if key == 'SelectionCriteria': values = values.get('Ids') if isinstance(values, dict) else None
        if not isinstance(values, list) or len(values) != 1: raise ContractError('Исполнитель пишет один объект за шаг.')
        result = self.call(service, method, params)
        rows = result.get(method.capitalize() + 'Results')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict): raise DirectFailure()
        row = rows[0]
        errors = row.get('Errors', [])
        if errors:
            code = errors[0].get('Code') if isinstance(errors, list) and isinstance(errors[0], dict) else None
            raise DirectFailure('REJECTED', code)
        object_id = row.get('Id', row.get('KeywordId'))
        try: positive_id(object_id)
        except ContractError: raise DirectFailure() from None
        # Warnings are not permission to accept a normalized/unexpected result.
        return object_id

    def currency(self):
        result = self.call('clients', 'get', {'FieldNames': ['Login', 'Currency']})
        clients = result.get('Clients')
        if not isinstance(clients, list) or len(clients) != 1 or clients[0].get('Login') != self.client_login:
            raise DirectFailure()
        return clients[0].get('Currency')

    def performance(self, campaign_ids, start, end, *, queries=False, goal=None):
        """Bounded factual detail for Codex; conversions come from goal-specific collect."""
        from ..analytics import period
        from datetime import date
        period(start,end)
        if (date.fromisoformat(end)-date.fromisoformat(start)).days>30:raise ContractError('Детальный отчёт ограничен 31 днём.')
        for value in campaign_ids:positive_id(value)
        if goal is not None and (not isinstance(goal,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',goal)):raise ContractError('Нужен точный ID цели.')
        fields=['Date','CampaignId','AdGroupId','CriterionId','Criterion']+(['Query'] if queries else [])+['Impressions','Clicks','Cost']
        dimensions=fields[:-3]
        if goal:fields=fields+['Conversions']
        spec={'SelectionCriteria':{'DateFrom':start,'DateTo':end,'Filter':[{'Field':'CampaignId','Operator':'IN','Values':list(map(str,campaign_ids))}]},
              'FieldNames':fields,'ReportName':'detail-'+digest([campaign_ids,start,end,queries,goal])[:20],
              'ReportType':'SEARCH_QUERY_PERFORMANCE_REPORT' if queries else 'CRITERIA_PERFORMANCE_REPORT',
              'DateRangeType':'CUSTOM_DATE','Format':'TSV','IncludeVAT':'YES','IncludeDiscount':'NO'}
        conversion='Conversions_'+goal+'_AUTO' if goal else None
        if goal:spec.update(Goals=[goal],AttributionModels=['AUTO'])
        output_fields=[conversion if f=='Conversions' else f for f in fields]
        if not campaign_ids:return {'fields':output_fields,'rows':[],'cost_units':'micros','include_vat':True,'goal_id':goal,'attribution':'AUTO' if goal else None}
        raw=self._request('reports',{'params':spec},report=True)
        reader=csv.DictReader(io.StringIO(raw),delimiter='\t');rows=[];seen=set()
        if reader.fieldnames!=output_fields:raise DirectFailure()
        for row in reader:
            if len(rows)>=50000 or set(row)!=set(output_fields) or None in row.values():raise DirectFailure()
            if row['CampaignId'] not in set(map(str,campaign_ids)) or not start<=row['Date']<=end:raise DirectFailure()
            try:date.fromisoformat(row['Date'])
            except ValueError:raise DirectFailure() from None
            key=tuple(row[k] for k in dimensions)
            if key in seen:raise DirectFailure()
            seen.add(key)
            for key in ('Impressions','Clicks','Cost'):
                if not re.fullmatch(r'[0-9]{1,20}',row[key]):raise DirectFailure()
                row[key]=int(row[key])
            if conversion:
                value=row[conversion]
                if value=='--':row[conversion]=None
                elif not re.fullmatch(r'[0-9]{1,20}(?:\.[0-9]{1,8})?',value):raise DirectFailure()
            # Criterion IDs may be '--' for platform targeting; never invent a write ID.
            rows.append(row)
        return {'fields':output_fields,'rows':rows,'cost_units':'micros','include_vat':True,'period':{'start':start,'end':end},'goal_id':goal,'attribution':'AUTO' if goal else None,
                'report_type':spec['ReportType'],'query_coverage':'platform_report_only' if queries else None}

    def spend(self, campaign_ids, start, end):
        if not campaign_ids: return 0
        from ..analytics import period
        period(start, end)
        for value in campaign_ids: positive_id(value)
        spec = {'SelectionCriteria': {'DateFrom': start, 'DateTo': end,
                'Filter': [{'Field': 'CampaignId', 'Operator': 'IN', 'Values': list(map(str, campaign_ids))}]},
                'FieldNames': ['Date', 'CampaignId', 'Cost'], 'ReportName': 'guard-' + digest([campaign_ids, start, end])[:20],
                'ReportType': 'CAMPAIGN_PERFORMANCE_REPORT', 'DateRangeType': 'CUSTOM_DATE',
                'Format': 'TSV', 'IncludeVAT': 'YES', 'IncludeDiscount': 'NO'}
        raw = self._request('reports', {'params': spec}, report=True)
        reader = csv.DictReader(io.StringIO(raw), delimiter='\t')
        if reader.fieldnames != ['Date', 'CampaignId', 'Cost']: raise DirectFailure()
        total, seen = 0, set()
        for row in reader:
            if set(row) != {'Date', 'CampaignId', 'Cost'} or None in row.values(): raise DirectFailure()
            key = (row['Date'], row['CampaignId'])
            if key in seen or row['CampaignId'] not in set(map(str, campaign_ids)) or not start <= row['Date'] <= end:
                raise DirectFailure()
            if not re.fullmatch(r'[0-9]{1,20}', row['Cost']): raise DirectFailure()
            seen.add(key);total += int(row['Cost'])
        return total
