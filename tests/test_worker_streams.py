import json
import unittest
from runtime.adapters.claude import ClaudeAdapter
from runtime.adapters.grok import GrokAdapter
from runtime.cli import _parse_provider_models_json
from runtime.contracts import TaskInput


def task(model, effort):
    return TaskInput(task_id='test',prompt='Read a file',repo_root='/tmp',target_paths=['.'],timeout_seconds=60,metadata={'model':model,'effort':effort})

class WorkerStreams(unittest.TestCase):
    def test_claude_model_and_effort_applied(self):
        for model in ['claude-fable-5-1','claude-opus-5']:
            for effort in ['medium','high','xhigh']:
                cmd=ClaudeAdapter()._build_command(task(model,effort))
                self.assertEqual(cmd[cmd.index('--model')+1],model)
                self.assertEqual(cmd[cmd.index('--effort')+1],effort)
                self.assertIn('stream-json',cmd)

    def test_grok_stream_and_effort(self):
        cmd=GrokAdapter()._build_command(task('grok-4.6','high'))
        self.assertNotIn('--no-auto-update',cmd)
        self.assertIn('streaming-messages-json',cmd)
        self.assertEqual(cmd[cmd.index('--reasoning-effort')+1],'high')

    def test_effort_policy_is_accepted(self):
        self.assertEqual(_parse_provider_models_json('{"claude":{"effort":"xhigh"}}')['claude']['effort'],'xhigh')

    def test_stream_answer_is_not_duplicated(self):
        records=[
            {'type':'stream_event','event':{'type':'message_start','message':{'id':'m1'}}},
            {'type':'stream_event','event':{'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}},
            {'type':'stream_event','event':{'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'Hello'}}},
            {'type':'assistant','message':{'id':'m1','content':[{'type':'text','text':'Hello'}]}},
            {'type':'result','subtype':'success','result':'Hello','is_error':False},
        ]
        for adapter in [ClaudeAdapter(),GrokAdapter()]:
            result=adapter.decode_transport('\n'.join(map(json.dumps,records)))
            self.assertEqual(result.final_answer,'Hello')
            self.assertEqual(''.join(d.text for d in result.deltas),'Hello')

    def test_provider_error_overrides_zero_exit(self):
        raw=json.dumps({'type':'result','is_error':True,'result':'Model unavailable'})
        for adapter in [ClaudeAdapter(),GrokAdapter()]:
            self.assertFalse(adapter._is_success(0,raw,''))

    def test_tool_result_is_matched_and_readable(self):
        from runtime.message_stream import message_activity
        events = [
            {'type':'assistant','message':{'id':'m','content':[{'type':'tool_use','id':'t','name':'read_file','input':{'target_file':'numbers.txt'}}]}},
            {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'t','content':json.dumps({'FileContent':{'raw_output':'17\n25\n8'}})}]}},
        ]
        activity = message_activity('\n'.join(map(json.dumps,events)))['items']
        self.assertEqual(activity[0]['status'],'completed')
        self.assertEqual(activity[0]['content'][0]['content']['text'],'17\n25\n8')
