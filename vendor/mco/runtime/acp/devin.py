"""Devin's stable ACP protocol, with verified SWE-2 selection and event capture."""
from __future__ import annotations

import json
import os
import time
import uuid
import threading
from pathlib import Path

from .adapter import AcpAdapter
from .client import AcpClient, AgentInfo

MODELS = {'swe-2-medium', 'swe-2-high', 'swe-2-max'}


class DevinClient(AcpClient):
    def __init__(self, command, cwd='.', stderr_path=None, *, model, mode):
        super().__init__(command, cwd=cwd, stderr_path=stderr_path)
        if model not in MODELS:
            raise ValueError('Devin adapter requires an explicit SWE-2 model')
        if mode not in {'ask', 'accept-edits', 'smart'}:
            raise ValueError('Devin adapter supports ask, accept-edits, or smart mode')
        self.model, self.mode = model, mode
        self._text = ''
        self._lock = threading.Lock()
        self._events_path = Path(stderr_path).with_suffix('.events.jsonl') if stderr_path else None
        self._output_path = Path(stderr_path.replace('.stderr.log', '.stdout.log')) if stderr_path else None
        self.denied = []
        self.permission_dir = Path(os.environ['MCO_PERMISSION_DIR']) if os.environ.get('MCO_PERMISSION_DIR') else None
        self.permission_timeout = 300

    def record(self, event):
        if self._events_path:
            with self._events_path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(event, ensure_ascii=False) + '\n')

    def permission(self, params):
        title = params.get('toolCall', {}).get('title') or json.dumps(params.get('toolCall', {}))[:1000]
        reason = 'No coordinating permission queue configured'
        if self.permission_dir:
            self.permission_dir.mkdir(parents=True, exist_ok=True)
            request_id = uuid.uuid4().hex
            path = self.permission_dir / (request_id + '.request.json')
            decision = self.permission_dir / (request_id + '.decision.json')
            request = dict(id=request_id, status='pending', request=params,
                           repo=str(self._cwd), mode=self.mode, created=time.time())
            def save():
                temp = path.with_suffix('.tmp')
                temp.write_text(json.dumps(request), encoding='utf-8')
                temp.replace(path)
            save()
            self.record({'type':'permission_pending', **request})
            deadline = time.monotonic() + self.permission_timeout
            while time.monotonic() < deadline:
                if decision.exists():
                    try:
                        selected = json.loads(decision.read_text()).get('optionId')
                    except (ValueError, OSError):
                        time.sleep(.1)
                        continue
                    allowed = {o['optionId'] for o in params.get('options', []) if o.get('kind') == 'allow_once'}
                    if selected in allowed:
                        request['status'] = 'approved'
                        save()
                        self.record({'type':'permission_resolved','id':request_id,'status':'approved'})
                        return {'outcome':{'outcome':'selected','optionId':selected}}
                    reason = 'Permission declined by coordinating agent'
                    break
                time.sleep(.1)
            else:
                reason = 'Timed out waiting for coordinating agent permission'
            request['status'] = 'denied'
            request['reason'] = reason
            save()
        self.denied.append(reason + ': ' + title)
        self.record({'type':'permission_denied', 'reason':reason, 'request':params})
        if params.get('sessionId'):
            self._transport.send_notification('session/cancel', {'sessionId':params['sessionId']})
        return {'outcome':{'outcome':'cancelled'}}

    def start(self, **kwargs):
        # Devin executes its own tools. Do not advertise client filesystem or shell access.
        self._transport.register_handler('session/request_permission', self.permission)
        self._transport.start(self._command, cwd=self._cwd, stderr_path=self._stderr_path)

    def initialize(self, timeout=30):
        result = self._transport.send_request('initialize', params={
            'protocolVersion':1, 'clientInfo':{'name':'mco','version':'0.11.0'},
            'clientCapabilities':{},
        }, timeout=timeout)
        info = result.get('agentInfo', {})
        self._agent_info = AgentInfo(info.get('name',''), info.get('version',''))
        return self._agent_info

    def new_session(self, working_directory=None, timeout=30):
        result = self._transport.send_request('session/new', params={
            'cwd':str(Path(working_directory or self._cwd).resolve()), 'mcpServers':[],
        }, timeout=timeout)
        sid = result['sessionId']
        current = {o['id']:o.get('currentValue') for o in result.get('configOptions', [])}
        if current.get('model') != self.model:
            raise ValueError('Devin model mismatch: requested {}, got {}'.format(self.model, current.get('model')))
        mode = self._transport.send_request('session/set_config_option', params={
            'sessionId':sid, 'configId':'mode', 'value':self.mode,
        }, timeout=timeout)
        current_mode = {o['id']:o.get('currentValue') for o in mode.get('configOptions', [])}
        if current_mode.get('mode') != self.mode:
            raise ValueError('Devin permission mode was not confirmed')
        self.record({'type':'session_ready','sessionId':sid,'model':self.model,'mode':self.mode})
        return sid

    def prompt(self, session_id, text, timeout=600):
        from ..coordinator import directory, parse_question, create_question, poll_reply
        remaining = timeout
        while True:
            started = time.monotonic()
            self._text = ''
            self.record({'type':'turn_started','sessionId':session_id})
            self._prompt_turn(session_id, text, timeout=max(.1, remaining))
            remaining -= time.monotonic() - started
            question = parse_question(self.collect_text()) if directory() else None
            if not question:
                return
            path = create_question(question, session_id)
            while True:
                text = poll_reply(path)
                if text is not None:
                    break
                time.sleep(.1)
            if remaining <= 0:
                raise RuntimeError('Worker execution budget exhausted')

    def _prompt_turn(self, session_id, text, timeout=600):
        stop = threading.Event()
        def consume():
            while not stop.is_set():
                event = self._transport.receive_notification(timeout=0.1)
                if event:
                    self.record(event)
                    update = event.get('params', {}).get('update', {})
                    if update.get('sessionUpdate') == 'agent_message_chunk':
                        content = update.get('content', {})
                        if content.get('type') == 'text':
                            with self._lock:
                                self._text += content.get('text','')
                                if self._output_path:
                                    self._output_path.write_text(self._text, encoding='utf-8')
        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            result = self._transport.send_request('session/prompt', params={
                'sessionId':session_id, 'prompt':[{'type':'text','text':text}],
            }, timeout=timeout)
            # The transport reader enqueues preceding notifications before waking this request.
            while not self._transport._notifications.empty():
                stop.wait(0.01)
            if self.denied:
                raise RuntimeError('Devin needs permission: ' + '; '.join(self.denied))
            if result.get('stopReason') != 'end_turn':
                raise RuntimeError('Devin stopped: {}'.format(result.get('stopReason')))
        finally:
            stop.set()
            worker.join(timeout=2)

    def collect_text(self):
        with self._lock:
            return self._text


class DevinAdapter(AcpAdapter):
    def __init__(self, command):
        super().__init__('devin', command[0], acp_command=command, permission_keys=['mode'])

    def supported_model_keys(self):
        return ['model']

    def supported_context_keys(self):
        return []

    def _make_client(self, task, command, stderr_path):
        model = task.metadata.get('model', 'swe-2-high')
        mode = task.metadata.get('provider_permissions', {}).get('mode', 'ask')
        return DevinClient([*command, '--model', model], cwd=task.repo_root,
                           stderr_path=stderr_path, model=model, mode=mode)
