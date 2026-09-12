"""Normalize Claude Code and Grok Messages JSON streams for answers and activity."""
from __future__ import annotations
import json
from .answer_transport import AnswerDelta, AnswerTransport, decode_plain_text


def message_activity(raw):
    items, slots, calls = [], {}, {}
    current = 'initial'
    model = None
    status = 'running'
    final = None
    recognized = False

    def block(key, content, replace=False):
        kind = content.get('type')
        if kind == 'text':
            if key not in slots:
                slots[key] = {'type': 'message', 'text': ''}
                items.append(slots[key])
            if replace or content.get('text'):
                slots[key]['text'] = content.get('text', '')
        elif kind == 'tool_use':
            call_id = content.get('id', str(key))
            if call_id not in calls:
                name = content.get('name', 'Tool')
                lower = name.lower()
                tool_kind = 'read' if any(x in lower for x in ['read', 'glob', 'grep', 'search']) else 'edit' if any(x in lower for x in ['edit', 'write', 'patch']) else 'execute' if any(x in lower for x in ['bash', 'shell', 'command']) else 'other'
                calls[call_id] = {'type': 'tool', 'title': name, 'kind': tool_kind, 'status': 'in_progress'}
                items.append(calls[call_id])
            slots[key] = calls[call_id]
            if content.get('input'):
                calls[call_id]['rawInput'] = content['input']

    for line in raw.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        kind = e.get('type')
        recognized |= kind in ('system', 'stream_event', 'assistant', 'user', 'result')
        if kind == 'system' and e.get('model'):
            model = e['model']
        if kind == 'stream_event':
            event = e.get('event', {})
            event_kind = event.get('type')
            if event_kind == 'message_start':
                current = event.get('message', {}).get('id') or str(len(items))
                model = event.get('message', {}).get('model') or model
            key = (current, event.get('index', 0))
            if event_kind == 'content_block_start':
                block(key, event.get('content_block', {}))
            elif event_kind == 'content_block_delta':
                delta = event.get('delta', {})
                if delta.get('type') == 'text_delta':
                    if key not in slots:
                        block(key, {'type': 'text'})
                    slots[key]['text'] += delta.get('text', '')
                elif delta.get('type') == 'input_json_delta' and key in slots:
                    item = slots[key]
                    item['_input'] = item.get('_input', '') + delta.get('partial_json', '')
                    try:
                        item['rawInput'] = json.loads(item['_input'])
                    except ValueError:
                        pass
        elif kind == 'assistant':
            message = e.get('message', {})
            mid = message.get('id', current)
            model = message.get('model') or model
            for i, content in enumerate(message.get('content', [])):
                # A completed text block may accompany the same streamed message.
                if content.get('type') == 'text' and any(k[0] == mid and v.get('text') == content.get('text') for k, v in slots.items()):
                    continue
                block((mid, i), content, replace=True)
        elif kind == 'user':
            for content in e.get('message', {}).get('content', []):
                if content.get('type') == 'tool_result':
                    call = calls.get(content.get('tool_use_id'))
                    if call:
                        call['status'] = 'failed' if content.get('is_error') else 'completed'
                        output = content.get('content', '')
                        if isinstance(output, str):
                            try:
                                decoded = json.loads(output)
                                if isinstance(decoded, dict) and isinstance(decoded.get('FileContent'), dict):
                                    output = decoded['FileContent'].get('raw_output', output)
                            except ValueError:
                                pass
                        call['content'] = [{'type': 'content', 'content': {'type': 'text', 'text': output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)}}]
        elif kind == 'result':
            status = 'failed' if e.get('is_error') or str(e.get('subtype', '')).startswith('error') else 'succeeded'
            final = e.get('result')
            if not final and e.get('errors'):
                final = '\n'.join(map(str, e['errors']))
    for item in items:
        item.pop('_input', None)
    return {'items': items, 'model': model, 'status': status, 'final': final, 'recognized': recognized}


def decode_messages(raw):
    state = message_activity(raw)
    if not state['recognized']:
        return decode_plain_text(raw)
    texts = [i['text'] for i in state['items'] if i['type'] == 'message']
    answer = state['final'] if isinstance(state['final'], str) else '\n\n'.join(texts)
    return AnswerTransport(tuple(AnswerDelta(t) for t in texts) or ((AnswerDelta(answer),) if answer else ()), answer, state['status'], None)
