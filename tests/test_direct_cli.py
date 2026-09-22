import json
import shutil
import unittest
from pathlib import Path
from analysis_fixtures import setup
from test_direct_executor import grant
from directologist.cli import execute,parser
from directologist.contracts import ContractError,digest

class DirectCLITests(unittest.TestCase):
    def setUp(self):
        setup(self)
        shutil.copytree(Path(__file__).resolve().parents[1]/'knowledge',self.root/'knowledge')
        self.value=grant(self.ctx);self.file=self.root/'grant.json';self.file.write_text(json.dumps(self.value))
    def command(self,*args):
        return execute(parser().parse_args(['--workspace',str(self.root),'--project','fixture',*args]))
    def test_grant_activation_and_context_then_revocation(self):
        check=self.command('direct','grant-check','--input',str(self.file))
        self.assertFalse(check['activated'])
        with self.assertRaises(ContractError):self.command('direct','grant-activate','--input',str(self.file),'--confirmation','wrong')
        result=self.command('direct','grant-activate','--input',str(self.file),'--confirmation',check['sha256'])
        self.assertTrue(result['registered'])
        self.assertEqual(self.command('context')['live_write_mode'],'TRUSTED_LOCAL')
        self.assertEqual(self.command('direct','grant-status')['managed_campaign_ids'],[1])
        self.command('direct','revoke')
        self.assertFalse(self.command('context')['autonomous_writes'])
    def test_example_requires_real_context_and_fresh_dates(self):
        root=Path(__file__).resolve().parents[1]
        base=root/'examples' if (root/'examples').exists() else root/'release/examples'
        example=json.loads((base/'direct-grant.example.json').read_text())
        self.file.write_text(json.dumps(example))
        with self.assertRaises(ContractError):self.command('direct','grant-check','--input',str(self.file))
