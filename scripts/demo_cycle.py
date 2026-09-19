"""Offline synthetic cycle in a temporary workspace; no accounts or network."""
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from directologist.analytics import bundle, save_bundle, summarize
from directologist.contracts import canonical, digest, load_project
from directologist.decisions import knowledge, recover, save
from directologist.execution import Executor, SimulationAPI
from directologist.learning import Learning
from directologist.onboarding import create
from directologist import policy


def demo():
    with tempfile.TemporaryDirectory(prefix='directologist-demo-') as folder:
        root = Path(folder).resolve()
        shutil.copytree(Path(__file__).resolve().parents[1] / 'knowledge', root / 'knowledge')
        create(root, 'demo', 'Synthetic demonstration', 'UTC')
        profile_path = root / 'projects/demo/profile.json'
        profile = json.loads(profile_path.read_text())
        # Synthetic binding, never used for network calls or credential lookup.
        profile.update(binding_version=1, bindings={'direct': {'connection_id': 'synthetic',
            'resources': {'client_login': 'synthetic', 'campaign_ids': ['1']}}})
        profile_path.write_text(canonical(profile), encoding='utf-8')
        ctx = load_project(root, 'demo')
        day = datetime.now(timezone.utc).date().isoformat()
        evidence = bundle(ctx, 'direct', [{'date': day, 'campaign_id': '1', 'impressions': '1000',
            'clicks': '30', 'cost': '1200', 'conversions': '3'}], start=day, end=day, goal_id='9',
            attribution='AUTO', scope={'campaign_ids': ['1']}, source_timezone='UTC',
            units={'currency': 'RUB', 'money': 'major', 'vat': 'excluded', 'discount': 'excluded'})
        key = save_bundle(ctx, evidence)['evidence_id']
        analysis = summarize(evidence)
        decision = save(ctx, {'schema_version': 1, 'project_id': 'demo', 'context_hash': ctx.context_hash,
            'knowledge_version': knowledge(ctx)['version'], 'methods': ['diagnosis'], 'status': 'OBSERVE',
            'evidence_ids': [key], 'facts': [{'text': 'Синтетический расход: 1200 RUB', 'evidence_id': key}],
            'hypotheses': ['Рекламные события требуют сверки с CRM'],
            'alternatives': ['Наблюдать', 'Проверить качество цели и CRM-связь'],
            'uncertainty': ['Нет сведений о квалифицированных лидах'],
            'expected_outcome': 'Показать сохранение проверяемого предложения',
            'evaluation_window': 'Только локальная демонстрация', 'next_step': 'Сверка данных до реального решения'})
        now = datetime.now(timezone.utc)
        before = {'name': 'Synthetic campaign', 'state': 'ON', 'daily_budget_micros': 1000000}
        plan = {'schema_version': 1, 'project_id': 'demo', 'context_hash': ctx.context_hash,
            'policy_version': 'demo-v1', 'expires_at': (now + timedelta(minutes=10)).isoformat(),
            'operations': [{'id': 'pause', 'capability': 'campaign.pause', 'object_id': '1',
                'before': before, 'after': before | {'state': 'OFF'},
                'must_not_change': {'name': before['name'], 'daily_budget_micros': 1000000}, 'depends_on': []}]}
        plan['sha256'] = digest(plan)
        with Executor(ctx, SimulationAPI({'1': before})) as executor:
            policy.register(executor.store, {'schema_version': 1, 'mode': 'SIMULATION', 'project_id': 'demo',
                'context_hash': ctx.context_hash, 'version': 'demo-v1', 'resources': ['1'],
                'capabilities': ['campaign.pause'], 'max_reserved_micros': 1000000, 'max_operations': 1,
                'valid_from': (now - timedelta(seconds=1)).isoformat(),
                'expires_at': (now + timedelta(minutes=10)).isoformat(), 'approval_source': 'Synthetic demo only'})
            execution = executor.execute(plan)
            assert execution['status'] == 'CONFIRMED'
            assert executor.execute(plan) == execution
            assert executor.api.calls == ['pause']
        with Learning(ctx) as learning:
            baseline = learning.head()
            candidate = learning.propose({'schema_version': 1, 'project_id': 'demo', 'context_hash': ctx.context_hash,
                'base_version': baseline, 'methods_version': knowledge(ctx)['version'], 'scope': 'project',
                'domain': 'demand', 'rule': 'Синтетический урок: проверять неоднозначный интент',
                'applicability': 'Учебный пример', 'exceptions': 'Не использовать как доказанный бизнес-эффект',
                'sources': ['https://example.org/synthetic'], 'evidence_ids': [], 'training_case_ids': ['train-1'],
                'claim': 'method', 'outcome_evidence_id': None, 'conflicts': []})['candidate_id']
            criteria = {'version': 'demo-rubric', 'approval_source': 'Synthetic demonstration only',
                'min_improvement': 1, 'sources_verified': True, 'outcome_verified': False,
                'cases': [{'id': 'holdout-1', 'input': 'Неоднозначный запрос', 'expected': 'review', 'critical': True},
                          {'id': 'holdout-2', 'input': 'Целевой запрос', 'expected': 'keep', 'critical': False}]}
            def evaluator(base, candidate, case):
                assert 'expected' not in case
                return {'baseline': 'keep', 'candidate': 'review' if case['id'] == 'holdout-1' else 'keep'}
            lesson = learning.evaluate(candidate, criteria, evaluator)
            assert lesson['status'] == 'ACTIVE'
            learning.rollback(baseline, learning.head())
            assert learning.status(candidate)['status'] == 'ROLLED_BACK'
        state = recover(ctx)
        assert len(state['decisions']) == 1
        assert analysis['ratios']['cpc'] == '40.000000'
        return {'status': 'PASS', 'data': 'SYNTHETIC', 'network_calls': 0,
                'analysis': analysis['ratios'], 'decision_saved': bool(decision),
                'simulation': execution['status'], 'repeated_plan_calls': 1,
                'lesson': lesson['status'], 'rollback': 'ROLLED_BACK', 'recovered_decisions': len(state['decisions']),
                'autonomous_writes': False,
                'note': 'Проверка механизма на заранее заданных ответах; качество ИИ и эффект рекламы не измерены.'}


if __name__ == '__main__':
    print(json.dumps(demo(), ensure_ascii=False, indent=2))
