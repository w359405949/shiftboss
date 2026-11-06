import unittest
from unittest.mock import patch
from shiftboss import config, merge
from shiftboss.background_commands import commands
from shiftboss.named_imports_check import verification_gates

class MovedFastCommands(unittest.TestCase):
    def test_same_argv_registered_once_and_not_run_in_foreground(self):
        items=[{'name':'slow','run':['python','rule.py'],'background':True}]
        with patch.object(config,'fast_commands',return_value=items), patch('shiftboss.named_imports_check.enabled',return_value=False):
            with patch.object(merge,'run_until_idle',side_effect=AssertionError('foreground')):
                merge.run_fast_commands(['src/a.ts'])
            commands=verification_gates([],None)
            self.assertEqual(commands,[['python','rule.py']])
            self.assertEqual(verification_gates(commands,None),commands)
    def test_bad_registration_refuses_instead_of_silent_skip(self):
        for bad in [True, 'regen.closure_command', []]:
            with patch.object(config,'fast_commands',return_value=[{'name':'bad','run':bad,'background':True}]):
                with self.assertRaises(ValueError):commands()
