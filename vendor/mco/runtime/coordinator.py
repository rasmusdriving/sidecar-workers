"""Opt-in, disk-backed clarification protocol for Sidecar workers.

Only a whole final answer matching the protocol creates a question. Tool output,
partial deltas and ordinary prose never create requests. No provider credentials
or global provider configuration are involved.
"""
import json
import os
import time
import uuid
from pathlib import Path


def directory():
    value = os.environ.get('MCO_QUESTION_DIR')
    return Path(value) if value else None


def save(path, value):
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value), encoding='utf-8')
    temp.replace(path)


def parse_question(answer):
    try:
        value = json.loads(answer.strip())
    except (ValueError, AttributeError):
        return None
    if not isinstance(value, dict) or set(value) != {'sidecar_question'}:
        return None
    text = value['sidecar_question']
    return text.strip() if isinstance(text, str) and 0 < len(text.strip()) <= 4000 else None


def create_question(text, session_id):
    folder = directory()
    if folder is None:
        raise RuntimeError('No coordinating question queue configured')
    folder.mkdir(parents=True, exist_ok=True)
    if len(list(folder.glob('*.request.json'))) >= 5:
        raise RuntimeError('Worker reached the limit of five clarification questions')
    rid = uuid.uuid4().hex
    now = time.time()
    request = dict(id=rid, question=text, session_id=session_id, status='pending',
                   created=now, deadline=now + int(os.environ.get('MCO_QUESTION_TIMEOUT', '600')))
    path = folder / (rid + '.request.json')
    save(path, request)
    return path


def poll_reply(path):
    request = json.loads(path.read_text())
    reply = path.with_name(request['id'] + '.reply.json')
    if reply.exists():
        value = json.loads(reply.read_text())
        if value['created'] <= request['deadline']:
            request.update(status='answered', finished=value['created'], answer=value['answer'])
            save(path, request)
            return 'The coordinating agent replied to your clarification question:\n' + value['answer'] + '\nContinue the original task within its existing scope.'
    if time.time() >= request['deadline']:
        request.update(status='expired', finished=request['deadline'])
        save(path, request)
        raise RuntimeError('Timed out waiting for the coordinating agent to answer: ' + request['question'])
    return None


def paused_seconds(folder=None):
    folder = folder or directory()
    if folder is None:
        return 0
    total = 0
    for path in folder.glob('*.request.json'):
        try:
            r = json.loads(path.read_text())
            total += max(0, min(r.get('finished', time.time()), r['deadline']) - r['created'])
        except (ValueError, KeyError, OSError):
            continue
    return total


def close_pending(folder, reason='Worker stopped before receiving an answer'):
    for path in folder.glob('*.request.json'):
        r = json.loads(path.read_text())
        if r['status'] == 'pending':
            r.update(status='cancelled', finished=time.time(), reason=reason)
            save(path, r)
