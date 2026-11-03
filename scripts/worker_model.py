"""Current default and frozen worker identity; old receipts retain their model."""

from collections import namedtuple

PROVIDER = 'opencode-ds'
LEGACY_MODEL = PROVIDER + '/deepseek-v4-flash'
DEFAULT_MODEL = PROVIDER + '/deepseek-v4.1-flash'
SECONDARY_MODEL = 'secondary-ds/deepseek-v4-flash-ascend1'
SUPPORTED_MODELS = (LEGACY_MODEL, DEFAULT_MODEL, SECONDARY_MODEL)

Route = namedtuple('Route', 'profile credential_file credential_key credential_env')


_ROUTES = {
    LEGACY_MODEL: Route('codex-ds', 'credentials.json', 'opencode_api_key', 'OMP_OPENCODE_API_KEY'),
    DEFAULT_MODEL: Route('codex-ds', 'credentials.json', 'opencode_api_key', 'OMP_OPENCODE_API_KEY'),
    SECONDARY_MODEL: Route('codex-ds-secondary', 'secondary-credentials.json', 'api_key', 'OMP_SECONDARY_DS_API_KEY'),
}


def route_for(selector):
    if not isinstance(selector, str) or selector not in _ROUTES:
        raise ValueError('Unsupported worker model; prepare a fresh run')
    return _ROUTES[selector]


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


def matches_saved_selector(meta, observed_provider, observed_model):
    expected_model = diagnostic_model_id(meta)
    if expected_model is None or observed_model != expected_model:
        return False
    return observed_provider == meta.get('worker_model', LEGACY_MODEL).split('/', 1)[0]
