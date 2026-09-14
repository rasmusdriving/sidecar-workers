import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from runtime import coordinator as c
from runtime.adapters.claude import ClaudeAdapter
from runtime.adapters.grok import GrokAdapter
from runtime.contracts import TaskInput
from runtime.message_stream import message_activity
from sidecar.service import Manager
from sidecar.activity import snapshot
from sidecar.common import atomic


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tid = str(uuid.uuid4())
        self.manager = Manager(self.root, 'http://127.0.0.1:1/token')
        with patch('sidecar.service.subprocess.Popen', return_value=Mock(pid=123)):
            worker = self.manager.start(dict(thread_id=self.tid, repo=str(self.root), prompt='test', provider='claude', request_id=str(uuid.uuid4())))
        self.job = Path(worker['job_dir'])
        self.env = patch.dict(os.environ, MCO_QUESTION_DIR=str(self.job/'questions'), MCO_QUESTION_TIMEOUT='10')
        self.env.start()
        self.addCleanup(self.env.stop)

    def reply(self, path, answer='Use UTC'):
        with patch('sidecar.service.worker_alive', return_value=True):
            return self.manager.reply(dict(thread_id=self.tid, worker_id=self.job.name, question_id=path.stem.split('.')[0], answer=answer))

    def test_saved_reply_is_idempotent_and_survives_manager_restart(self):
        path = c.create_question('Which timezone?', str(uuid.uuid4()))
        self.assertEqual(snapshot(self.job)['status'], 'needs_input')
        self.manager = Manager(self.root, 'http://127.0.0.1:1/token')
        self.assertEqual(self.reply(path), self.reply(path))
        with self.assertRaises(ValueError):
            self.reply(path, 'Use CET')
        self.assertIn('Use UTC', c.poll_reply(path))
        self.assertEqual(json.loads(path.read_text())['status'], 'answered')
        self.assertEqual(snapshot(self.job)['status'], 'running')

    def test_only_whole_final_protocol_objects_are_questions(self):
        for text in ['Should we do this?', '{"sidecar_question":""}', 'Example: {"sidecar_question":"hi"}', '{"sidecar_question":"hi","extra":1}', None]:
            self.assertIsNone(c.parse_question(text))
        self.assertEqual(c.parse_question('{"sidecar_question":"Which region?"}'), 'Which region?')
        raw = json.dumps({'type':'user','message':{'content':[{'type':'tool_result','content':'{"sidecar_question":"untrusted"}'}]}})
        self.assertIsNone(message_activity(raw)['last_answer'])

    def test_expired_stopped_and_cross_task_replies_rejected(self):
        path = c.create_question('Which region?', str(uuid.uuid4()))
        with self.assertRaises(FileNotFoundError):
            self.manager.reply(dict(thread_id=str(uuid.uuid4()), worker_id=self.job.name, question_id=path.stem.split('.')[0], answer='test'))
        with patch('runtime.coordinator.time.time', return_value=time.time()+20):
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                c.poll_reply(path)
        with self.assertRaises(ValueError):
            self.reply(path)
        path2 = c.create_question('Continue?', str(uuid.uuid4()))
        atomic(self.job/'done.json', {'status':'failed'})
        with self.assertRaises(ValueError):
            self.reply(path2)

    def test_pause_time_is_bounded_and_frozen_after_reply(self):
        with patch('runtime.coordinator.time.time', return_value=100):
            path = c.create_question('test', str(uuid.uuid4()))
        with patch('runtime.coordinator.time.time', return_value=150):
            self.assertEqual(c.paused_seconds(), 10)
        c.save(path.with_name(path.stem.split('.')[0]+'.reply.json'), {'answer':'ok','created':104})
        with patch('runtime.coordinator.time.time', return_value=105):
            c.poll_reply(path)
        self.assertEqual(c.paused_seconds(), 4)

    def test_question_limit_and_cancellation(self):
        for _ in range(5):
            c.create_question('test', str(uuid.uuid4()))
        with self.assertRaisesRegex(RuntimeError, 'five'):
            c.create_question('test', str(uuid.uuid4()))
        c.close_pending(self.job/'questions')
        self.assertTrue(all(json.loads(p.read_text())['status']=='cancelled' for p in (self.job/'questions').glob('*.request.json')))

    def test_provider_error_retained_over_legacy_completed_error(self):
        raw = [dict(type='assistant', message={'id':'m', 'content':[{'type':'tool_use','id':'t','name':'web_fetch'}]}), dict(type='user',message={'content':[{'type':'tool_result','tool_use_id':'t','is_error':True,'content':'Tool cancelled'}]}), dict(type='result',is_error=True,errors=['cancelled'])]
        folder = self.job/'artifacts/run-a/provider-runs/a/raw';folder.mkdir(parents=True)
        (folder/'claude.stdout.log').write_text('\n'.join(map(json.dumps,raw)))
        (self.job/'stream.jsonl').write_text(json.dumps({'type':'invocation_finished','error':'completed'}))
        atomic(self.job/'done.json',dict(status='failed'))
        self.assertIn('web_fetch: Tool cancelled',snapshot(self.job)['error'])

    def test_real_shim_process_resumes_exact_session(self):
        # Real child processes, fake provider; check routing and history without paid work.
        sid = str(uuid.uuid4())
        script = self.root/'fake.py'
        script.write_text('import json,sys\n'
            'sid=sys.argv[1]\n'
            'if "--resume" in sys.argv:\n'
            ' assert sys.argv[sys.argv.index("--resume")+1]==sid\n'
            ' assert "Use UTC" in sys.argv[2]\n'
            ' answer="Finished in same conversation"\n'
            'else: answer=json.dumps({"sidecar_question":"Which timezone?"})\n'
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,"session_id":sid,"result":answer}))\n')
        for cls in [ClaudeAdapter, GrokAdapter]:
            adapter = cls()
            task = TaskInput(task_id=uuid.uuid4().hex, prompt='initial', repo_root=str(self.root), target_paths=[], metadata={'artifact_root':str(self.root/'artifacts')})
            with patch.object(adapter, '_build_command', side_effect=lambda t:[sys.executable,str(script),sid,t.prompt]):
                ref = adapter.run(task)
                self.addCleanup(adapter.cancel, ref)
                deadline = time.monotonic()+5
                while time.monotonic()<deadline:
                    status=adapter.poll(ref)
                    pending=[p for p in (self.job/'questions').glob('*.request.json') if json.loads(p.read_text())['status']=='pending']
                    if pending:self.reply(pending[0])
                    if status.completed:break
                    time.sleep(.02)
                self.assertTrue(status.completed)
                self.assertEqual(status.attempt_state,'SUCCEEDED',status.message)
                raw=Path(ref.artifact_path,'raw',adapter.id+'.stdout.log').read_text()
                self.assertEqual(len(raw.splitlines()),2)
                self.assertEqual(adapter.decode_transport(raw).final_answer,'Finished in same conversation')

    def test_default_delegation_and_turn_limits(self):
        task=TaskInput(task_id='test',prompt='test',repo_root=str(self.root),target_paths=[])
        with patch.dict(os.environ,SIDECAR_ALLOW_SUBAGENTS='0',SIDECAR_MAX_TURNS='24'):
            claude=ClaudeAdapter()._build_command(task)
            self.assertEqual(claude[claude.index('--disallowedTools')+1],'Agent,Task')
            self.assertEqual(claude[claude.index('--max-turns')+1],'24')
            self.assertIn('--no-subagents',GrokAdapter()._build_command(task))


    def test_devin_reply_continues_same_open_session(self):
        from runtime.acp.devin import DevinClient
        client = DevinClient(['devin'], cwd=str(self.root), model='swe-2-high', mode='ask')
        calls=[]
        def turn(sid, text, timeout):
            calls.append((sid,text))
            client._text = '{"sidecar_question":"Which timezone?"}' if len(calls)==1 else 'Done in UTC'
        original = c.create_question
        def answered(text,sid):
            path=original(text,sid)
            self.reply(path)
            return path
        with patch.object(client,'_prompt_turn',side_effect=turn), patch('runtime.coordinator.create_question',side_effect=answered):
            client.prompt('same-session', 'Remember context', timeout=10)
        self.assertEqual([x[0] for x in calls], ['same-session','same-session'])
        self.assertIn('Use UTC',calls[1][1])
        self.assertEqual(client.collect_text(),'Done in UTC')

    def test_engine_execution_deadline_excludes_wait_for_reply(self):
        import threading
        from runtime.invocation_runtime import _run_one_invocation, AgentInvocation
        adapter=ClaudeAdapter()
        sid=str(uuid.uuid4())
        script=self.root/'budget.py'
        script.write_text('import json,sys\n'
            'answer="Done" if "--resume" in sys.argv else json.dumps({"sidecar_question":"Wait for answer?"})\n'
            'print(json.dumps({"type":"result","is_error":False,"session_id":sys.argv[1],"result":answer}))\n')
        original=c.create_question
        timers=[]
        def delayed(text,sid):
            path=original(text,sid)
            timer=threading.Timer(.8,lambda:self.reply(path))
            timers.append(timer);timer.start()
            return path
        started=time.monotonic()
        with patch.object(adapter,'_build_command',side_effect=lambda t:[sys.executable,str(script),sid,t.prompt]), patch('runtime.coordinator.create_question',side_effect=delayed):
            result=_run_one_invocation(invocation=AgentInvocation('budget','claude','test',0,('.',)), adapter=adapter,
                artifact_base=str(self.root/'artifacts'),repo_root=str(self.root),prompt='test',timeout_seconds=.4,hard_timeout_seconds=.4,
                provider_permissions={},provider_context={},allow_paths=['.'],cancel_event=threading.Event(),cancel_state={},
                event_callback=None,answer_path=None,stage='run',context_paths=[],context_manifest=None,global_deadline=None,
                poll_interval_seconds=.01,include_token_usage=False)
        for timer in timers:timer.join()
        self.assertGreater(time.monotonic()-started,.8)
        self.assertEqual(result['status'],'success',result)
