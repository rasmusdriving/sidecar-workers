import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from sidecar.engine import setup, command
setup()
from sidecar.models import resolve_model
from sidecar.providers import login_command, provider_status
from runtime import coordinator, cli
from runtime.adapters.codex import CodexAdapter
from runtime.codex_stream import codex_activity
from runtime.contracts import TaskInput


class CodexTests(unittest.TestCase):
    def test_requested_models_and_efforts(self):
        for model, efforts in [('gpt-6-astra', ['low', 'medium', 'high', 'xhigh']),
                              ('gpt-5.6-sol', ['medium', 'high', 'xhigh'])]:
            for effort in efforts:
                self.assertEqual(resolve_model('codex', model, effort), (model, effort))
        self.assertEqual(resolve_model('codex', None, None), ('gpt-6-astra', 'medium'))
        self.assertEqual(resolve_model('codex', None, 'light'), ('gpt-6-astra', 'low'))
        for model, effort in [('gpt-5.6-sol', 'low'), ('gpt-6-astra', 'max'), ('unknown', 'high')]:
            with self.assertRaises(ValueError):
                resolve_model('codex', model, effort)

    def test_native_auth_status_and_login(self):
        self.assertEqual(login_command('codex', 'codex'), ['codex', 'login'])
        with patch('sidecar.providers.resolve_binary', return_value=sys.executable), \
             patch('sidecar.providers.subprocess.run') as run:
            run.return_value = Mock(returncode=0, stdout='', stderr='Logged in using ChatGPT')
            self.assertEqual(provider_status('codex')['status'], 'ready')
            self.assertEqual(run.call_args.args[0][-2:], ['login', 'status'])
            run.return_value = Mock(returncode=1, stdout='', stderr='Not logged in')
            self.assertEqual(provider_status('codex')['status'], 'needs_auth')
            run.return_value = Mock(returncode=1, stdout='', stderr='Invalid configuration')
            self.assertEqual(provider_status('codex')['status'], 'check_failed')

    def task(self, root, mode='read-only', model='gpt-6-astra', effort='low'):
        return TaskInput(task_id=uuid.uuid4().hex, prompt='initial', repo_root=str(root), target_paths=[],
                         metadata={'artifact_root': str(root / 'artifacts'), 'model': model, 'effort': effort,
                                   'provider_permissions': {'sandbox': mode, 'approval_policy': 'never'}})

    def test_launch_and_exact_resume_keep_model_effort_and_sandbox(self):
        adapter = CodexAdapter()
        sid = str(uuid.uuid4())
        with patch.dict(os.environ, SIDECAR_ALLOW_SUBAGENTS='0'):
            for mode in ['read-only', 'workspace-write']:
                task = self.task(Path.cwd(), mode)
                for cmd in [adapter._build_command(task), adapter._build_resume_command(task, sid)]:
                    self.assertEqual(cmd[cmd.index('--model') + 1], 'gpt-6-astra')
                    self.assertIn('model_reasoning_effort="low"', cmd)
                    self.assertEqual(cmd[cmd.index('--sandbox') + 1], mode)
                    self.assertLess(cmd.index('--sandbox'), cmd.index('exec'))
                    self.assertEqual(cmd[cmd.index('--ask-for-approval') + 1], 'never')
                    self.assertIn('multi_agent', cmd)
                    self.assertIn('multi_agent_v2', cmd)
                    self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', cmd)
                    self.assertNotIn('--last', cmd)
                    self.assertNotIn('--resume', cmd)
                resumed = adapter._build_resume_command(task, sid)
                self.assertEqual(resumed[resumed.index('exec') + 1], 'resume')
                self.assertEqual(resumed[-2], sid)
        with self.assertRaises(ValueError):
            adapter._build_resume_command(task, '--last')

    def test_stream_activity_updates_and_errors(self):
        sid = str(uuid.uuid4())
        events = [{'type': 'thread.started', 'thread_id': sid}, {'type': 'turn.started'},
                  {'type': 'item.started', 'item': {'id': '1', 'type': 'command_execution', 'command': 'git status'}},
                  {'type': 'item.completed', 'item': {'id': '1', 'type': 'command_execution', 'command': 'git status',
                                                     'status': 'completed', 'aggregated_output': 'clean'}},
                  {'type': 'item.completed', 'item': {'id': '2', 'type': 'agent_message', 'text': 'Done'}},
                  {'type': 'turn.completed'}]
        raw = '\n'.join(map(json.dumps, events))
        state = codex_activity(raw)
        self.assertEqual(state['session_id'], sid)
        self.assertIsNone(state['model'])
        self.assertEqual(len(state['items']), 2)
        self.assertEqual(state['items'][0]['status'], 'completed')
        self.assertEqual(state['last_answer'], 'Done')
        failed = raw + '\n' + json.dumps({'type': 'turn.failed', 'error': {'message': 'Out of quota'}})
        self.assertEqual(codex_activity(failed)['error'], 'Out of quota')
        self.assertFalse(CodexAdapter()._is_success(0, failed, ''))
        self.assertFalse(CodexAdapter()._is_success(1, raw, ''))

    def fake_provider(self, root, sid):
        script = root / 'fake_codex.py'
        script.write_text('''import json, sys
args = sys.argv[1:]
assert args[args.index('--model') + 1] == 'gpt-6-astra'
assert 'model_reasoning_effort="low"' in args
assert args[args.index('--sandbox') + 1] == 'read-only'
assert args[args.index('--ask-for-approval') + 1] == 'never'
assert '--last' not in args and '--resume' not in args
sid = ''' + repr(sid) + '''
if args[args.index('exec') + 1] == 'resume':
    assert args[-2] == sid
    assert 'Use UTC' in args[-1]
    answer = 'Finished in same conversation'
else:
    answer = json.dumps({'sidecar_question': 'Which timezone?'})
for event in [{'type':'thread.started','thread_id':sid}, {'type':'turn.started'},
              {'type':'item.completed','item':{'id':'0','type':'agent_message','text':answer}},
              {'type':'turn.completed'}]:
    print(json.dumps(event), flush=True)
''', encoding='utf-8')
        return script

    def test_real_process_question_reply_resumes_exact_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sid = str(uuid.uuid4())
            script = self.fake_provider(root, sid)
            adapter = CodexAdapter()
            original = adapter._command
            with patch.dict(os.environ, MCO_QUESTION_DIR=str(root / 'questions')), \
                 patch.object(adapter, '_command', side_effect=lambda task, session_id=None:
                              [sys.executable, str(script)] + original(task, session_id)[1:]):
                ref = adapter.run(self.task(root))
                self.addCleanup(adapter.cancel, ref)
                deadline = time.monotonic() + 10
                answered = False
                while time.monotonic() < deadline:
                    status = adapter.poll(ref)
                    for path in (root / 'questions').glob('*.request.json'):
                        request = json.loads(path.read_text(encoding='utf-8'))
                        if request['status'] == 'pending':
                            self.assertEqual(request['session_id'], sid)
                            coordinator.save(path.with_name(request['id'] + '.reply.json'),
                                             {'answer': 'Use UTC', 'created': time.time()})
                            answered = True
                    if status.completed:
                        break
                    time.sleep(.02)
                self.assertTrue(answered)
                self.assertTrue(status.completed)
                self.assertEqual(status.attempt_state, 'SUCCEEDED', status.message)
                state = codex_activity(Path(ref.artifact_path, 'raw/codex.stdout.log').read_text(encoding='utf-8'))
                self.assertEqual(state['last_answer'], 'Finished in same conversation')
                self.assertEqual(state['session_id'], sid)

    def test_sidecar_engine_passes_effort_through_real_runtime(self):
        # Exercise engine arguments, policy filtering and invocation dispatch, not a dry run.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job = root / 'job'
            job.mkdir()
            (job / 'prompt.txt').write_text('initial', encoding='utf-8')
            script = root / 'record.py'
            script.write_text('''import json,sys
args=sys.argv[1:]
assert 'model_reasoning_effort="xhigh"' in args, args
assert args[args.index('--model')+1]=='gpt-5.6-sol', args
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Effort received'}}))
print(json.dumps({'type':'turn.completed'}))
''', encoding='utf-8')
            config = dict(provider='codex', model='gpt-5.6-sol', effort='xhigh', repo=str(root), mode='read_only', timeout=20)
            args, env = command(config, job, root)
            original = CodexAdapter._build_command
            stdout = io.StringIO()
            with patch.dict(os.environ, env), patch('runtime.cli.discover_models', return_value={'ok': False}), \
                 patch.object(CodexAdapter, '_build_command',
                              lambda adapter, task: [sys.executable, str(script)] + original(adapter, task)[1:]), \
                 contextlib.redirect_stdout(stdout):
                result = cli.main(args[2:])
            self.assertEqual(result, 0, stdout.getvalue())
            self.assertIn('Effort received', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
