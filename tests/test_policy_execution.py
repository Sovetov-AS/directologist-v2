import copy
import unittest
from datetime import datetime,timedelta,timezone
from unittest.mock import Mock
from analysis_fixtures import setup
from directologist.contracts import ContractError,digest
from directologist.planning import validate,CAPABILITIES
from directologist.execution import Executor,SimulationAPI
from directologist import policy

BEFORE={'name':'Synthetic','state':'ON','daily_budget_micros':1000000}

def grant(ctx,**kw):
    now=datetime.now(timezone.utc)
    return dict(schema_version=1,mode='SIMULATION',project_id=ctx.project_id,context_hash=ctx.context_hash,
        version='v1',resources=['1','2','draft'],capabilities=sorted(CAPABILITIES),max_reserved_micros=10000000,
        max_operations=10,valid_from=(now-timedelta(minutes=1)).isoformat(),expires_at=(now+timedelta(hours=1)).isoformat(),approval_source='Synthetic test only')|kw

def plan(ctx,ops=None,**kw):
    op=dict(id='pause',capability='campaign.pause',object_id='1',before=BEFORE,after=BEFORE|{'state':'OFF'},must_not_change={'name':'Synthetic','daily_budget_micros':1000000},depends_on=[])
    p=dict(schema_version=1,project_id=ctx.project_id,context_hash=ctx.context_hash,policy_version='v1',expires_at=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(),operations=ops or [op])|kw
    p['sha256']=digest(p);return p

class ExecutionTests(unittest.TestCase):
    def setUp(self):
        setup(self);self.api=SimulationAPI({'1':BEFORE,'2':BEFORE});self.exe=Executor(self.ctx,self.api);self.addCleanup(self.exe.__exit__)
        policy.register(self.exe.store,grant(self.ctx))
    def test_intent_before_send_and_reread(self):
        base=self.api.apply
        def apply(op):
            self.assertEqual(self.exe.store.connection.execute('SELECT status FROM execution_steps').fetchone()[0],'STARTED')
            base(op)
        self.api.apply=apply
        p=plan(self.ctx)
        self.assertEqual(self.exe.execute(p)['status'],'CONFIRMED')
        self.exe.execute(p);self.assertEqual(self.api.calls,['pause'])
    def test_expired_revoked_and_foreign_rejected(self):
        with self.assertRaises(ContractError):self.exe.execute(plan(self.ctx,expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()))
        p=plan(self.ctx);p['operations'][0]['object_id']='outside';p['sha256']=digest({k:v for k,v in p.items() if k!='sha256'})
        with self.assertRaises(ContractError):self.exe.execute(p)
        policy.revoke(self.exe.store,'v1')
        with self.assertRaises(ContractError):self.exe.execute(plan(self.ctx))
        self.assertEqual(self.api.calls,[])
    def test_changed_before_and_forbidden_field(self):
        self.api.objects['1']['name']='Changed externally'
        self.assertEqual(self.exe.execute(plan(self.ctx))['status'],'REJECTED');self.assertFalse(self.api.calls)
        p=plan(self.ctx);p['operations'][0]['after']['name']='Different';p['sha256']=digest({k:v for k,v in p.items() if k!='sha256'})
        with self.assertRaises(ContractError):validate(self.ctx,p)
    def test_crash_after_send_no_blind_retry_and_explicit_reconcile(self):
        original=self.api.apply
        def crash(op):original(op);raise KeyboardInterrupt()
        self.api.apply=crash;p=plan(self.ctx)
        with self.assertRaises(KeyboardInterrupt):self.exe.execute(p)
        self.api.apply=original
        with Executor(self.ctx,self.api) as reopened:
            self.assertEqual(reopened.execute(p)['status'],'STARTED')
            self.assertEqual(reopened.reconcile(p['sha256'])['status'],'CONFIRMED')
        self.assertEqual(len(self.api.calls),1)
    def test_partial_postcondition_stays_unknown_until_resolved(self):
        self.api.apply=lambda op:self.api.objects['1'].update(name='unplanned')
        p=plan(self.ctx)
        self.assertEqual(self.exe.execute(p)['status'],'PARTIAL')
        self.assertEqual(self.exe.reconcile(p['sha256'])['status'],'UNKNOWN')
        with self.assertRaises(ContractError):self.exe.execute(plan(self.ctx))
    def test_crash_before_send_stays_unresolved(self):
        self.api.apply=lambda op:(_ for _ in ()).throw(KeyboardInterrupt())
        p=plan(self.ctx)
        with self.assertRaises(KeyboardInterrupt):self.exe.execute(p)
        self.assertEqual(self.exe.reconcile(p['sha256'])['status'],'UNKNOWN')
        self.assertEqual(self.api.calls,[])
    def test_revoke_between_steps_blocks_second(self):
        first=plan(self.ctx)['operations'][0];second=copy.deepcopy(first);second.update(id='pause-two',object_id='2',depends_on=['pause'])
        original=self.api.apply
        def apply(op):original(op);policy.revoke(self.exe.store,'v1')
        self.api.apply=apply
        result=self.exe.execute(plan(self.ctx,[first,second]))
        self.assertEqual(result['status'],'UNKNOWN');self.assertEqual(self.api.calls,['pause'])
    def test_aggregate_operation_limit_survives_new_executor(self):
        policy.register(self.exe.store,grant(self.ctx,version='limited',max_operations=1))
        p=plan(self.ctx,policy_version='limited');self.exe.execute(p)
        op=copy.deepcopy(p['operations'][0]);op.update(id='second',object_id='2')
        with Executor(self.ctx,self.api) as reopened,self.assertRaises(ContractError):reopened.execute(plan(self.ctx,[op],policy_version='limited'))
    def test_cumulative_financial_reservation(self):
        policy.register(self.exe.store,grant(self.ctx,version='limited',max_reserved_micros=3000000))
        op=plan(self.ctx)['operations'][0];op.update(capability='campaign.budget',after=BEFORE|{'daily_budget_micros':2000000},must_not_change={'state':'ON','name':'Synthetic'})
        self.exe.execute(plan(self.ctx,[op],policy_version='limited'))
        op=copy.deepcopy(op);op.update(id='second',object_id='2')
        with self.assertRaises(ContractError):self.exe.execute(plan(self.ctx,[op],policy_version='limited'))
    def test_off_draft_and_launch_are_distinct(self):
        op=dict(id='create',capability='campaign.create_off',object_id='draft',before=None,after=BEFORE|{'state':'OFF'},must_not_change={},depends_on=[])
        self.assertEqual(self.exe.execute(plan(self.ctx,[op]))['status'],'CONFIRMED')
        op['after']['state']='ON'
        with self.assertRaises(ContractError):validate(self.ctx,plan(self.ctx,[op]))
    def test_live_boundary_and_restore_block(self):
        with self.assertRaises(ContractError):Executor(self.ctx,Mock())
        with self.assertRaises(ContractError):policy.register(self.exe.store,grant(self.ctx,mode='LIVE'))
        with self.exe.store.transaction():self.exe.store.connection.execute("UPDATE metadata SET value='1' WHERE key='recovery_required'")
        with self.assertRaises(ContractError):self.exe.execute(plan(self.ctx))
        self.assertFalse(policy.capabilities()['live_write_enabled'])
    def test_changed_before_after_confirmed_step_blocks_new_plan(self):
        first=plan(self.ctx)['operations'][0]
        second=copy.deepcopy(first);second.update(id='pause-two',object_id='2',depends_on=['pause'])
        self.api.objects['2']['name']='Changed externally'
        result=self.exe.execute(plan(self.ctx,[first,second]))
        self.assertEqual(result['status'],'PARTIAL')
        self.assertEqual(self.api.calls,['pause'])
        with self.assertRaises(ContractError):self.exe.execute(plan(self.ctx))
    def test_concurrent_executor_cannot_acquire_lock(self):
        from directologist.setup import setup_lock
        with setup_lock(self.ctx),self.assertRaises(ContractError):self.exe.execute(plan(self.ctx))
