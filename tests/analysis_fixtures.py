"""Synthetic, temporary project shared by analytics tests; never real accounts."""
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from directologist.contracts import load_project, digest
from directologist.analytics import bundle

START, END = '2026-09-01', '2026-09-02'
COHORT = 'lead_or_deal_created_in_period_with_current_entity_state'

def setup(test):
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    test.root = Path(temp.name).resolve()
    test.ctx = project(test.root)
    blocker = patch('socket.socket', side_effect=AssertionError('Network forbidden'))
    blocker.start()
    test.addCleanup(blocker.stop)

def project(root, name='fixture'):
    folder = root / 'projects' / name
    folder.mkdir(parents=True)
    profile = dict(schema_version=1, project_id=name, display_name='Synthetic', timezone='Europe/Moscow', binding_version=1,
                   bindings={key: {'connection_id': 'synthetic', 'resources': resources} for key, resources in {
                       'direct': {'client_login': 'fixture', 'campaign_ids': ['1']},
                       'metrika': {'counter_id': '2', 'goal_ids': ['9']},
                       'crm': {'bridge_id': 'fixture'},
                       'wordstat': {'folder_id': 'fixture', 'region_ids': ['225']}}.items()})
    (folder / 'profile.json').write_text(json.dumps(profile))
    return load_project(root, name)

def rehash(data):
    data['sha256'] = digest({k: v for k, v in data.items() if k != 'sha256'})
    return data

def direct(ctx, rows=None, **options):
    defaults = dict(start=START, end=END, goal_id='9', attribution='AUTO', scope={'campaign_ids': ['1']},
                    units={'currency': 'RUB', 'money': 'major', 'vat': 'excluded', 'discount': 'excluded'})
    defaults.update(options)
    return bundle(ctx, 'direct', rows if rows is not None else [row()], **defaults)

def row(day=START, **metrics):
    return dict(date=day, campaign_id='1', **({'impressions': '10', 'clicks': '2', 'cost': '0.1', 'conversions': '1'} | metrics))

def crm_raw():
    return {'meta': {'readOnly': True, 'dateFrom': START, 'dateTo': END, 'cohort': COHORT},
            'summary': {'directLeads': 3, 'qualifiedDirectLeads': None, 'wonProjectSalesDeals': 1,
                        'wonRevenueByCurrency': {'RUB': '123.45', 'USD': '2.10'}, 'wonMarginByCurrency': {'RUB': '-10.25'}},
            'dataQuality': {'missingConfiguration': ['qualification'], 'leadsNotMatchedToDirect': 4, 'directDealsWithoutLeadLink': 1},
            'leads': [{'name': 'PRIVATE_SENTINEL', 'phone': 'PRIVATE_SENTINEL', 'email': 'PRIVATE_SENTINEL'}]}
