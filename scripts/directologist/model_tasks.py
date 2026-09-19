"""Provider-neutral, bounded local model-task contract. Stubs are not AI Studio."""
import multiprocessing
import queue
from .contracts import ContractError,identifier,canonical
from .decisions import text

class StubA:
    def __call__(self,task):return {'status':'ok','label':'AMBIGUOUS','reason':'Локальный stub не делает содержательного вывода.'}
class StubB:
    def __call__(self,task):return {'reason':'Проверка переносимости контракта.','label':'AMBIGUOUS','status':'ok'}

def worker(adapter,task,out):
    try:
        result=adapter(task)
        if len(canonical(result).encode())>10000:raise ValueError()
        out.put(result)
    except BaseException:out.put({'status':'error'})

def run(task,adapter):
    if (not isinstance(task,dict) or set(task)!={'schema_version','project_id','kind','text','timeout_seconds'}
        or type(task['schema_version']) is not int or task['schema_version']!=1 or task['kind']!='intent-classification'):
        raise ContractError('Неподдерживаемый ModelTask; секретные поля не допускаются.')
    identifier(task['project_id']);text(task['text'],2000)
    seconds=task['timeout_seconds']
    if type(seconds) not in (int,float) or not 0.01<=seconds<=30:raise ContractError('Неверный timeout ModelTask.')
    ctx=multiprocessing.get_context('spawn');out=ctx.Queue(1);process=ctx.Process(target=worker,args=(adapter,task,out),daemon=True)
    try:
        process.start();process.join(seconds)
        if process.is_alive():
            process.terminate();process.join(1)
            if process.is_alive():process.kill();process.join(1)
            return {'status':'abstain','reason':'timeout'}
        try:result=out.get(timeout=0.2)
        except queue.Empty:return {'status':'error','reason':'no-result'}
        if (not isinstance(result,dict) or set(result)!={'status','label','reason'} or result['status']!='ok'
            or result['label'] not in {'COMMERCIAL','INFORMATIONAL','AMBIGUOUS'}):return {'status':'abstain','reason':'invalid-result'}
        text(result['reason'],1000)
        return result
    except Exception:return {'status':'error','reason':'adapter-error'}
    finally:
        if process.pid and process.is_alive():process.terminate();process.join(1)
        out.close()
