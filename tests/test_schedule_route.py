"""Routing only: no scheduler installation or external state changes."""
from contextlib import redirect_stdout
import io
import sys
import types
import unittest
from unittest import mock

from agent_sync import __main__ as cli
from agent_sync import private_remote


class ScheduleRouteTests(unittest.TestCase):
    def test_schedule_tail_and_repo_forwarding(self):
        cases = [
            (['--repo', 'CONTENT', 'schedule', 'install', '--workspace', 'LOCAL'],
             ['install', '--workspace', 'LOCAL', '--repo', 'CONTENT']),
            (['schedule', 'install', '--repo', 'CONTENT', '--workspace', 'LOCAL'],
             ['install', '--repo', 'CONTENT', '--workspace', 'LOCAL']),
            (['--repo', 'GLOBAL', 'schedule', 'status', '--repo=CONTENT',
              '--workspace', 'LOCAL'],
             ['status', '--repo=CONTENT', '--workspace', 'LOCAL']),
            (['--repo=CONTENT', 'schedule', 'remove', '--workspace', 'LOCAL'],
             ['remove', '--workspace', 'LOCAL', '--repo', 'CONTENT']),
            (['schedule', 'install', '--workspace', 'LOCAL', '--interval-minutes', '60'],
             ['install', '--workspace', 'LOCAL', '--interval-minutes', '60']),
            (['schedule', '--help'], ['--help']),
        ]
        scheduler = types.ModuleType('agent_sync.scheduler')
        scheduler.main = mock.Mock(return_value=7)
        with mock.patch.dict(sys.modules, {'agent_sync.scheduler': scheduler}), \
             mock.patch.object(cli.core, 'configure_stdio'):
            for argv, expected in cases:
                with self.subTest(argv=argv):
                    scheduler.main.reset_mock()
                    self.assertEqual(cli.main(argv), 7)
                    scheduler.main.assert_called_once_with(expected)

    def test_main_help_advertises_schedule_without_running_it(self):
        output = io.StringIO()
        with mock.patch.object(cli.core, 'configure_stdio'), redirect_stdout(output):
            with self.assertRaises(SystemExit) as exited:
                cli.main(['--help'])
        self.assertEqual(exited.exception.code, 0)
        self.assertIn('schedule', output.getvalue())

    def test_private_installation_wrapper_preserves_metadata_and_errors(self):
        metadata = {'visibility': 'private'}
        with mock.patch.object(private_remote, 'require_private', return_value=metadata) as require:
            self.assertIs(private_remote.assert_private_repo('CONTENT'), metadata)
            require.assert_called_once_with('CONTENT')
        failure = ValueError('rejected')
        with mock.patch.object(private_remote, 'require_private', side_effect=failure):
            with self.assertRaises(ValueError) as raised:
                private_remote.assert_private_repo('CONTENT')
        self.assertIs(raised.exception, failure)
