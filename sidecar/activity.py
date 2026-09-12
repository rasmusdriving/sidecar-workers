import json
from pathlib import Path
from .engine import setup
setup()
from runtime.message_stream import message_activity

def records(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text(errors='replace').splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            pass  # A streaming writer may still be finishing the last line.
    return result


def snapshot(job):
    meta = json.loads((job / 'job.json').read_text())
    meta.setdefault('provider', 'devin')
    items, calls = [], {}
    for path in sorted((job / 'artifacts').glob('run-*/provider-runs/*/raw/devin.stderr.events.jsonl')):
        for event in records(path):
            if event.get('type') == 'session_ready':
                meta['verified_model'] = event.get('model')
                meta['verified_mode'] = event.get('mode')
            update = event.get('params', {}).get('update', {})
            kind = update.get('sessionUpdate')
            if kind == 'agent_message_chunk':
                content = update.get('content', {})
                if content.get('type') == 'text':
                    if not items or items[-1]['type'] != 'message':
                        items.append({'type': 'message', 'text': ''})
                    items[-1]['text'] += content.get('text', '')
            elif kind in ('tool_call', 'tool_call_update'):
                key = update.get('toolCallId')
                if key not in calls:
                    calls[key] = {'type': 'tool', 'title': 'Tool activity', 'status': 'pending'}
                    items.append(calls[key])
                calls[key].update({k: v for k, v in update.items() if k in ('title', 'status', 'kind', 'rawInput', 'content', 'locations')})
    if meta['provider'] != 'devin':
        for path in sorted((job / 'artifacts').glob('run-*/provider-runs/*/raw/' + meta['provider'] + '.stdout.log')):
            parsed = message_activity(path.read_text(errors='replace'))
            items.extend(parsed['items'])
            if parsed['model']:
                meta['verified_model'] = parsed['model']
            if parsed['status'] == 'failed':
                meta['provider_error'] = parsed['final']
    events = records(job / 'stream.jsonl')
    for event in events:
        if event.get('type') == 'invocation_finished' and event.get('error'):
            meta['provider_error'] = event['error']
        if event.get('type') == 'task_finished':
            meta['result'] = event
    done = job / 'done.json'
    meta['status'] = 'running'
    if done.exists():
        meta.update(json.loads(done.read_text()))
    if not items:
        answer = ''.join(e.get('delta', '') for e in events if e.get('type') == 'output_delta')
        if answer:
            items.append({'type': 'message', 'text': answer})
    meta['permissions'] = [json.loads(p.read_text()) for p in sorted((job / 'permissions').glob('*.request.json'))]
    pending = [p for p in meta['permissions'] if p['status'] == 'pending']
    if pending and meta['status'] == 'running':
        meta['status'] = 'needs_attention'
        meta['error'] = 'Waiting for coordinating agent permission:\n' + '\n'.join(p['request'].get('toolCall', {}).get('_meta', {}).get('cognition.ai/editableCommand') or p['request'].get('toolCall', {}).get('title') or json.dumps(p['request'].get('toolCall', {}), ensure_ascii=False) for p in pending)
    meta['items'] = items
    if meta['status'] == 'failed':
        meta['error'] = meta.get('provider_error') or meta.get('error') or ((job / 'stderr.log').read_text(errors='replace')[-6000:] if (job / 'stderr.log').exists() else 'Worker exited without an error message')
    return meta
