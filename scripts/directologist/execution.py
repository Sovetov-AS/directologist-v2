"""Durable intent before a simulated external step; no live API backend is exposed."""
import copy
import json
from .contracts import ContractError,canonical
from .storage import Store,now
from .planning import validate,exposure
from . import policy
from .setup import setup_lock

class SimulationAPI:
    def __init__(self,objects):self.objects={key:copy.deepcopy(value) for key,value in objects.items()};self.calls=[]
    def read(self,op):return copy.deepcopy(self.objects.get(op['object_id']))
    def apply(self,op):
        self.calls.append(op['id'])
        self.objects[op['object_id']]=copy.deepcopy(op['after'])

class Executor:
    def __init__(self,context,api):
        if not isinstance(api,SimulationAPI):raise ContractError('Live backend не включён; требуется независимая приёмка.')
        self.context,self.api=context,api
        self.store=Store(context,create=True)
        with self.store.transaction():
            policy.initialize(self.store)
            self.store.connection.execute('CREATE TABLE IF NOT EXISTS executions(plan_hash TEXT PRIMARY KEY,policy_version TEXT,data TEXT,status TEXT,reserved_micros INTEGER,operation_count INTEGER,created_at TEXT)')
            self.store.connection.execute('CREATE TABLE IF NOT EXISTS execution_steps(plan_hash TEXT,step_id TEXT,status TEXT,observed TEXT,PRIMARY KEY(plan_hash,step_id))')
    def __enter__(self):return self
    def __exit__(self,*_):self.store.__exit__()
    def status(self,key):
        row=self.store.connection.execute('SELECT status FROM executions WHERE plan_hash=?',(key,)).fetchone()
        if not row:raise ContractError('План исполнения не найден.')
        steps=[dict(r) for r in self.store.connection.execute('SELECT step_id,status FROM execution_steps WHERE plan_hash=? ORDER BY rowid',(key,))]
        return {'plan_hash':key,'status':row[0],'steps':steps,'mode':'SIMULATION','autonomous_writes':False}
    def _set(self,key,status,step=None,observed=None):
        with self.store.transaction():
            self.store.connection.execute('UPDATE executions SET status=? WHERE plan_hash=?',(status,key))
            if step:self.store.connection.execute('UPDATE execution_steps SET status=?,observed=? WHERE plan_hash=? AND step_id=?',(status,canonical(observed),key,step))
    def execute(self,plan):
        validate(self.context,plan);key=plan['sha256']
        with setup_lock(self.context):
            with self.store.transaction():
                existing=self.store.connection.execute('SELECT status FROM executions WHERE plan_hash=?',(key,)).fetchone()
                if existing:return self.status(key) # never blindly resume any recorded plan
                if self.store.connection.execute("SELECT 1 FROM executions WHERE status IN ('STARTED','UNKNOWN','PARTIAL')").fetchone():
                    raise ContractError('Сначала сверить незавершённые действия проекта.')
                policy.check(self.store,plan)
                self.store.connection.execute('INSERT INTO executions VALUES (?,?,?,?,?,?,?)',(key,plan['policy_version'],canonical(plan),'PREPARED',exposure(plan),len(plan['operations']),now()))
                self.store.connection.executemany('INSERT INTO execution_steps VALUES (?,?,?,?)',[(key,op['id'],'PENDING','null') for op in plan['operations']])
            for op in plan['operations']:
                try:
                    with self.store.transaction():policy.check(self.store,plan)
                    if self.api.read(op)!=op['before']:
                        self._set(key,'REJECTED',op['id'])
                        # Earlier confirmed steps mean the plan was partially applied.
                        # Preserve the unresolved gate until explicit reconciliation.
                        if self.store.connection.execute("SELECT 1 FROM execution_steps WHERE plan_hash=? AND status='CONFIRMED'",(key,)).fetchone():
                            self._set(key,'PARTIAL')
                        return self.status(key)
                    with self.store.transaction():
                        validate(self.context,plan);policy.check(self.store,plan)
                        self.store.connection.execute("UPDATE executions SET status='STARTED' WHERE plan_hash=?",(key,))
                        self.store.connection.execute("UPDATE execution_steps SET status='STARTED' WHERE plan_hash=? AND step_id=?",(key,op['id']))
                    self.api.apply(op)
                    observed=self.api.read(op)
                    if observed!=op['after']:
                        self._set(key,'PARTIAL',op['id']);return self.status(key)
                    # Store only the reviewed expected snapshot; raw provider responses never persisted.
                    with self.store.transaction():
                        self.store.connection.execute("UPDATE execution_steps SET status='CONFIRMED',observed=? WHERE plan_hash=? AND step_id=?",(canonical(op['after']),key,op['id']))
                except Exception:
                    self._set(key,'UNKNOWN',op['id']);return self.status(key)
            self._set(key,'CONFIRMED')
            return self.status(key)
    def reconcile(self,key):
        """Explicit read-only reconciliation. No missing step is sent again."""
        with setup_lock(self.context):
            row=self.store.connection.execute('SELECT data FROM executions WHERE plan_hash=?',(key,)).fetchone()
            if not row:raise ContractError('Неизвестный план.')
            plan=json.loads(row[0])
            for op in plan['operations']:
                status=self.store.connection.execute('SELECT status FROM execution_steps WHERE plan_hash=? AND step_id=?',(key,op['id'])).fetchone()[0]
                if status in {'PENDING','REJECTED'}:continue
                try:matched=self.api.read(op)==op['after']
                except Exception:matched=False
                with self.store.transaction():
                    self.store.connection.execute('UPDATE execution_steps SET status=? WHERE plan_hash=? AND step_id=?',('CONFIRMED' if matched else 'UNKNOWN',key,op['id']))
            states=[r[0] for r in self.store.connection.execute('SELECT status FROM execution_steps WHERE plan_hash=?',(key,))]
            outcome='CONFIRMED' if all(s=='CONFIRMED' for s in states) else 'UNKNOWN'
            self._set(key,outcome)
            return self.status(key)
