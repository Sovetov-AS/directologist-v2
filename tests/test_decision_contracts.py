import copy
import json
import shutil
import unittest
from pathlib import Path
from analysis_fixtures import setup,direct
from directologist.analytics import save_bundle
from directologist.contracts import ContractError
from directologist.decisions import knowledge,validate,save,recover

class DecisionTests(unittest.TestCase):
    def setUp(self):
        setup(self)
        shutil.copytree(Path(__file__).resolve().parents[1]/'knowledge',self.root/'knowledge')
        self.data=dict(schema_version=1,project_id='fixture',context_hash=self.ctx.context_hash,
            knowledge_version=knowledge(self.ctx)['version'],methods=['diagnosis'],status='INSUFFICIENT_DATA',
            evidence_ids=[],facts=[],hypotheses=['Проверить цель'],alternatives=['Наблюдать','Сверить CRM'],
            uncertainty=['Нет CRM'],expected_outcome='Уточнить данные',evaluation_window='До согласованного срока',next_step='Получить отчёт')
    def test_save_recover_and_idempotency(self):
        first=save(self.ctx,self.data);self.assertEqual(first,save(self.ctx,self.data))
        self.assertEqual(len(recover(self.ctx)['decisions']),1)
        self.assertFalse(first['autonomous_writes'])
    def test_evidence_required_and_stale_knowledge_rejected(self):
        self.data['status']='PROPOSED'
        with self.assertRaises(ContractError):validate(self.ctx,self.data)
        self.data['evidence_ids']=[save_bundle(self.ctx,direct(self.ctx))['evidence_id']]
        validate(self.ctx,self.data)
        p=self.root/'knowledge/methods/ads.md';p.write_text(p.read_text(encoding='utf-8')+'\nNew version\n',encoding='utf-8')
        with self.assertRaises(ContractError):validate(self.ctx,self.data)
    def test_fact_must_reference_evidence(self):
        self.data['facts']=[{'text':'CPL 10','evidence_id':'0'*64}]
        with self.assertRaises(ContractError):validate(self.ctx,self.data)
    def test_no_permissions_or_hidden_operations_in_proposal(self):
        for key,value in [('status','EXECUTE'),('grant',True),('budget','50000'),('context_hash','0'*64)]:
            data=self.data|{key:value}
            with self.subTest(key=key),self.assertRaises(ContractError):validate(self.ctx,data)
    def test_alternatives_and_uncertainty_required(self):
        for field in ('alternatives','uncertainty','hypotheses'):
            with self.subTest(field=field),self.assertRaises(ContractError):validate(self.ctx,self.data|{field:[]})

class OnboardingTests(unittest.TestCase):
    def setUp(self): setup(self)
    def test_new_user_project_has_no_accounts_and_never_overwrites(self):
        from directologist.onboarding import create
        from directologist.contracts import load_project
        create(self.root,'new-client','Новый клиент','UTC')
        profile=load_project(self.root,'new-client').profile
        self.assertEqual(profile['bindings'],{})
        self.assertEqual(profile['binding_version'],0)
        with self.assertRaises(ContractError):create(self.root,'new-client','Другое имя','UTC')
        self.assertEqual(load_project(self.root,'new-client').profile,profile)
