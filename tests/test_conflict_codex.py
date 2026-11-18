import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_sync import conflict_codex as reviewer
from agent_sync.core import Error


def case(ident='c1'):
    return dict(id=ident, base='Items', local='Items\nA', repository='Items\nB',
                proposed='Items\nA\nB')


def response(decisions):
    return [{'type': 'thread.started'}, {'type': 'turn.started'},
            {'type': 'item.completed', 'item': {'type': 'agent_message',
             'text': json.dumps({'decisions': decisions})}}, {'type': 'turn.completed'}]


def event_bytes(events):
    return b'\n'.join(reviewer.json_bytes(e).rstrip() for e in events)


class ConflictCodexTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / 'workspace'
        self.repo = self.root / 'repo'

    def review(self, cases, output=None, failure=None):
        def run(argv, **kw):
            self.assertEqual(kw['shell'], False)
            self.assertEqual(kw['stderr'], subprocess.DEVNULL)
            self.assertEqual(kw['timeout'], reviewer.TIMEOUT)
            self.assertNotIn(str(self.root), str(argv))
            self.assertNotIn('Items', str(argv))
            self.assertEqual(reviewer.json_loads(kw['input']), {'cases': cases})
            self.assertFalse(Path(kw['cwd']).is_relative_to(self.root))
            schema = Path(argv[argv.index('--output-schema') + 1])
            self.assertEqual(reviewer.json_loads(schema.read_bytes()), reviewer.SCHEMA)
            self.assertIn('--ephemeral', argv)
            self.assertIn('--ignore-user-config', argv)
            self.assertIn('--ignore-rules', argv)
            self.assertIn('read-only', argv)
            self.assertNotIn('CODEX_APP_TOOLS_PIPE_PATH', kw['env'])
            if isinstance(failure, Exception):
                raise failure
            kw['stdout'].write(output or b'')
            return subprocess.CompletedProcess(argv, failure or 0)

        with patch.object(reviewer, '_executable', return_value='codex'), \
                patch.object(reviewer.subprocess, 'run', side_effect=run):
            return reviewer.approve_merge(self.work, self.repo, cases)

    def test_classifies_only_requested_ids_and_isolates_process(self):
        decisions = [{'id': 'c2', 'decision': 'review'}, {'id': 'c1', 'decision': 'approve'}]
        events = response(decisions)
        events.insert(1, {'type': 'item.completed', 'item': {'type': 'error', 'message': 'startup notice'}})
        self.assertEqual(self.review([case(), case('c2')], event_bytes(events)),
                         {'c1': True, 'c2': False})

    def test_validate_before_start(self):
        with patch.object(reviewer.subprocess, 'run') as launch:
            self.assertEqual(reviewer.approve_merge(self.work, self.repo, []), {})
            for invalid in ([case(), case()], [case('../c')], [dict(case(), path='/test')],
                            [dict(case(), base=b'bytes')], [dict(case(), local='\0')],
                            [case(str(i)) for i in range(reviewer.MAX_CASES + 1)],
                            [dict(case(), base='字' * (reviewer.MAX_INPUT // 3))]):
                with self.subTest(invalid_type=type(invalid)), self.assertRaises(reviewer.UnsupportedInput):
                    reviewer.approve_merge(self.work, self.repo, invalid)
            launch.assert_not_called()

    def test_refuses_common_credentials_without_echoing(self):
        samples = ['https://example:synthetic@host.invalid', 'sk-' + 'a' * 26,
                   'ghp_' + 'h' * 22, '-----BEGIN OPENSSH PRIVATE KEY-----',
                   'Authorization: Bearer synthetic-example', '{"api_key":"synthetic"}']
        with patch.object(reviewer.subprocess, 'run') as launch:
            for field, sample in zip(reviewer.FIELDS * 2, samples):
                with self.subTest(field=field), self.assertRaises(reviewer.UnsupportedInput) as err:
                    reviewer.approve_merge(self.work, self.repo, [dict(case(), **{field: sample})])
                self.assertNotIn(sample, str(err.exception))
            launch.assert_not_called()

    def test_rejects_incomplete_or_ambiguous_decisions(self):
        valid = {'id': 'c1', 'decision': 'approve'}
        for entries in ([], [dict(valid, id='c9')], [valid, valid], [dict(valid, merged='X')],
                        [dict(valid, decision=True)], [dict(valid, decision=['approve'])]):
            with self.subTest(entries=entries), self.assertRaises(Error):
                self.review([case()], event_bytes(response(entries)))
        for text in ('{"decisions":[],"decisions":[]}', 'Prefix {"decisions":[]}',
                     '{"decisions":[],"other":1}', 'null', '[]'):
            events = response([valid])
            events[2]['item']['text'] = text
            with self.subTest(text=text), self.assertRaises(Error):
                self.review([case()], event_bytes(events))

    def test_rejects_other_actions_truncation_and_faults(self):
        events = response([{'id': 'c1', 'decision': 'approve'}])
        tool = {'type': 'item.completed', 'item': {'type': 'command_execution'}}
        for stream in (events[:-1], events[:-1] + [events[2], events[3]], events + [tool],
                       [tool, *events], [{'type': 'error'}], [], [*events, *events]):
            with self.subTest(stream=stream), self.assertRaises(Error):
                self.review([case()], event_bytes(stream))
        for output in (b'x' * (reviewer.MAX_EVENTS + 1), b'\xff'):
            with self.assertRaises(Error):
                self.review([case()], output)
        for failure in (1, FileNotFoundError(), subprocess.TimeoutExpired('codex', 90)):
            with self.assertRaises(Error):
                self.review([case()], failure=failure)


if __name__ == '__main__':
    unittest.main()
