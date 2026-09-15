import concurrent.futures
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from sidecar.activity import COMPACT_BYTES, read_activity, records
from sidecar.common import atomic
from sidecar.service import thread_state


class CompactStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tid = str(uuid.uuid4())
        self.wid = 'devin-' + uuid.uuid4().hex
        self.job = self.root / 'threads' / self.tid / self.wid
        self.job.mkdir(parents=True)
        atomic(self.job / 'job.json', {'thread_id': self.tid, 'prompt': 'private prompt'})
        atomic(self.job / 'done.json', {'status': 'complete'})
        self.raw = self.job / 'artifacts/run-test/provider-runs/test/raw'
        self.raw.mkdir(parents=True)
        old = json.dumps({'params': {'update': {'sessionUpdate': 'agent_message_chunk',
                          'content': {'type': 'text', 'text': 'old' * COMPACT_BYTES}}}})
        recent = json.dumps({'params': {'update': {'sessionUpdate': 'agent_message_chunk',
                            'content': {'type': 'text', 'text': 'latest answer'}}}})
        self.log = self.raw / 'devin.stderr.events.jsonl'
        header = json.dumps({'type': 'session_ready', 'model': 'swe-2-max', 'mode': 'smart'})
        self.log.write_text(header + '\n' + old + '\n' + recent + '\n')

    def test_compact_is_bounded_and_full_history_is_preserved(self):
        self.assertLessEqual(len(read_activity(self.log, True).encode()), COMPACT_BYTES)
        state = thread_state(self.root, self.tid, compact=True)
        worker = state['workers'][0]
        self.assertEqual(worker['status'], 'complete')
        self.assertEqual(worker['verified_model'], 'swe-2-max')
        self.assertEqual(worker['verified_mode'], 'smart')
        self.assertEqual(worker['latest_message'], 'latest answer')
        self.assertNotIn('prompt', worker)
        self.assertNotIn('items', worker)
        full = thread_state(self.root, self.tid)['workers'][0]
        self.assertIn('old' * COMPACT_BYTES, full['items'][0]['text'])

    def test_worker_filter_never_reads_other_worker_or_task(self):
        other = self.job.parent / ('devin-' + uuid.uuid4().hex)
        other.mkdir()
        (other / 'job.json').write_text('corrupt unrelated history')
        self.assertEqual(len(thread_state(self.root, self.tid, self.wid, True)['workers']), 1)
        with self.assertRaises(FileNotFoundError):
            thread_state(self.root, str(uuid.uuid4()), self.wid, True)
        with self.assertRaises(ValueError):
            thread_state(self.root, self.tid, '../' + self.wid, True)

    def test_concurrent_status_requests_preserve_pending_permissions_and_failures(self):
        atomic(self.job / 'done.json', {'status': 'failed'})
        atomic(self.job / 'stream.jsonl', {'type': 'invocation_finished', 'error': 'permission expired'})
        folder = self.job / 'permissions'
        folder.mkdir()
        atomic(folder / 'a.request.json', {'status': 'pending', 'request': {}})
        atomic(folder / 'b.request.json', {'status': 'answered', 'request': {}})
        with concurrent.futures.ThreadPoolExecutor() as pool:
            states = list(pool.map(lambda _: thread_state(self.root, self.tid, self.wid, True), range(4)))
        for state in states:
            worker = state['workers'][0]
            self.assertEqual(worker['error'], 'permission expired')
            self.assertEqual(len(worker['permissions']), 1)

    def test_partial_record_at_each_boundary_is_ignored(self):
        self.log.write_text('x' * COMPACT_BYTES + '\n{"type":"ok"}\n{"type":')
        self.assertEqual(records(self.log, True), [{'type': 'ok'}])
