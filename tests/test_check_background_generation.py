"""Fast source checks do not require synchronous generation receipts."""
import subprocess
import unittest
from unittest.mock import patch
from shiftboss import check, regen
from shiftboss.merge import MergeError

class BackgroundGenerationCheck(unittest.TestCase):
    def test_fast_check_never_discovers_generation_closure(self):
        with (patch.object(check, 'git', return_value=subprocess.CompletedProcess([],0,'base\n','')),
              patch.object(check, 'fast_checks') as fast,
              patch.object(regen, 'closure_stages', side_effect=AssertionError('generation in fast check'))):
            self.assertEqual(check.main([]), 0)
            fast.assert_called_once_with('base')

    def test_source_failure_still_rejects(self):
        with (patch.object(check, 'git', return_value=subprocess.CompletedProcess([],0,'base\n','')),
              patch.object(check, 'fast_checks', side_effect=MergeError('invalid source'))):
            self.assertEqual(check.main([]), 1)
