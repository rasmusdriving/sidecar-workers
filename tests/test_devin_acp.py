import unittest
from unittest.mock import Mock
from runtime.acp.devin import DevinClient

class DevinProtocolTests(unittest.TestCase):
    def client(self):
        c = DevinClient(['devin', 'acp'], cwd='/tmp', model='swe-2-high', mode='ask')
        c._transport = Mock()
        return c

    def test_model_mismatch_fails_before_prompt(self):
        c = self.client()
        c._transport.send_request.return_value = {'sessionId':'s','configOptions':[{'id':'model','currentValue':'adaptive'}]}
        with self.assertRaisesRegex(ValueError, 'model'):
            c.new_session()

    def test_session_sets_and_verifies_mode(self):
        c = self.client()
        c._transport.send_request.side_effect = [
            {'sessionId':'s','configOptions':[{'id':'model','currentValue':'swe-2-high'}]},
            {'configOptions':[{'id':'mode','currentValue':'ask'}]},
        ]
        self.assertEqual(c.new_session(), 's')
        self.assertEqual(c._transport.send_request.call_args_list[0].kwargs['params'], {'cwd':str(__import__('pathlib').Path('/tmp').resolve()),'mcpServers':[]})

    def test_permission_requests_are_not_auto_approved(self):
        c = self.client()
        self.assertEqual(c.permission({'options':[{'optionId':'yes','kind':'allow_always'}]}), {'outcome':{'outcome':'cancelled'}})

    def test_write_uses_smart_mode(self):
        from runtime.execution_modes import execution_permissions
        self.assertEqual(execution_permissions('devin', 'write'), {'mode': 'smart'})
        DevinClient(['devin'], model='swe-2-high', mode='smart')

    def test_parent_can_approve_exact_pending_request(self):
        import tempfile, threading, time, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            c = self.client()
            c.permission_dir = Path(root)
            result = []
            thread = threading.Thread(target=lambda: result.append(c.permission({
                'sessionId':'s', 'toolCall':{'title':'npm install'},
                'options':[{'optionId':'once','kind':'allow_once'}]})))
            thread.start()
            deadline = time.monotonic() + 2
            requests = []
            while not requests and time.monotonic() < deadline:
                requests = list(Path(root).glob('*.request.json'))
                time.sleep(.01)
            self.assertTrue(requests, 'permission must become visible to the parent')
            request = json.loads(requests[0].read_text())
            Path(root, request['id']+'.decision.json').write_text(json.dumps({'optionId':'once'}))
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [{'outcome':{'outcome':'selected','optionId':'once'}}])
            self.assertEqual(json.loads(requests[0].read_text())['status'], 'approved')

    def test_invalid_or_denied_decision_never_approves(self):
        import tempfile, json
        from pathlib import Path
        from unittest.mock import patch
        for option in (None, 'always'):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as root:
                c = self.client()
                c.permission_dir = Path(root)
                Path(root, 'fixed.decision.json').write_text(json.dumps({'optionId':option}))
                with patch('runtime.acp.devin.uuid.uuid4', return_value=Mock(hex='fixed')):
                    outcome = c.permission({'sessionId':'s','options':[{'optionId':'always','kind':'allow_always'}]})
                self.assertEqual(outcome, {'outcome':{'outcome':'cancelled'}})
                self.assertTrue(c.denied)
                c._transport.send_notification.assert_called_once()

    def test_permission_timeout_cancels_turn(self):
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            c = self.client()
            c.permission_dir = Path(root)
            c.permission_timeout = 0
            self.assertEqual(c.permission({'sessionId':'s','toolCall':{'title':'install'}}), {'outcome':{'outcome':'cancelled'}})
            c._transport.send_notification.assert_called_once_with('session/cancel', {'sessionId':'s'})
            request = json.loads(next(Path(root).glob('*.request.json')).read_text())
            self.assertEqual(request['status'], 'denied')
            self.assertIn('Timed out', c.denied[0])

    def test_streamed_text_and_activity_are_preserved(self):
        import queue
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            c = DevinClient(['devin','acp'], cwd=root, stderr_path=root+'/devin.stderr.log', model='swe-2-max', mode='ask')
            c._transport = Mock()
            q = queue.Queue()
            c._transport._notifications = q
            def receive(timeout):
                try: return q.get(timeout=timeout)
                except queue.Empty: return None
            c._transport.receive_notification.side_effect = receive
            def prompt(*a, **kw):
                for chunk in ['Hello ', 'world']:
                    q.put({'method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':chunk}}}})
                return {'stopReason':'end_turn'}
            c._transport.send_request.side_effect = prompt
            c.prompt('s', 'hello')
            self.assertEqual(c.collect_text(), 'Hello world')
            self.assertEqual(Path(root+'/devin.stdout.log').read_text(), 'Hello world')
            self.assertEqual(len(Path(root+'/devin.stderr.events.jsonl').read_text().splitlines()), 2)

    def test_incomplete_turn_is_failure(self):
        c = self.client()
        c._transport.receive_notification.return_value = None
        c._transport._notifications.empty.return_value = True
        c._transport.send_request.return_value = {'stopReason':'max_tokens'}
        with self.assertRaisesRegex(RuntimeError, 'max_tokens'):
            c.prompt('s', 'hello')

if __name__ == '__main__': unittest.main()
