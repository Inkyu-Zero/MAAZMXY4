"""One bounded same-frame recognition cache; no input or Maa dependency."""
import hashlib
import threading
from types import SimpleNamespace

_lock = threading.RLock()
_frame_key = None
_results = {}


def clear_cache():
    global _frame_key
    with _lock:
        _frame_key = None
        _results.clear()


def _probe(context, frame, name):
    global _frame_key
    # Only the latest frame is retained, and only recognition results, never
    # click coordinates for another image. Tasker identity isolates controllers;
    # registering a guard for a loaded resource clears previous results.
    tasker = context.tasker
    scope = getattr(tasker, '_handle', id(tasker))
    scope = getattr(scope, 'value', scope)
    key = (scope, frame.shape, hashlib.blake2b(frame.tobytes(), digest_size=16).digest())
    with _lock:
        if key != _frame_key:
            _results.clear()
            _frame_key = key
        if name not in _results:
            result = context.run_recognition(name, frame)
            if result is None:
                raise ValueError('浮窗识别资源不可用：' + name)
            # Debug RecognitionDetail may contain full screenshots and drawn
            # copies. Retain only the decision and a tiny same-frame rectangle.
            _results[name] = SimpleNamespace(hit=bool(result.hit), box=getattr(result, 'box', None))
        return _results[name]


def modal_present(context, frame):
    return bool(_probe(context, frame, 'Nav_ModalPresent').hit)

