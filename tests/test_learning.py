import copy
import shutil
import time
import unittest
from pathlib import Path
from analysis_fixtures import setup
from directologist.contracts import ContractError
from directologist.decisions import knowledge
from directologist.learning import Learning
from directologist.model_tasks import run,StubA,StubB

class Slow:
    def __call__(self,task):time.sleep(5)
class Bad:
    def __call__(self,task):return {'status':'ok','label':'LAUNCH','token':'SECRET_SENTINEL'}

class LearningTests(unittest.TestCase):
    def setUp(self):
        setup(self);shutil.copytree(Path(__file__).resolve().parents[1]/'knowledge',self.root/'knowledge')
        self.learn=Learning(self.ctx);self.addCleanup(self.learn.__exit__)
        self.data=dict(schema_version=1,project_id='fixture',context_hash=self.ctx.context_hash,base_version=self.learn.head(),
            methods_version=knowledge(self.ctx)['version'],scope='project',domain='demand',rule='Проверять неоднозначный интент',
            applicability='Неоднозначные запросы',exceptions='Подтверждённый иной интент',sources=['https://example.org/method'],
            evidence_ids=[],training_case_ids=['training-1'],claim='method',outcome_evidence_id=None,conflicts=[])
        self.criteria=dict(version='test-v1',approval_source='Synthetic rubric',min_improvement=1,sources_verified=True,outcome_verified=False,
            cases=[dict(id='holdout-1',input='Конфликт запроса',expected='review',critical=True),dict(id='holdout-2',input='Целевой запрос',expected='keep',critical=False)])
    def good(self,base,candidate,case):
        self.assertNotIn('expected',case)
        return {'baseline':'keep','candidate':'review' if case['id']=='holdout-1' else 'keep'}
    def propose(self):return self.learn.propose(self.data)['candidate_id']
    def test_missing_criteria_sources_and_outcomes_do_not_activate(self):
        key=self.propose();self.assertEqual(self.learn.evaluate(key,None,self.good)['status'],'INSUFFICIENT_DATA')
        self.assertEqual(self.learn.evaluate(key,self.criteria|{'sources_verified':False},self.good)['status'],'INSUFFICIENT_DATA')
        self.data['claim']='business_effect';key=self.propose()
        self.assertEqual(self.learn.evaluate(key,self.criteria,self.good)['status'],'INSUFFICIENT_DATA')
    def test_passing_holdouts_auto_activate_and_recovery(self):
        key=self.propose();before=knowledge(self.ctx)['version']
        result=self.learn.evaluate(key,self.criteria,self.good)
        self.assertEqual(result['status'],'ACTIVE');self.assertNotEqual(before,knowledge(self.ctx)['version'])
        self.assertEqual(self.learn.evaluate(key,self.criteria,self.good),result)
        with Learning(self.ctx) as recovered:self.assertEqual(len(recovered.cards()),1)
    def test_regression_or_conflict_rejected(self):
        key=self.propose()
        bad=lambda *args:{'baseline':'review','candidate':'launch'}
        self.assertEqual(self.learn.evaluate(key,self.criteria,bad)['status'],'REJECTED_REGRESSION')
        self.data['conflicts']=['Противоречит другому уроку'];key=self.propose()
        self.assertEqual(self.learn.evaluate(key,self.criteria,self.good)['status'],'OWNER_REQUIRED')
    def test_training_holdout_overlap_and_no_gain_rejected(self):
        key=self.propose();criteria=copy.deepcopy(self.criteria);criteria['cases'][0]['id']='training-1'
        with self.assertRaises(ContractError):self.learn.evaluate(key,criteria,self.good)
        equal=lambda base,candidate,case:{'baseline':'review' if case['id']=='holdout-1' else 'keep','candidate':'review' if case['id']=='holdout-1' else 'keep'}
        self.assertEqual(self.learn.evaluate(key,self.criteria,equal)['status'],'INSUFFICIENT_IMPROVEMENT')
    def test_racing_base_requires_reevaluation(self):
        first=self.propose();self.data['rule']='Другой кандидат';second=self.propose()
        self.learn.evaluate(first,self.criteria,self.good)
        self.assertEqual(self.learn.evaluate(second,self.criteria,self.good)['status'],'STALE_BASE')
    def test_base_changes_during_evaluation_cas_fails(self):
        key=self.propose();self.data['rule']='Другой кандидат';second=self.propose();called=False
        def race(base,candidate,case):
            nonlocal called
            if not called:called=True;self.learn.evaluate(second,self.criteria,self.good)
            return self.good(base,candidate,case)
        self.assertEqual(self.learn.evaluate(key,self.criteria,race)['status'],'STALE_BASE')
    def test_rollback_history_and_no_code_or_policy_edit(self):
        original={str(p):p.read_bytes() for p in (self.root/'knowledge').rglob('*') if p.is_file()}
        baseline=self.learn.head();key=self.propose();self.learn.evaluate(key,self.criteria,self.good)
        head=self.learn.head();self.learn.rollback(baseline,head)
        self.assertEqual(self.learn.cards(),[]);self.assertEqual(self.learn.status(key)['status'],'ROLLED_BACK')
        self.assertEqual(original,{str(p):p.read_bytes() for p in (self.root/'knowledge').rglob('*') if p.is_file()})
        with self.assertRaises(ContractError):self.learn.rollback(baseline,head)
    def test_candidate_cannot_add_authority_or_global_scope(self):
        for change in ({'budget':50000},{'scope':'global'},{'domain':'policy'}):
            with self.assertRaises(ContractError):self.learn.propose(self.data|change)
    def test_imported_results_need_approved_policy_and_matching_case_set(self):
        import json
        from directologist.learning import evaluate_imported
        from directologist.contracts import digest
        key=self.propose()
        self.assertEqual(evaluate_imported(self.learn,key,{})["status"],"INSUFFICIENT_DATA")
        config={"schema_version":1,"approved":True,"criteria":self.criteria}
        (self.ctx.directory/"learning-policy.json").write_text(json.dumps(config))
        results={"candidate_id":key,"base_version":self.learn.head(),"criteria_hash":digest(self.criteria),
                 "answers":{"holdout-1":{"baseline":"keep","candidate":"review"},"holdout-2":{"baseline":"keep","candidate":"keep"}}}
        with self.assertRaises(ContractError):evaluate_imported(self.learn,key,results|{"criteria_hash":"wrong"})
        self.assertEqual(evaluate_imported(self.learn,key,results)["status"],"ACTIVE")

    def test_evaluator_exception_is_not_activation(self):
        def fail(*args):raise RuntimeError('SECRET_SENTINEL')
        result=self.learn.evaluate(self.propose(),self.criteria,fail)
        self.assertEqual(result['status'],'EVALUATION_ERROR');self.assertNotIn('SECRET_SENTINEL',str(result))

class ModelTaskTests(unittest.TestCase):
    def task(self,seconds=2):return {'schema_version':1,'project_id':'fixture','kind':'intent-classification','text':'Условный запрос','timeout_seconds':seconds}
    def test_two_stub_adapters_share_contract(self):
        self.assertEqual(run(self.task(),StubA())['label'],'AMBIGUOUS')
        self.assertEqual(run(self.task(),StubB())['label'],'AMBIGUOUS')
    def test_timeout_and_invalid_response_abstain(self):
        self.assertEqual(run(self.task(.05),Slow()),{'status':'abstain','reason':'timeout'})
        result=run(self.task(),Bad());self.assertEqual(result['status'],'abstain');self.assertNotIn('SECRET_SENTINEL',str(result))
    def test_extra_secret_field_rejected(self):
        with self.assertRaises(ContractError):run(self.task()|{'api_key':'SECRET_SENTINEL'},StubA())
