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


def _pixels(frame):
    try:
        view = memoryview(frame)
        if view.c_contiguous:
            return view.cast('B')
    except TypeError:
        pass
    return frame.tobytes()


class FrameRecognition:
    """One callback's observation; the global cache retains decisions only."""
    def __init__(self, context, frame):
        self.context, self.frame = context, frame
        tasker = context.tasker
        scope = getattr(tasker, '_handle', id(tasker))
        scope = getattr(scope, 'value', scope)
        self.key = (scope, frame.shape, hashlib.blake2b(_pixels(frame), digest_size=16).digest())

    def _activate(self):
        global _frame_key
        if self.key != _frame_key:
            _results.clear()
            _frame_key = self.key

    def probe(self, name):
        with _lock:
            self._activate()
            if name not in _results:
                result = self.context.run_recognition(name, self.frame)
                if result is None:
                    raise ValueError('浮窗识别资源不可用：' + name)
                self._activate()
                _results[name] = SimpleNamespace(hit=bool(result.hit), box=getattr(result, 'box', None))
                self._remember_children(getattr(result, 'raw_detail', None))
            return _results[name]

    @staticmethod
    def _remember_children(children):
        # These named positive predicates have absolute ROIs and no inverse.
        # Maa already returns their executed And/Or details; never fetch child
        # images or infer a result for a short-circuited/unexecuted child.
        if not isinstance(children, list):
            return
        for child in children:
            if not isinstance(child, dict):
                continue
            name, box = child.get('name'), child.get('box')
            if name in ('Nav_ModalPresent', 'Result_ReviveEvidence', 'Nav_Probe_hub_world_entry') and 'box' in child:
                if box is None or (isinstance(box, list) and len(box) == 4):
                    _results[name] = SimpleNamespace(hit=box is not None, box=box)
            if child.get('algorithm') in ('And', 'Or'):
                FrameRecognition._remember_children(child.get('detail'))

    def any_of(self, combined, names):
        with _lock:
            self._activate()
            if any(name in _results and _results[name].hit for name in names):
                return True
            missing = [name for name in names if name not in _results]
            if not missing:
                return False
            return self.probe(missing[0] if len(missing) == 1 else combined).hit


def _probe(context, frame, name):
    return FrameRecognition(context, frame).probe(name)


def modal_present(context, frame):
    return bool(_probe(context, frame, 'Nav_ModalPresent').hit)

