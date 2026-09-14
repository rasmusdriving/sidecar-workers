import subprocess
from .engine import setup

def resolve_model(provider, model, effort):
    if provider == 'devin':
        model = model or 'swe-2-high'
        if model not in ('swe-2-high', 'swe-2-max', 'swe-2-medium') or effort:
            raise ValueError('Use a SWE-2 model with effort included in its name')
        return model, None
    if provider == 'claude':
        model = model or 'claude-opus-5'
        if model not in ('claude-fable-5-1', 'claude-opus-5'):
            raise ValueError('Claude workers use Fable 5.1 or Opus 5')
        if effort and effort not in ('medium', 'high', 'xhigh'):
            raise ValueError('Claude effort must be medium, high, or xhigh')
        return model, effort or 'high'
    if effort not in (None, 'high'):
        raise ValueError('Grok workers use high reasoning')
    if not model or model == 'latest':
        import re
        setup()
        from runtime.platform import prepare_spawn
        cmd, options = prepare_spawn(['grok', 'models'])
        catalog = subprocess.run(cmd, **options, capture_output=True, text=True, check=True, timeout=20).stdout
        models = set(re.findall(r'\bgrok-\d+(?:\.\d+)+\b', catalog))
        if not models:
            raise ValueError('Could not resolve the latest Grok model from the live catalog')
        model = max(models, key=lambda x: tuple(map(int, x[5:].split('.'))))
    return model, 'high'
