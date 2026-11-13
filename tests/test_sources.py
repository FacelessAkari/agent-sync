"""Fictional exports only: no real CLI, application database or network."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_sync import core, history as h, sources as s
from agent_sync.codex_rpc import json_bytes


CANARY = 'FICTIONAL_SOURCE_CANARY_do_not_execute_or_disclose'


class FakeRPC:
    """Public read-only protocol fixture; pagination is implemented by history."""

    def __init__(self, case, text=CANARY, mode='normal', nested_time='fixed'):
        self.case = case
        self.text = text
        self.mode = mode
        self.nested_time = nested_time
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def call(self, method, params):
        self.case.assertIn(method, ('thread/read', 'thread/turns/list', 'thread/items/list'))
        self.calls.append((method, dict(params)))
        self.case.assertEqual(params['threadId'], 'fictional-thread')
        if method == 'thread/read':
            self.case.assertEqual(params, {'threadId': 'fictional-thread', 'includeTurns': False})
            return {'thread': {'id': 'wrong-thread' if self.mode == 'wrong' else 'fictional-thread',
                               'extra': [1, False, None], 'captured_at': self.nested_time},
                    'opaque': {'inert': CANARY}}
        self.case.assertEqual(params, {
            'threadId': 'fictional-thread', 'limit': 100, 'sortDirection': 'asc',
            **({'cursor': 'next-page'} if 'cursor' in params else {})})
        if self.mode == 'error':
            raise RuntimeError(CANARY)
        if self.mode == 'loop':
            return {'data': [], 'nextCursor': 'next-page'}
        if method == 'thread/turns/list':
            return {'data': [{'id': 'fictional-turn', 'extra': {'preserved': True}}],
                    'nextCursor': None, 'extra_envelope': [None, 7]}
        if 'cursor' not in params:
            return {'items': [{'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': self.text}]}],
                'nextCursor': 'next-page', 'extra_envelope': {'keep': True}}
        return {'items': [{'type': 'function_call', 'name': 'never_execute', 'arguments': CANARY},
                          {'type': 'reasoning', 'encrypted_content': CANARY}],
                'nextCursor': None, 'backwardsCursor': 'inert-back'}


class SourcesTests(unittest.TestCase):
    def setUp(self):
        # Resolve the trusted temporary root before constructing paths: /var
        # can be a system symlink on macOS. Selected paths are never resolved.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'content'
        core.init_repo(self.repo)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.config = self.workspace / 'sources.json'
        self.state = self.workspace / 'sources-state.json'
        self.input = self.root / 'fictional-export.txt'
        self.input.write_bytes((CANARY + '\r\nsecond line\n').encode('utf-8'))
        self.command = self.root / 'fictional-command.json'
        self.command.write_bytes(json_bytes(['fictional-codex', 'app-server', '--stdio']))
        self.rpc_guard = mock.patch.object(s, 'RPC', side_effect=AssertionError('RPC forbidden'))
        self.no_rpc = self.rpc_guard.start()
        self.addCleanup(self.rpc_guard.stop)

    def file_entry(self, **changes):
        entry = {'id': 'selected', 'kind': 'file', 'path': str(self.input),
                 'format': 'text', 'tool': 'dsh'}
        entry.update(changes)
        return entry

    def codex_entry(self, **changes):
        entry = {'id': 'remote-export', 'kind': 'codex', 'thread_id': 'fictional-thread',
                 'command_file': str(self.command)}
        entry.update(changes)
        return entry

    def configure(self, entries, **changes):
        value = {'version': 1, 'sources': entries}
        value.update(changes)
        self.config.write_bytes(json_bytes(value))

    def refresh(self, workspace=None, repo=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = s.refresh(workspace or self.workspace, repo or self.repo)
        self.assertEqual(out.getvalue(), '')
        self.assertEqual(err.getvalue(), '')
        encoded = json.dumps(result)
        self.assertNotIn(CANARY, encoded)
        self.assertNotIn(str(self.input), encoded)
        self.assertEqual(set(result), {'status', 'sources', 'archived', 'unchanged', 'items'})
        for item in result['items']:
            self.assertEqual(set(item), {'id', 'status', 'archive_id'})
            self.assertRegex(item['archive_id'], h.ID_RE)
        return result

    def rejected(self, workspace=None, repo=None):
        before = self.state.read_bytes() if self.state.is_file() else None
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(h, 'create_archive', wraps=h.create_archive) as create:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                with self.assertRaises(core.Error) as caught:
                    s.refresh(workspace or self.workspace, repo or self.repo)
            create.assert_not_called()
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        message = str(caught.exception)
        for secret in (CANARY, str(self.input), str(self.workspace), str(self.repo), str(self.command)):
            self.assertNotIn(secret, message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(self.state.read_bytes() if self.state.is_file() else None, before)

    def archive_id(self, result):
        return result['items'][0]['archive_id']

    def test_missing_config_is_noop_without_state_archive_command_or_rpc(self):
        with mock.patch.object(s, 'command_argv') as command, \
                mock.patch.object(h, 'create_archive') as create, \
                mock.patch.object(core, '_atomic') as save:
            self.assertEqual(self.refresh(), {'status': 'not_configured', 'sources': 0,
                                             'archived': 0, 'unchanged': 0, 'items': []})
        command.assert_not_called()
        create.assert_not_called()
        save.assert_not_called()
        self.no_rpc.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertFalse((self.repo / 'data/history').exists())

    def test_text_changed_unchanged_preserves_exact_bytes_and_workspace_lock(self):
        self.configure([self.file_entry()])
        lock = self.workspace / 'lock'
        lock.write_bytes(b'caller owns this fictional lock')
        raw = self.input.read_bytes()
        first = self.refresh()
        aid = self.archive_id(first)
        self.assertEqual(first['archived'], 1)
        self.assertEqual(h.read_archive(self.repo, aid)[1], raw)
        state = json.loads(self.state.read_bytes())
        self.assertEqual(state, {'version': 1, 'sources': {'selected': {
            'fingerprint': h.sha(raw), 'archive_id': aid}}})
        second = self.refresh()
        self.assertEqual(second['unchanged'], 1)
        self.assertEqual(self.archive_id(second), aid)
        changed = raw + 'fictional unicode: 雪\r\n'.encode('utf-8')
        self.input.write_bytes(changed)
        third = self.refresh()
        self.assertNotEqual(self.archive_id(third), aid)
        self.assertEqual(h.read_archive(self.repo, self.archive_id(third))[1], changed)
        self.assertEqual(h.read_archive(self.repo, aid)[1], raw)
        self.assertEqual(self.input.read_bytes(), changed)
        self.assertEqual(lock.read_bytes(), b'caller owns this fictional lock')
        self.assertEqual(len(h.list_archives(self.repo)), 2)
        self.no_rpc.assert_not_called()

    def test_text_metadata_changes_do_not_hide_behind_raw_fingerprint(self):
        entry = self.file_entry()
        ids = []
        for change in ({}, {'tool': 'antigravity'}, {'source_id': 'fictional-session'},
                       {'project_id': 'fictional-project'}, {'source_id': None}):
            entry.update(change)
            self.configure([entry])
            result = self.refresh()
            self.assertEqual(result['archived'], 1)
            aid = self.archive_id(result)
            ids.append(aid)
            record, raw, _, normalized = h.read_archive(self.repo, aid)
            self.assertEqual(raw, self.input.read_bytes())
            self.assertEqual(normalized['tool'], entry['tool'])
            self.assertEqual(record['source_session_id'], entry.get('source_id'))
            self.assertEqual(record['project_id'], entry.get('project_id'))
            self.assertEqual(self.refresh()['unchanged'], 1)
        self.assertEqual(len(set(ids)), len(ids))

    def test_transcript_assertions_preflight_entire_batch_and_never_override(self):
        path = self.root / 'fictional-transcript.json'
        document = {'schema': h.TRANSCRIPT_SCHEMA, 'tool': 'codex',
                    'source_session_id': 'fictional-session', 'project_id': 'fictional-project',
                    'coverage': 'user-supplied', 'messages': [{'role': 'user', 'text': CANARY}]}
        raw = json.dumps(document, indent=3).encode('utf-8') + b'\n\n'
        path.write_bytes(raw)
        entry = {'id': 'transcript', 'kind': 'file', 'path': str(path), 'format': 'transcript-v1'}
        for change in ({'tool': 'dsh'}, {'source_id': 'different-session'},
                       {'project_id': 'different-project'}, {'project_id': None}):
            self.configure([self.file_entry(), {**entry, **change}])
            self.rejected()
        self.configure([{**entry, 'tool': 'codex', 'source_id': 'fictional-session',
                         'project_id': 'fictional-project'}])
        result = self.refresh()
        self.assertEqual(h.read_archive(self.repo, self.archive_id(result))[1], raw)
        self.assertEqual(path.read_bytes(), raw)

    def test_agy_raw_json_partial_coverage_and_project_rejection(self):
        raw = json.dumps({'conversation_id': 'fictional-agy', 'status': 'SUCCESS',
                          'response': CANARY, 'usage': {'tokens': 3},
                          'extra': {'inert': [True, None]}}, indent=2).encode('utf-8') + b'\n'
        self.input.write_bytes(raw)
        entry = self.file_entry(format='agy-result', tool='antigravity', source_id='fictional-agy')
        self.configure([{**entry, 'project_id': 'not-supported'}])
        self.rejected()
        self.configure([{**entry, 'source_id': 'mismatch'}])
        self.rejected()
        self.configure([{**entry, 'project_id': None}])
        result = self.refresh()
        record, stored, _, normalized = h.read_archive(self.repo, self.archive_id(result))
        self.assertEqual(stored, raw)
        self.assertEqual(record['coverage'], 'partial')
        self.assertEqual(normalized['omitted']['agy-field:extra'], 1)
        self.assertIsNone(record['project_id'])
        self.assertEqual(self.refresh()['unchanged'], 1)

    def test_codex_real_pagination_envelopes_and_captured_at_only_dedup(self):
        self.configure([self.codex_entry(timeout_seconds=4, max_pages=3)])
        results = []
        for timestamp, text, nested in [('capture-one', CANARY, 'fixed'),
                                         ('capture-two', CANARY, 'fixed'),
                                         ('capture-three', 'changed fictional content', 'fixed'),
                                         ('capture-four', 'changed fictional content', 'changed-nested')]:
            fake = FakeRPC(self, text=text, nested_time=nested)
            with mock.patch.object(s, 'RPC', return_value=fake) as rpc, \
                    mock.patch.object(h, 'now', return_value=timestamp), \
                    mock.patch.object(h, 'fetch_pages', wraps=h.fetch_pages) as pages:
                results.append(self.refresh())
            rpc.assert_called_once_with(['fictional-codex', 'app-server', '--stdio'],
                                        timeout=4, max_bytes=s.DEFAULT_LIMIT)
            self.assertEqual(pages.call_count, 2)
            self.assertTrue(fake.closed)
            self.assertEqual([method for method, _ in fake.calls],
                             ['thread/read', 'thread/turns/list', 'thread/items/list', 'thread/items/list'])
        first_id = self.archive_id(results[0])
        self.assertEqual(self.archive_id(results[1]), first_id)
        self.assertEqual(results[1]['unchanged'], 1)
        self.assertEqual([r['archived'] for r in results], [1, 0, 1, 1])
        record, raw, _, normalized = h.read_archive(self.repo, first_id)
        doc = json.loads(raw)
        self.assertEqual(doc['captured_at'], 'capture-one')
        self.assertEqual(doc['metadata']['thread']['extra'], [1, False, None])
        self.assertEqual(doc['turns_pages'][0]['result']['extra_envelope'], [None, 7])
        self.assertEqual(doc['items_pages'][0]['result']['extra_envelope'], {'keep': True})
        self.assertEqual(doc['items_pages'][1]['cursor'], 'next-page')
        self.assertEqual(doc['items_pages'][1]['result']['backwardsCursor'], 'inert-back')
        self.assertEqual(normalized['omitted']['item:function_call'], 1)
        self.assertEqual(record['coverage'], 'full-export')
        self.assertEqual(len(h.list_archives(self.repo)), 3)

    def test_codex_failure_or_invalid_selection_precedes_all_writes(self):
        for mode, pages in [('error', 3), ('loop', 3), ('normal', 1), ('wrong', 3)]:
            self.configure([self.file_entry(), self.codex_entry(max_pages=pages)])
            fake = FakeRPC(self, mode=mode)
            with mock.patch.object(s, 'RPC', return_value=fake):
                self.rejected()
            self.assertTrue(fake.closed)
            if mode == 'wrong':
                self.assertEqual(len(fake.calls), 1)
        for changes in ({'project_id': 'unsupported'}, {'timeout_seconds': True},
                        {'timeout_seconds': 301}, {'timeout_seconds': 0},
                        {'max_pages': True}, {'max_pages': h.MAX_PAGES + 1},
                        {'thread_id': ''}, {'command_file': None}):
            self.configure([self.file_entry(), self.codex_entry(**changes)])
            self.rejected()
        entry = self.codex_entry()
        del entry['command_file']
        self.configure([entry])
        with mock.patch.object(s, 'command_argv') as command:
            self.rejected()
        command.assert_not_called()
        self.no_rpc.assert_not_called()
        self.assertEqual(h.list_archives(self.repo), [])

    def test_missing_or_damaged_references_error_even_when_source_changed(self):
        self.configure([self.file_entry()])
        aid = self.archive_id(self.refresh())
        directory = h.archive_path(self.repo, aid)
        original_raw = self.input.read_bytes()
        for changed in (False, True):
            self.input.write_bytes(original_raw + (b'changed' if changed else b''))
            for name in ('record.json', 'transcript.md'):
                target = directory / name
                original = target.read_bytes()
                for damage in ('missing', 'tampered'):
                    with self.subTest(changed=changed, name=name, damage=damage):
                        if damage == 'missing':
                            target.unlink()
                        else:
                            target.write_bytes(original + CANARY.encode('utf-8'))
                        try:
                            self.rejected()
                        finally:
                            target.write_bytes(original)
            moved = self.root / 'temporarily-missing-archive'
            directory.rename(moved)
            try:
                self.rejected()
            finally:
                moved.rename(directory)

    def test_state_fingerprint_and_archive_substitution_are_not_authority(self):
        self.configure([self.file_entry()])
        self.refresh()
        original = self.state.read_bytes()
        other = h.create_archive(self.repo, b'different fictional payload', 'text', tool='dsh')['archive_id']
        state = json.loads(original)
        state['sources']['selected']['archive_id'] = other
        self.state.write_bytes(json_bytes(state))
        self.rejected()
        state = json.loads(original)
        self.input.write_bytes(b'new fictional input')
        state['sources']['selected']['fingerprint'] = h.sha(self.input.read_bytes())
        self.state.write_bytes(json_bytes(state))
        self.rejected()
        # A stale fingerprint must agree with its old archive even if it does
        # not match the current source fingerprint.
        state['sources']['selected']['fingerprint'] = '0' * 64
        self.state.write_bytes(json_bytes(state))
        self.rejected()

    def test_strict_config_and_late_invalid_or_oversized_files_write_nothing(self):
        good = self.file_entry()
        cases = [
            {'version': True, 'sources': [good]},
            {'version': 1, 'sources': [good], 'unknown': CANARY},
            {'version': 1, 'sources': [good, good]},
            {'version': 1, 'sources': [{**good, 'id': '../invalid'}]},
            {'version': 1, 'sources': [{**good, 'unknown': CANARY}]},
            {'version': 1, 'sources': [{**good, 'format': 'codex-export-v1'}]},
            {'version': 1, 'sources': [{k: v for k, v in good.items() if k != 'tool'}]},
            {'version': 1, 'sources': [good] * 101},
        ]
        cases.extend({'version': 1, 'sources': [good], 'limit_bytes': limit}
                     for limit in (True, 0, -1, 1.5, s.MAX_LIMIT + 1))
        for document in cases:
            self.config.write_bytes(json_bytes(document))
            self.rejected()
        for raw in (b'{"version":1,"version":1,"sources":[]}',
                    b'{"version":1,"sources":[],"limit_bytes":NaN}',
                    b'\xff', b' ' * (s.JSON_LIMIT + 1)):
            self.config.write_bytes(raw)
            self.rejected()
        bad = self.root / 'late-invalid.txt'
        bad.write_bytes(b'\xff')
        self.configure([good, self.file_entry(id='late', path=str(bad))])
        self.rejected()
        bad.write_bytes(b'x' * 4097)
        self.configure([good, self.file_entry(id='late', path=str(bad))], limit_bytes=4096)
        self.rejected()
        self.assertEqual(h.list_archives(self.repo), [])
        self.no_rpc.assert_not_called()

    def test_state_schema_bounds_and_removed_sources_are_pruned(self):
        self.configure([self.file_entry()])
        aid = self.archive_id(self.refresh())
        saved = self.state.read_bytes()
        valid = {'fingerprint': h.sha(self.input.read_bytes()), 'archive_id': aid}
        cases = [
            {'version': True, 'sources': {}},
            {'version': 1, 'sources': {}, 'extra': None},
            {'version': 1, 'sources': []},
            {'version': 1, 'sources': {'../invalid': valid}},
            {'version': 1, 'sources': {'selected': {**valid, 'extra': 1}}},
            {'version': 1, 'sources': {'selected': {**valid, 'archive_id': '../invalid'}}},
            {'version': 1, 'sources': {'selected': {**valid, 'fingerprint': 'G' * 64}}},
            {'version': 1, 'sources': {'selected': valid, 'other': valid}},
        ]
        for document in cases:
            self.state.write_bytes(json_bytes(document))
            self.rejected()
        for raw in (b' ' * (s.JSON_LIMIT + 1), b'{"version":1,"version":1,"sources":{}}'):
            self.state.write_bytes(raw)
            self.rejected()
        self.state.write_bytes(saved)
        self.configure([])
        result = self.refresh()
        self.assertEqual(result['sources'], 0)
        self.assertEqual(json.loads(self.state.read_bytes()), {'version': 1, 'sources': {}})
        self.assertEqual(len(h.list_archives(self.repo)), 1)

    def test_absolute_external_paths_and_root_overlaps_are_rejected(self):
        for path in (self.config, self.state, self.repo / 'manifest.json',
                     Path(s.__file__).resolve(), self.workspace, self.root / 'absent.txt',
                     self.root / 'unused' / '..' / self.input.name, Path('relative.txt')):
            self.configure([self.file_entry(path=str(path))])
            self.rejected()
            self.configure([self.codex_entry(command_file=str(path))])
            self.rejected()
        self.configure([self.file_entry()])
        for workspace, repo in ((self.repo, self.repo), (self.root, self.repo),
                                (self.workspace, self.root), (s.CODE_ROOT, self.repo),
                                (self.workspace, s.CODE_ROOT), (self.input, self.repo)):
            self.rejected(workspace=workspace, repo=repo)
        self.no_rpc.assert_not_called()

    def test_real_symlinks_ancestors_hardlinks_and_workspace_files(self):
        # No skips: Windows project tests require elevated symlink privileges.
        link = self.root / 'source-link.txt'
        link.symlink_to(self.input)
        ancestor = self.root / 'directory-link'
        ancestor.symlink_to(self.root, target_is_directory=True)
        broken = self.root / 'broken-link.txt'
        broken.symlink_to(self.root / 'missing-target')
        for path in (link, ancestor / self.input.name, broken):
            self.configure([self.file_entry(path=str(path))])
            self.rejected()
            self.configure([self.codex_entry(command_file=str(path))])
            self.rejected()
        hard = self.root / 'hardlinked-input.txt'
        os.link(self.input, hard)
        try:
            for path in (hard, self.input):
                self.configure([self.file_entry(path=str(path))])
                self.rejected()
        finally:
            hard.unlink()
        self.configure([self.file_entry()])
        config_raw = self.config.read_bytes()
        external_config = self.root / 'external-config.json'
        external_config.write_bytes(config_raw)
        self.config.unlink()
        self.config.symlink_to(external_config)
        self.rejected()
        self.config.unlink()
        self.config.write_bytes(config_raw)
        os.link(self.config, hard)
        try:
            self.rejected()
        finally:
            hard.unlink()
        external_state = self.root / 'external-state.json'
        external_state.write_bytes(json_bytes({'version': 1, 'sources': {}}))
        self.state.symlink_to(external_state)
        self.rejected()
        self.state.unlink()
        self.state.write_bytes(external_state.read_bytes())
        os.link(self.state, hard)
        try:
            self.rejected()
        finally:
            hard.unlink()
        self.no_rpc.assert_not_called()

    def test_unstable_reads_reject_changed_hash_and_changed_stat(self):
        self.configure([self.file_entry()])
        original = self.input.read_bytes()
        real_read = s.bounded_read
        for mode in ('hash', 'stat'):
            calls = []

            def unstable(path, limit):
                raw = real_read(path, limit)
                if Path(path) == self.input:
                    calls.append(1)
                    if mode == 'hash' and len(calls) == 2:
                        return b'z' * len(raw)
                    if mode == 'stat' and len(calls) == 1:
                        self.input.write_bytes(raw + b'changed')
                return raw

            with mock.patch.object(s, 'bounded_read', side_effect=unstable):
                self.rejected()
            self.assertTrue(calls)
            self.input.write_bytes(original)
        self.assertEqual(h.list_archives(self.repo), [])

    def test_failed_final_state_commit_leaves_immutable_orphan_not_success(self):
        self.configure([self.file_entry()])
        first = self.archive_id(self.refresh())
        old_state = self.state.read_bytes()
        old_archive = h.read_archive(self.repo, first)
        self.input.write_bytes(b'changed fictional export')
        with mock.patch.object(core, '_atomic', side_effect=OSError(CANARY)):
            with self.assertRaises(core.Error) as caught:
                self.refresh()
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(self.state.read_bytes(), old_state)
        self.assertEqual(h.read_archive(self.repo, first), old_archive)
        self.assertEqual(len(h.list_archives(self.repo)), 2)
        result = self.refresh()
        self.assertEqual(result['archived'], 1)
        self.assertEqual(len(h.list_archives(self.repo)), 3)
        self.assertEqual(h.read_archive(self.repo, first), old_archive)


if __name__ == '__main__':
    unittest.main()
