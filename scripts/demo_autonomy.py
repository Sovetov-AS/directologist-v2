"""Run the offline autonomous campaign acceptance scenarios with networking forbidden."""
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_direct_executor import DirectExecutorTests


def demo():
    cases = [
        'test_workflow_build_wait_then_resume_no_duplicate_objects',
        'test_timeout_after_send_reconcile_without_second_write',
        'test_budget_stop_still_allows_suspension',
        'test_revoked_grant_can_stop_and_reconcile_but_not_write',
        'test_blueprint_invalid_landing_rejected_before_creation',
    ]
    output=io.StringIO()
    result=unittest.TextTestRunner(stream=output).run(unittest.TestSuite(DirectExecutorTests(case) for case in cases))
    if not result.wasSuccessful():
        raise RuntimeError('Offline autonomy scenario failed; run unittest for details.')
    return {'status':'PASS','mode':'OFFLINE_SYNTHETIC','network':'FORBIDDEN',
            'scenarios':cases,'tests':result.testsRun,'real_api_acceptance':False}


if __name__=='__main__':print(json.dumps(demo(),ensure_ascii=False,indent=2))
