"""Restore visible stage icons on source-calibrated, scrollable normal maps."""
from functools import lru_cache
import json
from pathlib import Path

from . import NavigationError

DATA = Path(__file__).resolve().parents[1] / 'data/navigation/stage-scroll.json'


@lru_cache(maxsize=1)
def _recipes():
    return {item['target']: item for item in json.loads(DATA.read_text(encoding='utf8'))['maps']}


def has_standard_view(target):
    return 'end_x' in _recipes().get(target, {})


def wait_stage_ready(context, navigator, target):
    """Observe a temporarily missing icon using the navigator's current frame."""
    navigator._check()
    remaining = max(1, int((navigator.deadline-navigator.clock())*1000))
    navigator.log('等待关卡图标稳定：仅重新识别，不发送点击。')
    result=navigator.wait_for(['Nav_CachedModal',target],min(1200,remaining))
    navigator._check()
    # A modal can also end the wait. The caller must re-localize the full scene
    # before it can return success or attempt a guarded viewport restoration.
    return result is not None


def restore_stage_view(context, navigator, target):
    recipe = _recipes().get(target)
    if recipe is None:
        return False
    # Locate, terrain and the actual drag share a single current observation.
    if getattr(navigator,'deadline',None) is None:
        navigator.deadline = navigator.clock() + 120
    for step in range(recipe['max_steps'] + 1):
        navigator._check()
        state = navigator.locate()
        if state == 'task_destination':
            return True
        if state not in ('map',):
            return False
        if step == recipe['max_steps']:
            return False
        guard = navigator.recognize(recipe['drag_node'])
        navigator._check()
        if not guard.hit:
            navigator.log('大地图视野未恢复：未确认地图、普通模式或可拖动地形，已停止。')
            return False
        navigator.log(f"恢复大地图视野：{recipe['task']}（{step+1}/{recipe['max_steps']}）")
        if 'end_x' in recipe:
            # Only this freshly recognized non-interactive terrain supplies the
            # press point. A deterministic horizontal gesture reaches one edge;
            # no scroll-offset estimate or old click coordinate is retained.
            x, y, width, height = guard.box
            cx, cy = int(x + width // 2), int(y + height // 2)
            override = {recipe['drag_node']: {'action': {'type': 'Swipe', 'param': {
                'begin': [cx, cy, 1, 1], 'end': [recipe['end_x'], cy, 1, 1],
                'begin_offset': [0, 0, 0, 0], 'end_offset': [0, 0, 0, 0]}}}}
            result = navigator.perform(recipe['drag_node'],guard,pipeline_override=override)
        else:
            result = navigator.perform(recipe['drag_node'],guard)
        navigator._check()
        if not result:
            raise NavigationError('大地图拖动未确认，已停止。', reason='transition_failed')
        if 'end_x' in recipe:
            # Native swipe already includes its transition delay. The target
            # includes the map guard, so one new frame also rejects mis-entry.
            if navigator.recognize(target).hit:
                return True
            # A game frame can miss part of a gesture. Re-localize and match
            # safe terrain on the new frame before the one corrective swipe.
            # Mis-entry, dialogs and missing terrain never receive a blind drag.
            continue
        navigator._settle()
    return False
