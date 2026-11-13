"""Request-scoped usage collection; never stores prompts, credentials or answers."""
from contextlib import contextmanager
from contextvars import ContextVar

_collector = ContextVar('model_usage', default=None)


@contextmanager
def collect_usage():
    rows = []
    token = _collector.set(rows)
    try:
        yield rows
    finally:
        _collector.reset(token)


def record_usage(model, usage, seconds, kind='chat'):
    rows = _collector.get()
    if rows is not None:
        rows.append({'model': model, 'kind': kind, 'seconds': round(seconds, 4),
                     'usage': usage or {}, 'usage_available': bool(usage)})
