"""Project-scoped candidate -> independent evaluation -> atomic version switch."""
import json
from urllib.parse import urlsplit
from .contracts import ContractError,canonical,digest,identifier
from .decisions import text,knowledge
from .analytics import load_bundle
from .storage import Store,now

class Learning:
    def __init__(self,context):
        self.context=context;self.store=Store(context,create=True)
        with self.store.transaction():
            for sql in (
                'CREATE TABLE IF NOT EXISTS knowledge_versions(id TEXT PRIMARY KEY,parent TEXT,data TEXT,created_at TEXT)',
                'CREATE TABLE IF NOT EXISTS lesson_candidates(id TEXT PRIMARY KEY,data TEXT,status TEXT,evaluation TEXT)',
                'CREATE TABLE IF NOT EXISTS knowledge_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,previous TEXT,current TEXT,reason TEXT,created_at TEXT)'):
                self.store.connection.execute(sql)
            base=digest({'project_id':context.project_id,'cards':[]})
            self.store.connection.execute('INSERT OR IGNORE INTO knowledge_versions VALUES (?,?,?,?)',(base,None,'[]',now()))
            self.store.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('knowledge_head',?)",(base,))
    def __enter__(self):return self
    def __exit__(self,*_):self.store.__exit__()
    def head(self):return self.store.connection.execute("SELECT value FROM metadata WHERE key='knowledge_head'").fetchone()[0]
    def cards(self):return json.loads(self.store.connection.execute('SELECT data FROM knowledge_versions WHERE id=?',(self.head(),)).fetchone()[0])
    def propose(self,candidate):
        fields={'schema_version','project_id','context_hash','base_version','methods_version','scope','domain','rule','applicability','exceptions','sources','evidence_ids','training_case_ids','claim','outcome_evidence_id','conflicts'}
        if not isinstance(candidate,dict) or set(candidate)!=fields or type(candidate['schema_version']) is not int or candidate['schema_version']!=1:raise ContractError('Неподдерживаемый LessonCandidate.')
        if candidate['project_id']!=self.context.project_id or candidate['context_hash']!=self.context.context_hash or candidate['scope']!='project':raise ContractError('Урок не может менять область или проект.')
        if candidate['base_version']!=self.head() or candidate['methods_version']!=knowledge(self.context)['version']:raise ContractError('База знаний изменилась.')
        if candidate['domain'] not in {'diagnosis','demand','ads'} or candidate['claim'] not in {'method','business_effect'}:raise ContractError('Неизвестная область или вид утверждения.')
        for key in ('rule','applicability','exceptions'):text(candidate[key])
        for key in ('sources','evidence_ids','training_case_ids','conflicts'):
            if not isinstance(candidate[key],list) or len(candidate[key])>50:raise ContractError('Некорректные основания урока.')
        for value in candidate['sources']:
            text(value,1000);parsed=urlsplit(value)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:raise ContractError('Нужен публичный источник без credentials.')
        for eid in candidate['evidence_ids']:load_bundle(self.context,eid)
        for cid in candidate['training_case_ids']:identifier(cid)
        for conflict in candidate['conflicts']:text(conflict)
        if candidate['outcome_evidence_id'] is not None:
            if candidate['outcome_evidence_id'] not in candidate['evidence_ids']:raise ContractError('Исход не связан с evidence.')
        key=digest(candidate)
        with self.store.transaction():
            self.store.connection.execute('INSERT OR IGNORE INTO lesson_candidates VALUES (?,?,?,?)',(key,canonical(candidate),'CANDIDATE',None))
        return self.status(key)
    def status(self,key):
        row=self.store.connection.execute('SELECT status,evaluation FROM lesson_candidates WHERE id=?',(key,)).fetchone()
        if not row:raise ContractError('Кандидат не найден.')
        return {'candidate_id':key,'status':row[0],'evaluation':json.loads(row[1]) if row[1] else None,'active_version':self.head(),'autonomous_writes':False}
    def _result(self,key,status,details=None):
        with self.store.transaction():self.store.connection.execute('UPDATE lesson_candidates SET status=?,evaluation=? WHERE id=?',(status,canonical(details),key))
        return self.status(key)
    def evaluate(self,key,criteria,evaluator):
        row=self.store.connection.execute('SELECT data,status FROM lesson_candidates WHERE id=?',(key,)).fetchone()
        if not row:raise ContractError('Кандидат не найден.')
        candidate=json.loads(row[0])
        if row[1]=='ACTIVE':return self.status(key)
        if not criteria:return self._result(key,'INSUFFICIENT_DATA')
        if (not isinstance(criteria,dict) or set(criteria)!={'version','approval_source','cases','min_improvement','sources_verified','outcome_verified'}
            or type(criteria['min_improvement']) is not int or criteria['min_improvement']<1
            or type(criteria['sources_verified']) is not bool or type(criteria['outcome_verified']) is not bool):raise ContractError('Неподдерживаемые критерии проверки.')
        identifier(criteria['version']);text(criteria['approval_source'])
        if candidate['conflicts']:return self._result(key,'OWNER_REQUIRED')
        if not candidate['sources'] or not criteria['sources_verified']:return self._result(key,'INSUFFICIENT_DATA')
        if candidate['claim']=='business_effect' and (not candidate['outcome_evidence_id'] or not criteria['outcome_verified']):return self._result(key,'INSUFFICIENT_DATA')
        for eid in candidate['evidence_ids']:load_bundle(self.context,eid)
        if candidate['base_version']!=self.head() or candidate['methods_version']!=knowledge(self.context)['version']:return self._result(key,'STALE_BASE')
        cases=criteria['cases']
        if not isinstance(cases,list) or len(cases)<2 or len(cases)>100:raise ContractError('Нужен независимый набор контрольных случаев.')
        seen=set();outputs=[]
        for case in cases:
            if not isinstance(case,dict) or set(case)!={'id','input','expected','critical'} or type(case['critical']) is not bool:raise ContractError('Некорректный контрольный случай.')
            identifier(case['id']);text(case['input']);text(case['expected'])
            if case['id'] in seen or case['id'] in candidate['training_case_ids']:raise ContractError('Контрольный случай совпадает с обучающим или дублируется.')
            seen.add(case['id'])
            try:
                result=evaluator(self.cards(),candidate,{"id":case["id"],"input":case["input"]})
                if not isinstance(result,dict) or set(result)!={'baseline','candidate'}:raise ValueError()
                text(result['baseline']);text(result['candidate'])
                outputs.append({'id':case['id'],'baseline_pass':result['baseline']==case['expected'],'candidate_pass':result['candidate']==case['expected'],'critical':case['critical']})
            except Exception:return self._result(key,'EVALUATION_ERROR')
        if not any(c['critical'] for c in cases):raise ContractError('Нет обязательных защитных случаев.')
        before=sum(o['baseline_pass'] for o in outputs);after=sum(o['candidate_pass'] for o in outputs)
        details={'criteria_hash':digest(criteria),'case_results':outputs,'baseline_passed':before,'candidate_passed':after}
        if any((o['baseline_pass'] and not o['candidate_pass']) or (o['critical'] and not o['candidate_pass']) for o in outputs):return self._result(key,'REJECTED_REGRESSION',details)
        if after-before<criteria['min_improvement']:return self._result(key,'INSUFFICIENT_IMPROVEMENT',details)
        # CAS, event and head change in one transaction. Nothing rewrites skills/code/policy.
        with self.store.transaction():
            if candidate['base_version']!=self.head() or candidate['methods_version']!=knowledge(self.context)['version']:
                self.store.connection.execute("UPDATE lesson_candidates SET status='STALE_BASE' WHERE id=?",(key,))
            else:
                cards=self.cards()+[candidate];version=digest(cards);previous=self.head()
                self.store.connection.execute('INSERT OR IGNORE INTO knowledge_versions VALUES (?,?,?,?)',(version,previous,canonical(cards),now()))
                self.store.connection.execute("UPDATE metadata SET value=? WHERE key='knowledge_head'",(version,))
                self.store.connection.execute('INSERT INTO knowledge_events(previous,current,reason,created_at) VALUES (?,?,?,?)',(previous,version,'ACTIVATE:'+key,now()))
                self.store.connection.execute("UPDATE lesson_candidates SET status='ACTIVE',evaluation=? WHERE id=?",(canonical(details),key))
        return self.status(key)
    def rollback(self,version,expected_head):
        with self.store.transaction():
            current=self.head()
            if current!=expected_head:raise ContractError('Версия изменилась перед откатом.')
            ancestor=current
            while ancestor and ancestor!=version:
                row=self.store.connection.execute('SELECT parent FROM knowledge_versions WHERE id=?',(ancestor,)).fetchone()
                ancestor=row[0] if row else None
            if not ancestor:raise ContractError('Откат разрешён только к собственной предыдущей версии.')
            self.store.connection.execute("UPDATE metadata SET value=? WHERE key='knowledge_head'",(version,))
            self.store.connection.execute('INSERT INTO knowledge_events(previous,current,reason,created_at) VALUES (?,?,?,?)',(current,version,'ROLLBACK',now()))
            retained={digest(c) for c in self.cards()}
            for row in self.store.connection.execute("SELECT id FROM lesson_candidates WHERE status='ACTIVE'").fetchall():
                if row[0] not in retained:self.store.connection.execute("UPDATE lesson_candidates SET status='ROLLED_BACK' WHERE id=?",(row[0],))
        return {'active_version':version,'autonomous_writes':False}


def evaluate_imported(learning,key,results):
    """Import independently produced answers under an explicitly approved local rubric."""
    from .contracts import confined,read_json
    path=confined(learning.context.directory,'learning-policy.json')
    if not path.exists():return learning._result(key,'INSUFFICIENT_DATA')
    config=read_json(path)
    if set(config)!={'schema_version','approved','criteria'} or type(config['schema_version']) is not int or config['schema_version']!=1 or config['approved'] is not True:
        return learning._result(key,'OWNER_REQUIRED')
    criteria=config['criteria']
    if not isinstance(results,dict) or set(results)!={'candidate_id','base_version','criteria_hash','answers'} or results['candidate_id']!=key or results['base_version']!=learning.head() or results['criteria_hash']!=digest(criteria):
        raise ContractError('Контрольные ответы относятся к другому кандидату, базе или критериям.')
    expected_ids={case['id'] for case in criteria['cases']}
    if not isinstance(results['answers'],dict) or set(results['answers'])!=expected_ids:raise ContractError('Неполный контрольный прогон.')
    return learning.evaluate(key,criteria,lambda base,candidate,case:results['answers'][case['id']])
