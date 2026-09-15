"""Exercise Sidecar's generated policy through the real invocation scheduler."""
import concurrent.futures
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sidecar.engine import command
from runtime.cli import build_parser, _resolve_config
from runtime.contracts import TaskRunRef, TaskStatus
from runtime.invocation_runtime import parse_invocations, run_invocation_workflow


class ProgressAdapter:
    def __init__(self, advance, finish_at=2000):
        self.advance = advance
        self.finish_at = finish_at
        self.elapsed = 0
        self.cancelled = False

    def run(self, task):
        self.raw = Path(task.metadata['artifact_root']) / task.task_id / 'raw'
        self.raw.mkdir(parents=True)
        return TaskRunRef(task.task_id, 'claude', 'fake', str(self.raw.parent), '')

    def poll(self, ref):
        self.elapsed += 1000
        (self.raw / 'claude.stdout.log').write_text('progress ' + str(self.elapsed))
        self.advance(1000)
        # Let the outer scheduler observe the same deadline as the invocation.
        time.sleep(.08)
        done = self.elapsed >= self.finish_at
        return TaskStatus(ref.task_id, 'claude', ref.run_id,
                          'SUCCEEDED' if done else 'STARTED', done, None, None)

    def cancel(self, ref):
        self.cancelled = True


class WorkerBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def policy(self):
        config = dict(provider='claude', model='claude-opus-5', repo=str(self.root),
                      mode='write', timeout=7200)
        cmd, _ = command(config, self.root, self.root)
        return _resolve_config(build_parser().parse_args(cmd[2:]), {}).policy

    def run_worker(self, policy, adapter, cancel=None):
        return run_invocation_workflow(
            invocations=parse_invocations(['claude:claude-opus-5'], ['.']),
            adapters={'claude': adapter}, repo_root=str(self.root), prompt='test',
            timeout_seconds=policy.stall_timeout_seconds,
            hard_timeout_seconds=policy.timeout_seconds,
            global_timeout_seconds=policy.review_hard_timeout_seconds or None,
            provider_permissions={}, allow_paths=['.'], cancel_event=cancel,
            poll_interval_seconds=.001)

    def test_review_default_reproduces_early_stop_and_generated_policy_completes(self):
        from dataclasses import replace
        for legacy in (True, False):
            clock = [0]
            def advance(seconds):
                clock[0] += seconds
            adapter = ProgressAdapter(advance, finish_at=3000)
            policy = self.policy()
            if legacy:
                policy = replace(policy, review_hard_timeout_seconds=1800)
            with patch('runtime.invocation_runtime.time', SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep)):
                result = self.run_worker(policy, adapter)
            self.assertEqual(result['outputs'][0]['status'], 'timeout' if legacy else 'success')
            self.assertEqual(adapter.cancelled, legacy)
            if legacy:
                self.assertIn(result['outputs'][0]['error'], ('task stopped while invocation was running',
                    "invocation 'claude-claude-opus-5' timed out"))

    def test_execution_limit_is_still_enforced(self):
        clock = [0]
        adapter = ProgressAdapter(lambda n: clock.__setitem__(0, clock[0] + n), finish_at=10000)
        with patch('runtime.invocation_runtime.time', SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep)):
            result = self.run_worker(self.policy(), adapter)
        self.assertEqual(result['outputs'][0]['status'], 'timeout')
        self.assertTrue(adapter.cancelled)

    def test_concurrent_workers_have_independent_cancellation(self):
        stop = threading.Event()
        stopped = ProgressAdapter(lambda n: stop.set(), finish_at=3000)
        survivor = ProgressAdapter(lambda n: None, finish_at=4000)
        policy = self.policy()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            a = pool.submit(self.run_worker, policy, stopped, stop)
            b = pool.submit(self.run_worker, policy, survivor)
            self.assertEqual(a.result()['outputs'][0]['status'], 'cancelled')
            self.assertEqual(b.result()['outputs'][0]['status'], 'success')
        self.assertTrue(stopped.cancelled)
        self.assertFalse(survivor.cancelled)

    def test_clarification_pause_does_not_consume_execution_budget(self):
        clock = [0]
        adapter = ProgressAdapter(lambda n: clock.__setitem__(0, clock[0] + n), finish_at=9000)
        with patch('runtime.invocation_runtime.time', SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep)), patch('runtime.coordinator.paused_seconds', return_value=3000):
            result = self.run_worker(self.policy(), adapter)
        self.assertEqual(result['outputs'][0]['status'], 'success')
