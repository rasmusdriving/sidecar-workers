"""Codex exec JSONL activity and exact-session clarification metadata."""
import json


def codex_activity(raw):
    items, current = [], {}
    session_id = model = last_answer = error = None
    status = 'running'
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind == 'thread.started':
            session_id = event.get('thread_id')
            model = event.get('model') or model
            current = {}
        elif kind == 'turn.started':
            current = {}
            status, error, last_answer = 'running', None, None
        elif kind == 'turn.completed':
            status, error = 'succeeded', None
        elif kind in ('error', 'turn.failed', 'response.failed'):
            status = 'failed'
            detail = event.get('error', event.get('message', 'Codex turn failed'))
            error = detail.get('message', str(detail)) if isinstance(detail, dict) else str(detail)
        if kind not in ('item.started', 'item.updated', 'item.completed'):
            continue
        item = event.get('item', {})
        if not isinstance(item, dict):
            continue
        item_type = item.get('type')
        key = item.get('id', str(len(items)))
        if item_type in ('agent_message', 'assistant_message', 'message'):
            text = item.get('text', '')
            if key not in current:
                current[key] = {'type': 'message', 'text': text}
                items.append(current[key])
            else:
                current[key]['text'] = text
            if kind == 'item.completed':
                last_answer = text
        elif item_type in ('command_execution', 'file_change', 'mcp_tool_call', 'web_search', 'collab_tool_call'):
            if key not in current:
                current[key] = {'type': 'tool'}
                items.append(current[key])
            tool = current[key]
            tool.update(title=item.get('command') or item.get('tool') or item_type.replace('_', ' '),
                        kind='execute' if item_type == 'command_execution' else 'edit' if item_type == 'file_change' else 'other',
                        status=item.get('status') or ('completed' if kind == 'item.completed' else 'in_progress'),
                        rawInput=({'command': item.get('command', '')} if item_type == 'command_execution'
                                  else item.get('arguments', item.get('changes', {}))))
            if item.get('aggregated_output'):
                tool['content'] = [{'content': {'type': 'text', 'text': item['aggregated_output']}}]
    return {'items': items, 'session_id': session_id, 'model': model, 'last_answer': last_answer,
            'final': last_answer, 'status': status, 'error': error}
