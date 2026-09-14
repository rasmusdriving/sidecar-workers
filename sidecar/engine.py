"""Use the existing MCO execution engine without requiring its global install."""
import os
import shlex
import shutil
import sys
from pathlib import Path

from .common import host_free_env


def setup():
    root = Path(__file__).resolve().parent.parent / 'vendor/mco'
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def command(config, job, data):
    engine = setup()
    conf = data / 'engine-config'
    conf.mkdir(exist_ok=True)
    candidates = [os.environ.get('SIDECAR_DEVIN_BIN', ''), shutil.which('devin'), '/Applications/Devin - Next.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin', '/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin']
    binary = next((p for p in candidates if p and Path(p).is_file()), None)
    if config['provider'] == 'devin':
        if not binary:
            raise ValueError('Devin CLI not found. Set SIDECAR_DEVIN_BIN to the installed Devin executable.')
        import json
        # JSON string syntax is valid in the YAML scalar used by MCO.
        (conf / 'agents.yaml').write_text('agents:\n  - name: devin\n    transport: acp\n    command: ' + json.dumps(shlex.quote(binary) + ' acp') + '\n', encoding='utf-8')
    provider = config['provider']
    cmd = [sys.executable, str(engine / 'mco'), 'run', '--repo', config['repo'], '--agent', provider + ':' + config['model'], '--transport', 'acp' if provider == 'devin' else 'shim', '--execution-mode', config['mode'], '--file', str(job / 'prompt.txt'), '--invocation-hard-timeout', str(config['timeout']), '--result-mode', 'both', '--artifact-base', str(job / 'artifacts'), '--stream', 'jsonl']
    if config.get('effort'):
        import json
        cmd += ['--provider-models-json', json.dumps({provider: {'effort': config['effort']}})]
    return cmd, {**host_free_env(), 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'MCO_CONFIG_DIR': str(conf), 'MCO_PERMISSION_DIR': str(job / 'permissions'), 'MCO_QUESTION_DIR': str(job / 'questions'), 'MCO_QUESTION_TIMEOUT': str(config.get('question_timeout', 600)), 'SIDECAR_MAX_TURNS': str(config.get('max_turns', 24)), 'SIDECAR_ALLOW_SUBAGENTS': '1' if config.get('allow_subagents') else '0'}
