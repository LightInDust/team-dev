"""Current default and frozen worker identity; old receipts retain their model."""

PROVIDER = 'opencode-ds'
LEGACY_MODEL = PROVIDER + '/deepseek-v4-flash'
DEFAULT_MODEL = PROVIDER + '/deepseek-v4.1-flash'
SUPPORTED_MODELS = (LEGACY_MODEL, DEFAULT_MODEL)


def model_id(selector):
    if not isinstance(selector, str) or selector not in SUPPORTED_MODELS:
        raise ValueError('Unsupported saved worker model; prepare a fresh run')
    return selector.split('/', 1)[1]


def frozen_model(task, meta):
    """Both saved copies must agree; only markerless historical runs use V4."""
    if 'worker_model' not in task and 'worker_model' not in meta:
        return LEGACY_MODEL
    selected = task.get('worker_model')
    model_id(selected)
    if selected != meta.get('worker_model'):
        raise ValueError('Worker model changed; prepare a fresh run')
    return selected


def diagnostic_model_id(meta):
    """Bounded read-only metadata interpretation, never today's default."""
    try:
        return model_id(meta.get('worker_model', LEGACY_MODEL))
    except ValueError:
        return None
