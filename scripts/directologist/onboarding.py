"""Create an explicit user project, never copy accounts or budgets."""
import os
from .contracts import ContractError, canonical, confined, identifier, validate_profile

def create(workspace, project_id, display_name, timezone):
    identifier(project_id)
    workspace=workspace.expanduser().resolve(strict=True)
    profile={'schema_version':1,'project_id':project_id,'display_name':display_name,'timezone':timezone,'binding_version':0,'bindings':{}}
    validate_profile(profile,project_id)
    folder=confined(workspace,'projects',project_id)
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=confined(folder,'profile.json')
    try:
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        raise ContractError('Проект уже существует; профиль не перезаписывается.') from None
    with os.fdopen(fd,'w',encoding='utf-8') as f:f.write(canonical(profile)+'\n')
    return {'project_id':project_id,'created':True,'next_step':'setup','autonomous_writes':False}
