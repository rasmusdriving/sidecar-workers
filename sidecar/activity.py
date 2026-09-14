import json
from pathlib import Path
from .engine import setup
setup()
from runtime.message_stream import message_activity
from runtime.coordinator import parse_question, paused_seconds

def records(path):
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            pass  # A streaming writer may still be finishing the last line.
    return result


def snapshot(job):
    meta = json.loads((job / 'job.json').read_text(encoding='utf-8'))
    meta.setdefault('provider', 'devin')
    items, calls = [], {}
    for path in sorted((job / 'artifacts').glob('run-*/provider-runs/*/raw/devin.stderr.events.jsonl')):
        for event in records(path):
            if event.get('type') == 'turn_started' and items and items[-1]['type'] == 'message':
                items.append({'type':'message','text':''})
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
            parsed = message_activity(path.read_text(encoding='utf-8', errors='replace'))
            items.extend(parsed['items'])
            if parsed['model']:
                meta['verified_model'] = parsed['model']
            if parsed['status'] == 'failed':
                meta['provider_error'] = parsed['error'] or parsed['final']
    events = records(job / 'stream.jsonl')
    for event in events:
        if event.get('type') == 'invocation_finished' and event.get('error'):
            if event['error'] != 'completed' and not meta.get('provider_error'):
                meta['provider_error'] = event['error']
        if event.get('type') == 'task_finished':
            meta['result'] = event
    done = job / 'done.json'
    meta['status'] = 'running'
    if done.exists():
        meta.update(json.loads(done.read_text(encoding='utf-8')))
    if not items:
        answer = ''.join(e.get('delta', '') for e in events if e.get('type') == 'output_delta')
        if answer:
            items.append({'type': 'message', 'text': answer})
    meta['permissions'] = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((job / 'permissions').glob('*.request.json'))]
    pending = [p for p in meta['permissions'] if p['status'] == 'pending']
    if pending and meta['status'] == 'running':
        meta['status'] = 'needs_attention'
        meta['error'] = 'Waiting for coordinating agent permission:\n' + '\n'.join(p['request'].get('toolCall', {}).get('_meta', {}).get('cognition.ai/editableCommand') or p['request'].get('toolCall', {}).get('title') or json.dumps(p['request'].get('toolCall', {}), ensure_ascii=False) for p in pending)
    meta['questions'] = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((job / 'questions').glob('*.request.json'))]
    unanswered = [q for q in meta['questions'] if q['status'] == 'pending']
    if unanswered and meta['status'] == 'running':
        meta['status'] = 'needs_input'
        meta['error'] = 'Waiting for coordinating agent reply:\n' + '\n'.join(q['question'] for q in unanswered)
    meta['items'] = [i for i in items if i.get('type') != 'message' or (i.get('text') and not parse_question(i['text']))]
    meta['paused_seconds'] = paused_seconds(job / 'questions')
    meta['waiting_since'] = unanswered[0]['created'] if unanswered and meta['status'] == 'needs_input' else None
    if meta['status'] == 'failed':
        meta['error'] = meta.get('provider_error') or meta.get('error') or ((job / 'stderr.log').read_text(encoding='utf-8', errors='replace')[-6000:] if (job / 'stderr.log').exists() else 'Worker exited without an error message')
    return meta
