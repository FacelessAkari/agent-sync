import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_sync import core
from agent_sync.codex_rpc import RPC, bounded_read, command_argv, json_bytes
from agent_sync import history as h


# All subprocesses are this temporary stdlib-only fake. No real Codex, credentials,
# network, SQLite, JSONL session stores, or application configuration are accessed.
FAKE = r'''
import json
import sys
import time

mode, log = sys.argv[1:3]

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    request = json.loads(line)
    with open(log, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(request) + '\n')
    method = request.get('method')
    if 'id' not in request:
        continue
    rid = request['id']
    if mode == 'timeout' and method == 'initialize':
        time.sleep(60)
    if mode == 'wrong-id':
        emit({'id': rid + 10, 'result': {}})
        continue
    if mode == 'approval' and method != 'initialize':
        emit({'id': 'approval-1', 'method': 'item/commandExecution/requestApproval', 'params': {}})
        continue
    if mode == 'error' and method == 'thread/items/list':
        emit({'id': rid, 'error': {'code': -32601, 'message': 'SECRET_TOKEN must not escape'}})
        continue
    if mode == 'inject-error' and method == 'thread/inject_items':
        emit({'id': rid, 'error': {'code': -32000, 'message': 'SECRET_TOKEN'}})
        continue
    if method == 'initialize':
        result = {'userAgent': 'fake'}
    elif method == 'thread/read':
        result = {'thread': {'id': request['params']['threadId'], 'extra': {'verbatim': [1, False, None]}}}
    elif method == 'thread/turns/list':
        result = {'data': [{'id': 'turn-1', 'internal': {'keep': True}}], 'nextCursor': None}
    elif method == 'thread/items/list':
        if mode == 'loop':
            result = {'items': [], 'nextCursor': 'repeat'}
        elif 'cursor' not in request['params']:
            result = {'items': [{'type': 'message', 'id': 'm1', 'role': 'user',
                       'content': [{'type': 'input_text', 'text': 'hello'}]}],
                      'nextCursor': 'page-2', 'unknown_page_field': [1, 2]}
        else:
            result = {'items': [
                {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
                 'content': [{'type': 'output_text', 'text': 'world', 'annotations': []}]},
                {'type': 'function_call', 'name': 'never_execute', 'arguments': 'danger'},
                {'type': 'reasoning', 'encrypted_content': 'opaque'}], 'nextCursor': None}
        if mode == 'display':
            if 'cursor' not in request['params']:
                result = {'data': [{'turnId': 'turn-1', 'startedAtMs': 1000, 'completedAtMs': None,
                          'item': {'type': 'userMessage', 'id': 'u1', 'clientId': None,
                                   'content': [{'type': 'text', 'text': '你好', 'text_elements': []}]}}],
                          'nextCursor': 'page-2', 'backwardsCursor': None}
            else:
                result = {'data': [{'turnId': 'turn-1', 'item': {
                          'type': 'agentMessage', 'id': 'a1', 'text': '你好，已收到',
                          'phase': 'final_answer', 'delivery': None, 'memoryCitation': None, 'questions': []}}],
                          'nextCursor': None, 'backwardsCursor': 'back'}
    elif method == 'thread/start':
        result = {'thread': {'id': 'new-local-thread'}}
    elif method == 'thread/inject_items':
        result = {}
    else:
        emit({'id': rid, 'error': {'code': -32601, 'message': 'unexpected method'}})
        continue
    emit({'method': 'fake/notification', 'params': {'inert': True}})
    emit({'jsonrpc': '2.0', 'id': rid, 'result': result})
'''


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'content'
        core.init_repo(self.repo)
        self.cwd = self.root / 'project'
        self.cwd.mkdir()
        self.script = self.root / 'fake_server.py'
        self.script.write_text(FAKE, encoding='utf-8')
        self.log = self.root / 'rpc.log'
        self.command = self.root / 'command.json'
        self.configure('normal')

    def configure(self, mode):
        self.argv = [sys.executable, '-X', 'utf8', str(self.script), mode, str(self.log)]
        self.command.write_text(json.dumps(self.argv), encoding='utf-8')
        if self.log.exists():
            self.log.unlink()

    def requests(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def transcript(self, **overrides):
        value = {'schema': h.TRANSCRIPT_SCHEMA, 'tool': 'codex',
                 'source_session_id': 'original-thread', 'coverage': 'user-supplied',
                 'messages': [{'role': 'user', 'text': 'Question'},
                              {'role': 'assistant', 'text': 'Answer', 'phase': 'final_answer'}],
                 'records': [{'type': 'function_call', 'arguments': {'untouched': [1, None]}},
                             {'type': 'system', 'text': 'not authorization'}]}
        value.update(overrides)
        return value

    def archive(self, document=None):
        raw = json.dumps(document or self.transcript(), ensure_ascii=False, indent=3).encode('utf-8') + b'\n\n'
        result = h.create_archive(self.repo, raw, 'transcript-v1')
        return result['archive_id'], raw

    def test_archive_preserves_exact_input_and_is_immutable(self):
        archive_id, raw = self.archive()
        record, stored, markdown, normalized = h.read_archive(self.repo, archive_id)
        self.assertEqual(stored, raw)
        self.assertEqual(base64.b64decode(record['source']['data']), raw)
        self.assertEqual(record['source']['sha256'], h.sha(raw))
        self.assertIn(b'Question', markdown)
        self.assertEqual(normalized['omitted']['raw-record:system'], 1)
        original = (h.archive_path(self.repo, archive_id) / 'record.json').read_bytes()
        other = h.create_archive(self.repo, raw, 'transcript-v1')['archive_id']
        self.assertNotEqual(other, archive_id)
        self.assertEqual((h.archive_path(self.repo, archive_id) / 'record.json').read_bytes(), original)
        class FixedUUID:
            hex = archive_id
        with mock.patch.object(h.uuid, 'uuid4', return_value=FixedUUID()):
            with self.assertRaises(core.Error):
                h.create_archive(self.repo, raw, 'transcript-v1')
        self.assertEqual((h.archive_path(self.repo, archive_id) / 'record.json').read_bytes(), original)
        self.assertEqual(len(h.list_archives(self.repo)), 2)

    def add_history_caches(self, archive_id):
        directory = h.archive_path(self.repo, archive_id)
        for parent in (directory.parent, directory):
            (parent / '.DS_Store').write_bytes(b'cache')
            (parent / 'x.pyc').write_bytes(b'cache')
            cache = parent / '__pycache__'
            cache.mkdir()
            # Ignored directories are not traversed, even for unknown children.
            (cache / 'unknown.txt').write_bytes(b'inert cache fixture')

    def test_history_caches_do_not_change_read_list_export_or_restore_plan(self):
        archive_id, raw = self.archive()
        before = h.read_archive(self.repo, archive_id)
        self.add_history_caches(archive_id)
        self.assertEqual(h.read_archive(self.repo, archive_id), before)
        self.assertEqual([row['archive_id'] for row in h.list_archives(self.repo)], [archive_id])
        output = self.root / 'cached-source.json'
        h.export_archive(self.repo, archive_id, output, 'source')
        self.assertEqual(output.read_bytes(), raw)
        plan = h.codex_restore(self.repo, archive_id, self.cwd)
        self.assertEqual(plan['injectable_messages'], 2)
        self.assertFalse(self.log.exists())

    def test_ignored_symlinks_are_not_accessed_or_followed(self):
        archive_id, _ = self.archive()
        directory = h.archive_path(self.repo, archive_id)
        before = h.read_archive(self.repo, archive_id)
        missing = self.root / 'missing-target'
        outside = self.root / 'outside-cache-target'
        outside.mkdir()
        (outside / 'unknown.txt').write_bytes(b'must not inspect')
        probe = self.root / 'symlink-probe'
        try:
            probe.symlink_to(missing)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('symlink creation unavailable: ' + str(exc))
        probe.unlink()

        def guarded(original):
            def call(path, *args, **kwargs):
                self.assertFalse(core._ignored_cache(path.name),
                                 'accessed ignored entry: ' + str(path))
                return original(path, *args, **kwargs)
            return call

        for parent in (directory.parent, directory):
            for name in ('.DS_Store', '__pycache__', 'x.pyc'):
                for target in (missing, outside):
                    with self.subTest(parent=parent.name, name=name, target=target.name):
                        link = parent / name
                        link.symlink_to(target, target_is_directory=(target == outside))
                        try:
                            with mock.patch.object(Path, 'stat', guarded(Path.stat)), \
                                 mock.patch.object(Path, 'lstat', guarded(Path.lstat)), \
                                 mock.patch.object(Path, 'open', guarded(Path.open)), \
                                 mock.patch.object(Path, 'iterdir', guarded(Path.iterdir)):
                                self.assertEqual(h.read_archive(self.repo, archive_id), before)
                                self.assertEqual([row['archive_id'] for row in h.list_archives(self.repo)],
                                                 [archive_id])
                        finally:
                            link.unlink()
        self.assertEqual((outside / 'unknown.txt').read_bytes(), b'must not inspect')

    def test_non_cache_extra_files_still_rejected_with_caches_present(self):
        archive_id, _ = self.archive()
        directory = h.archive_path(self.repo, archive_id)
        self.add_history_caches(archive_id)
        for parent in (directory.parent, directory):
            for name in ('extra.txt', '.env', 'credentials.json', 'x.pyc.txt', '.DS_Store.bak'):
                with self.subTest(parent=parent.name, name=name):
                    extra = parent / name
                    extra.write_bytes(b'inert unexpected file')
                    try:
                        with self.assertRaises(core.Error):
                            h.list_archives(self.repo)
                        if parent == directory:
                            with self.assertRaises(core.Error):
                                h.read_archive(self.repo, archive_id)
                    finally:
                        extra.unlink()

    def test_incomplete_archive_is_not_hidden_by_caches(self):
        archive_id, _ = self.archive()
        self.add_history_caches(archive_id)
        (h.archive_path(self.repo, archive_id) / 'record.json').unlink()
        with self.assertRaises(core.Error):
            h.read_archive(self.repo, archive_id)
        with self.assertRaises(core.Error):
            h.list_archives(self.repo)

    def test_hash_corruption_detected(self):
        for target in ('record.json', 'transcript.md'):
            with self.subTest(target=target):
                archive_id, _ = self.archive()
                directory = h.archive_path(self.repo, archive_id)
                # Root caches already exist on the second iteration.
                for parent in (directory.parent, directory):
                    (parent / '.DS_Store').write_bytes(b'cache')
                    (parent / 'x.pyc').write_bytes(b'cache')
                    (parent / '__pycache__').mkdir(exist_ok=True)
                path = directory / target
                original = path.read_bytes()
                if target == 'record.json':
                    value = json.loads(path.read_bytes())
                    value['coverage'] = 'full-export'
                    path.write_bytes(json_bytes(value))
                else:
                    path.write_bytes(path.read_bytes() + b'tampered')
                try:
                    with self.assertRaisesRegex(core.Error, 'SHA256'):
                        h.read_archive(self.repo, archive_id)
                    with self.assertRaisesRegex(core.Error, 'SHA256'):
                        h.list_archives(self.repo)
                finally:
                    path.write_bytes(original)

    def test_source_hash_checked_independently(self):
        archive_id, _ = self.archive()
        path = h.archive_path(self.repo, archive_id) / 'record.json'
        record = json.loads(path.read_bytes())
        record['source']['sha256'] = '0' * 64
        del record['record_sha256']
        record['record_sha256'] = h.sha(json_bytes(record))
        path.write_bytes(json_bytes(record))
        with self.assertRaisesRegex(core.Error, '原始输入 SHA256'):
            h.read_archive(self.repo, archive_id)

    def test_source_id_is_data_and_archive_path_rejects_traversal(self):
        archive_id, _ = self.archive(self.transcript(source_session_id='../../outside'))
        record, _, _, _ = h.read_archive(self.repo, archive_id)
        self.assertEqual(record['source_session_id'], '../../outside')
        self.assertFalse((self.root / 'outside').exists())
        for bad in ('../outside', '/tmp/x', 'C:\\x', 'CON', 'a' * 32 + '/x'):
            with self.subTest(bad=bad), self.assertRaises(core.Error):
                h.read_archive(self.repo, bad)

    def test_external_export_never_overwrites_and_source_roundtrips(self):
        archive_id, raw = self.archive()
        output = self.root / 'out.json'
        h.export_archive(self.repo, archive_id, output, 'source')
        self.assertEqual(output.read_bytes(), raw)
        with self.assertRaises(core.Error):
            h.export_archive(self.repo, archive_id, output, 'markdown')
        self.assertEqual(output.read_bytes(), raw)
        imported = h.import_file(self.repo, output, 'transcript-v1')
        self.assertEqual(h.read_archive(self.repo, imported['archive_id'])[1], raw)

    def test_limits_and_strict_transcript_schema(self):
        path = self.root / 'large.txt'
        path.write_bytes(b'x' * 50)
        with self.assertRaises(core.Error):
            bounded_read(path, 49)
        with self.assertRaises(core.Error):
            h.create_archive(self.repo, path.read_bytes(), 'text', tool='dsh', limit=49)
        bad_messages = [
            {'role': 'system', 'text': 'bad'},
            {'role': 'assistant', 'text': 3},
            {'role': 'user', 'text': 'x', 'phase': 'commentary'},
            {'role': 'assistant', 'text': 'x', 'phase': 'analysis'},
            {'role': 'user', 'text': 'x', 'tool_calls': []},
        ]
        for message in bad_messages:
            with self.subTest(message=message), self.assertRaises(core.Error):
                h.normalize(json_bytes(self.transcript(messages=[message])), 'transcript-v1')
        with self.assertRaises(core.Error):
            h.normalize(b'{"schema":1,"schema":2}', 'transcript-v1')

    def test_agy_success_is_always_partial_and_dsh_text_is_unstructured(self):
        raw = b'{"conversation_id":"ag-1","status":"SUCCESS","response":"Hi","usage":{"tokens":1},"extra":[1,2]}'
        result = h.create_archive(self.repo, raw, 'agy-result')
        record, stored, _, normalized = h.read_archive(self.repo, result['archive_id'])
        self.assertEqual(record['coverage'], 'partial')
        self.assertEqual(stored, raw)
        self.assertIn('agy-field:extra', normalized['omitted'])
        with self.assertRaises(core.Error):
            h.codex_restore(self.repo, result['archive_id'], self.cwd)
        text = h.create_archive(self.repo, b'User exported arbitrary text', 'text', tool='dsh')
        record, _, markdown, normalized = h.read_archive(self.repo, text['archive_id'])
        self.assertEqual(record['coverage'], 'user-supplied')
        self.assertEqual(normalized['messages'], [])
        self.assertIn(b'arbitrary text', markdown)
        self.assertEqual(h.capabilities()['dsh']['original_id_cross_device'], 'unsupported')

    def test_codex_export_pagination_fidelity_and_reimport(self):
        output = self.root / 'codex-export.json'
        result = h.codex_export(self.repo, 'explicit-thread', command_file=self.command, output=output, timeout=3)
        record, raw, _, normalized = h.read_archive(self.repo, result['archive_id'])
        self.assertEqual(record['coverage'], 'full-export')
        document = json.loads(raw)
        self.assertEqual(document['metadata']['thread']['extra'], {'verbatim': [1, False, None]})
        self.assertEqual(document['items_pages'][0]['result']['unknown_page_field'], [1, 2])
        self.assertEqual(document['items_pages'][1]['cursor'], 'page-2')
        self.assertEqual(normalized['messages'][1]['phase'], 'final_answer')
        self.assertEqual(normalized['omitted']['item:function_call'], 1)
        self.assertEqual(output.read_bytes(), raw)
        reimported = h.import_file(self.repo, output, 'codex-export-v1')
        self.assertEqual(h.read_archive(self.repo, reimported['archive_id'])[1], raw)
        requests = self.requests()
        methods = [r['method'] for r in requests if 'method' in r]
        self.assertEqual(methods, ['initialize', 'initialized', 'thread/read',
                                   'thread/turns/list', 'thread/items/list', 'thread/items/list'])
        for request in requests:
            if request.get('method', '').startswith('thread/'):
                self.assertEqual(request['params']['threadId'], 'explicit-thread')
        self.assertFalse(requests[2]['params']['includeTurns'])
        self.assertNotIn('turnId', requests[-1]['params'])

    def test_official_display_entries_export_and_restore(self):
        self.configure('display')
        result = h.codex_export(self.repo, 'explicit-thread', command_file=self.command, timeout=3)
        record, raw, _, normalized = h.read_archive(self.repo, result['archive_id'])
        self.assertEqual(record['coverage'], 'full-export')
        document = json.loads(raw)
        self.assertIsNone(document['items_pages'][0]['result']['data'][0]['completedAtMs'])
        self.assertEqual(document['items_pages'][1]['result']['backwardsCursor'], 'back')
        self.assertEqual(normalized['messages'], [
            {'role': 'user', 'text': '你好'},
            {'role': 'assistant', 'text': '你好，已收到', 'phase': 'final_answer'}])
        self.assertEqual(normalized['omitted']['display-metadata:turnId'], 2)
        h.codex_restore(self.repo, result['archive_id'], self.cwd, apply=True,
                        command_file=self.command, timeout=3)
        request = self.requests()[-1]
        self.assertEqual(request['method'], 'thread/inject_items')
        self.assertEqual(request['params']['items'], [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '你好'}]},
            {'type': 'message', 'role': 'assistant', 'phase': 'final_answer',
             'content': [{'type': 'output_text', 'text': '你好，已收到'}]}])

    def test_display_allowlist_rejects_semantics_not_just_unknown_types(self):
        from copy import deepcopy
        user = {'turnId': 't', 'item': {'type': 'userMessage', 'id': 'u',
                'content': [{'type': 'text', 'text': 'safe', 'text_elements': []}]}}
        agent = {'turnId': 't', 'item': {'type': 'agentMessage', 'id': 'a', 'text': 'answer'}}
        bad = []
        for block in ({'type': 'image', 'url': 'synthetic'},
                      {'type': 'skill', 'name': 's', 'path': 'synthetic'},
                      {'type': 'mention', 'name': 'm', 'path': 'synthetic'}):
            entry = deepcopy(user)
            entry['item']['content'].append(block)
            bad.append(entry)
        entry = deepcopy(user)
        entry['item']['content'][0]['text_elements'] = [{'byteRange': {'start': 0, 'end': 1}, 'placeholder': 'quote'}]
        bad.append(entry)
        for key, value in (
            ('memoryCitation', {'entries': [], 'threadIds': ['source']}),
            ('questions', [{'title': 'question', 'options': ['yes']}]),
            ('future', None), ('phase', 'partial_answer'), ('delivery', {'text': 'nested'}),
        ):
            entry = deepcopy(agent)
            entry['item'][key] = value
            bad.append(entry)
        bad.extend([
            {'item': user['item']},
            {**user, 'extra': {}},
            {**user, 'startedAtMs': True},
            {'turnId': 't', 'item': {'type': 'reasoning', 'id': 'r', 'summary': ['private']}},
            {'turnId': 't', 'item': {'type': 'functionCallOutput', 'id': 'f', 'name': 'tool', 'output': 'x'}},
            {'payload': user},
            {'turnId': 't', 'item': {'type': 'message', 'role': 'user',
                                     'content': [{'type': 'input_text', 'text': 'not a ThreadItem'}]}},
        ])
        for entry in bad:
            with self.subTest(entry=entry):
                original = deepcopy(entry)
                omitted = h.Counter()
                self.assertIsNone(h.convert_display_entry(entry, omitted))
                self.assertTrue(omitted)
                self.assertEqual(entry, original)
        safe = deepcopy(agent)
        safe['item'].update(delivery='async', questions=[], memoryCitation={'entries': [], 'threadIds': []})
        self.assertEqual(h.convert_display_entry(safe, h.Counter()), {'role': 'assistant', 'text': 'answer'})

    def test_export_loop_rpc_error_and_page_cap_create_no_archive(self):
        for mode in ('loop', 'error'):
            with self.subTest(mode=mode):
                self.configure(mode)
                with self.assertRaises(core.Error) as caught:
                    h.codex_export(self.repo, 'one-id', command_file=self.command, timeout=3)
                self.assertNotIn('SECRET_TOKEN', str(caught.exception))
                self.assertEqual(h.list_archives(self.repo), [])
        self.configure('normal')
        with self.assertRaisesRegex(core.Error, '分页上限'):
            h.codex_export(self.repo, 'one-id', command_file=self.command, max_pages=1, timeout=3)
        self.assertEqual(h.list_archives(self.repo), [])

    def test_incomplete_saved_export_rejected(self):
        document = {'schema': h.EXPORT_SCHEMA, 'thread_id': 't', 'captured_at': h.now(),
                    'metadata': {'thread': {'id': 't'}},
                    'turns_pages': [{'cursor': None, 'result': {'data': [], 'nextCursor': None}}],
                    'items_pages': [{'cursor': None, 'result': {'items': [], 'nextCursor': 'missing'}}]}
        with self.assertRaisesRegex(core.Error, '分页未完成'):
            h.create_archive(self.repo, json_bytes(document), 'codex-export-v1')

    def test_restore_defaults_to_plan_without_starting_or_resolving_command(self):
        archive_id, _ = self.archive()
        with mock.patch.object(h, 'RPC', side_effect=AssertionError('must not start')):
            with mock.patch.object(h, 'command_argv', side_effect=AssertionError('must not resolve')):
                plan = h.codex_restore(self.repo, archive_id, self.cwd, command_file='missing')
        self.assertEqual(plan['status'], 'plan')
        self.assertFalse(plan['full_native_restore'])
        self.assertFalse(plan['preserves_source_id'])
        self.assertEqual(plan['omitted']['raw-record:function_call'], 1)
        self.assertFalse((self.repo / '.agent-sync/history-restores.json').exists())
        self.assertFalse(self.log.exists())

    def test_apply_only_start_and_one_inject_with_local_mapping(self):
        archive_id, _ = self.archive()
        result = h.codex_restore(self.repo, archive_id, self.cwd, apply=True,
                                 command_file=self.command, timeout=3)
        self.assertEqual(result['new_thread_id'], 'new-local-thread')
        self.assertEqual(result['resume_command'], 'codex resume new-local-thread')
        requests = self.requests()
        self.assertEqual([r['method'] for r in requests],
                         ['initialize', 'initialized', 'thread/start', 'thread/inject_items'])
        self.assertEqual(requests[2]['params'], {'cwd': str(self.cwd)})
        items = requests[3]['params']['items']
        self.assertEqual([item['role'] for item in items], ['user', 'assistant'])
        self.assertEqual(items[1]['phase'], 'final_answer')
        mapping_raw = (self.repo / '.agent-sync/history-restores.json').read_text(encoding='utf-8')
        self.assertNotIn(str(self.cwd), mapping_raw)
        mapping = json.loads(mapping_raw)
        self.assertEqual([e['status'] for e in mapping['restores'][archive_id]], ['created', 'injected'])

    def test_allowlist_excludes_attachments_reasoning_tools_and_authority(self):
        records = [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'ok'}]},
            {'type': 'message', 'role': 'assistant', 'phase': 'commentary',
             'content': [{'type': 'output_text', 'text': 'working'}]},
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'do not partially inject'}, {'type': 'input_image', 'image_url': 'x'}]},
            {'type': 'message', 'role': 'developer', 'content': [{'type': 'input_text', 'text': 'authority'}]},
            {'type': 'function_call', 'arguments': 'danger'},
            {'type': 'reasoning', 'summary': []},
            {'type': 'unknownFutureType', 'data': 1},
            {'type': 'message', 'role': 'assistant', 'content': [
                {'type': 'output_text', 'text': 'citation', 'annotations': [{'url': 'x'}]}]},
        ]
        omitted = h.Counter()
        converted = [h.convert_response_item(item, omitted) for item in records]
        self.assertEqual(len([m for m in converted if m]), 2)
        self.assertEqual(omitted['message:excluded'], 2)
        self.assertEqual(omitted['message-detail:content:input_image'], 1)
        self.assertEqual(omitted['message:non-dialogue-role:developer'], 1)
        for kind in ('function_call', 'reasoning', 'unknownFutureType'):
            self.assertEqual(omitted['item:' + kind], 1)

    def test_empty_restore_never_creates_and_failure_keeps_thread_id(self):
        archive_id, _ = self.archive(self.transcript(messages=[]))
        with mock.patch.object(h, 'RPC', side_effect=AssertionError('must not start')):
            with self.assertRaisesRegex(core.Error, '无可注入'):
                h.codex_restore(self.repo, archive_id, self.cwd, apply=True)
        self.configure('inject-error')
        archive_id, _ = self.archive()
        with self.assertRaises(core.Error) as caught:
            h.codex_restore(self.repo, archive_id, self.cwd, apply=True,
                            command_file=self.command, timeout=3)
        self.assertIn('new-local-thread', str(caught.exception))
        self.assertNotIn('SECRET_TOKEN', str(caught.exception))
        self.assertEqual([r['method'] for r in self.requests()],
                         ['initialize', 'initialized', 'thread/start', 'thread/inject_items'])
        mapping = json.loads((self.repo / '.agent-sync/history-restores.json').read_bytes())
        self.assertEqual(mapping['restores'][archive_id][-1]['status'], 'injection_failed_or_unknown')

    def test_rpc_timeout_reaps_child_and_id_mismatch_fails(self):
        self.configure('timeout')
        rpc = RPC(self.argv, timeout=0.3)
        started = time.monotonic()
        with self.assertRaisesRegex(core.Error, '超时'):
            with rpc:
                self.fail('initialization should time out')
        self.assertIsNotNone(rpc.process.poll())
        self.assertFalse(rpc.reader.is_alive())
        self.assertLess(time.monotonic() - started, 8)
        self.configure('wrong-id')
        rpc = RPC(self.argv, timeout=3)
        with self.assertRaisesRegex(core.Error, 'ID 不匹配'):
            with rpc:
                pass
        self.assertIsNotNone(rpc.process.poll())

    def test_rpc_rejects_server_requests_and_command_file_in_repo(self):
        self.configure('approval')
        rpc = RPC(self.argv, timeout=3)
        with rpc:
            with self.assertRaisesRegex(core.Error, '已拒绝'):
                rpc.call('thread/read', {'threadId': 't', 'includeTurns': False})
            # Give the fake time to record the rejection before the context terminates it.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                rows = self.requests()
                if any(row.get('id') == 'approval-1' for row in rows):
                    break
                time.sleep(0.01)
        rejection = next(r for r in self.requests() if r.get('id') == 'approval-1')
        self.assertIn('error', rejection)
        self.assertNotIn('result', rejection)
        inside = self.repo / 'command.json'
        inside.write_text(json.dumps(self.argv), encoding='utf-8')
        with self.assertRaises(core.Error):
            command_argv(self.repo, inside)
        self.command.write_text('["codex.cmd", "app-server", "--stdio"]', encoding='utf-8')
        with self.assertRaisesRegex(core.Error, '包装'):
            command_argv(self.repo, self.command)


if __name__ == '__main__':
    unittest.main()
