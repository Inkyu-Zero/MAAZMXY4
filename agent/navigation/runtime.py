"""Target first, related candidates next, shared recovery only on a miss.

Resources declare routes and native guarded actions. The runner owns bounded
attempts and verification; it never searches a graph or caches coordinates.
"""
import time
from types import SimpleNamespace

from . import NavigationError
from .recognition import modal_present
from background_mouse_guard import hold_background_mouse, MouseGuardError


def _move_cursor_off_target(controller):
    # Reuse the existing background-only message, without a native CustomAction
    # node (run_action has no recognition record for such callbacks in Maa 5.12).
    # Lazy import keeps the successful navigation path free of extra work.
    from mouse_input import move_off_target
    return move_off_target(controller)


class Navigator:
    def __init__(self, context, routine, *, capture, check, log=print, wait=time.sleep, clock=time.monotonic, frame=None):
        self.context, self.routine = context, routine
        self.capture, self.check, self.log = capture, check, log
        self.wait, self.clock = wait, clock
        self.deadline = None
        self.steps, self.attempts = 0, {}
        self.expected = []
        self.only_actions = None
        self.before_input = None
        self._frame = frame
        self._frame_id = 1 if frame is not None else 0
        self._recognized = {}
        self._entry_hover_cleared = False

    @property
    def image(self):
        """One current observation, shared until input or an explicit wait."""
        self._check()
        if self._frame is None:
            self._frame = self.capture()
            self._frame_id += 1
            self._check()
            if self._frame is None:
                raise ValueError('导航截图为空。')
        return self._frame

    def invalidate(self):
        self._frame = None
        self._recognized.clear()

    def recognize(self, name):
        self._check()
        if name not in self._recognized:
            detail = self.context.run_recognition(name, self.image)
            self._check()
            if detail is None:
                raise ValueError('导航识别节点不可用：' + name)
            # Native details may retain raw/drawn images. Keep only a decision
            # and its same-frame box; never accumulate screenshot history.
            self._recognized[name] = SimpleNamespace(hit=bool(detail.hit),
                box=getattr(detail, 'box', None), frame_id=self._frame_id, name=name)
        return self._recognized[name]

    def perform(self, name, hit=None, *, pipeline_override=None, center=False):
        """Dispatch native input using the box already verified on this frame."""
        hit = hit or self.recognize(name)
        self._check()
        if (not hit.hit or hit.box is None or self._frame is None
                or hit.frame_id != self._frame_id or hit.name != name):
            raise NavigationError('操作缺少当前帧的有效识别，未发送输入。', reason='transition_failed')
        box = hit.box
        if center:
            x, y, width, height = box
            box = [int(x + width // 2), int(y + height // 2), 1, 1]
        if self.before_input is not None:
            self.before_input()
        self._check()
        # A failed action can still have sent input. Invalidate before dispatch
        # so neither success nor error paths can reuse a pre-action observation.
        self.invalidate()
        try:
            options = {'pipeline_override': pipeline_override} if pipeline_override else {}
            with hold_background_mouse(getattr(self.context.tasker, 'controller', None)):
                self._check()
                result = self.context.run_action(name, box=box, **options)
        except MouseGuardError as error:
            # Cancellation still wins, but an expired navigation deadline must
            # not turn an ownership failure back into a restartable timeout.
            self.check()
            self.log(str(error))
            raise NavigationError(str(error), reason='input_guard_failed') from error
        except Exception as error:
            self._check()
            raise NavigationError('原生导航操作未确认：' + name + '；' + str(error), reason='transition_failed') from error
        self._check()
        kind = getattr(result, 'action', None)
        kind = getattr(kind, 'name', kind)
        if result is not None and result.success and kind in ('Click', 'LongPress', 'Swipe', 'MultiSwipe', 'Scroll', 'TouchUp'):
            moved, detail = _move_cursor_off_target(self.context.tasker.controller)
            self._check()
            if not moved:
                raise NavigationError('鼠标操作后的悬停未移开：' + str(detail), reason='transition_failed')
        return result is not None and bool(result.success)

    def _check(self):
        self.check()
        if self.deadline is not None and self.clock() >= self.deadline:
            raise TimeoutError('界面导航超过运行时限，已停止。')

    def _settle(self, milliseconds=None):
        delay = self.routine.get('settle_ms', 200) if milliseconds is None else milliseconds
        end = self.clock() + delay / 1000
        while self.clock() < end:
            self._check()
            self.wait(min(.03, max(0, end - self.clock())))

    def refresh_after_wait(self, milliseconds=None):
        self._settle(milliseconds)
        self.invalidate()

    def _modal_state(self):
        for attempt in range(3):
            if not modal_present(self.context, self.image):
                return None
            if self.recognize('Result_RevivePrompt').hit:
                return 'revive_prompt'
            if self.recognize('Nav_See_profile_sell').hit:
                return 'profile_sell'
            if self.recognize('Nav_BlockedDialog').hit:
                raise NavigationError('验证码、登录或阻断提示未处理，导航已停止。', reason='blocked')
            if attempt == 0:
                from battle_outcomes import save_unknown_frame
                save_unknown_frame(self.image)
            if attempt < 2:
                self.refresh_after_wait(200)
        raise NavigationError('确认浮窗连续重判后仍无法识别，未发送导航操作。', reason='unrecognized_dialog')

    def _identify(self, candidates):
        modal = self._modal_state()
        if modal:
            return modal
        screens = self.routine['screens']
        for ident in candidates:
            if ident not in screens or not self.recognize(screens[ident]['detector']).hit:
                continue
            if ident in ('challenge_failed', 'challenge_succeeded'):
                other = 'Result_Succeeded' if ident == 'challenge_failed' else 'Result_Failed'
                if self.recognize(other).hit:
                    raise NavigationError('战斗结果识别存在歧义，未发送返回操作。', reason='ambiguous')
            if ident in ('login', 'saves'):
                raise NavigationError('登录或存档界面需要游戏管理核验，导航不直接进入。', reason='blocked')
            return ident
        return None

    def wait_for(self, names, timeout_ms):
        """Bounded observation-only polling; the successful frame stays current."""
        end = self.clock() + timeout_ms / 1000
        while True:
            self._check()
            for name in names:
                if self.recognize(name).hit:
                    return name
            if self.clock() >= end:
                return None
            self.refresh_after_wait(min(200, max(0, (end-self.clock())*1000)))

    def locate(self):
        """Reuse one image and stop at the first complete relevant match."""
        screens = self.routine['screens']
        # Stage entries already combine clear-map and icon recognition. Use
        # their same-frame box directly; do not wrap/re-recognize that entry.
        target = self.routine.get('target_node')
        if (self.routine.get('kind') == 'stage' and 'task_destination' in self.routine['targets']
                and target and self.recognize(target).hit):
            return 'task_destination'
        quick = [*self.routine['targets'], *self.expected, 'map']
        quick += self.routine.get('nearby', {}).get(self.routine.get('kind'), [])
        quick += ['world', 'paused_task_map', 'settings_subpanel', 'settings_menu']
        candidates = list(dict.fromkeys([*quick, *self.routine['fallback']]))
        for attempt in range(self.routine.get('locate_attempts', 3)):
            self._check()
            ident = self._identify(candidates)
            if ident:
                self.log('当前界面：' + screens[ident]['label'])
                return ident
            # OCR is a failure fallback, not a tax on every successful probe.
            if self.recognize('Nav_BlockedDialog').hit:
                raise NavigationError('验证码、登录或阻断提示未处理，导航已停止。', reason='blocked')
            if attempt + 1 < self.routine.get('locate_attempts', 3):
                self.refresh_after_wait()
        raise NavigationError('当前界面无法定位，未发送导航操作。', reason='unknown')

    def _action(self, current):
        kind = self.routine.get('kind')
        if self.only_actions is not None:
            return next((name for name in self.only_actions
                         if current in self.routine['actions'][name]['from']), None)
        if kind == 'bounty':
            shortcut = {'map':'Nav_OpenQuest', 'quest_panel':'Nav_SelectBountyTab',
                        'accepted_bounties':'Nav_ChooseBounty', 'world':'Nav_WorldNanzhan'}
            if current in shortcut: return shortcut[current]
        elif kind == 'stage':
            if current == 'map':
                province = self.routine.get('province')
                if not province: return None
                self._check()
                detail = self.recognize('Nav_Province_' + province)
                # An icon on this province can be off-screen: the bounded
                # viewport routine must recheck terrain before every swipe.
                return None if detail.hit else 'Nav_OpenWorld'
            if current == 'world':
                province = self.routine.get('province')
                return 'Nav_World_' + province if province else None
        for name in self.routine['routes'].get(self.routine.get('destination', 'map'), []):
            if current in self.routine['actions'][name]['from']:
                return name
        return self.routine['screens'][current].get('exit')

    def _execute(self, name):
        action = self.routine['actions'][name]
        if self.attempts.get(name, 0) >= action.get('attempts', 2):
            raise NavigationError('该导航操作重试次数已用尽：' + action['label'], reason='unreachable')
        self.log('正在导航：' + action['label'])
        remaining = max(1, int((self.deadline - self.clock()) * 1000))
        candidates = [name, 'Result_FailedClear'] if action.get('wait_for_clear') else ['Nav_CachedModal',name]
        ready = self.wait_for(candidates, min(action.get('timeout_ms', 10000), remaining))
        if ready == 'Nav_CachedModal':
            return self._modal_state()
        if ready == 'Result_FailedClear':
            return 'challenge_failed'
        if ready is None:
            raise NavigationError('预期操作条件未就绪：' + action['label'], reason='transition_failed')
        self.steps += 1
        self.attempts[name] = self.attempts.get(name, 0) + 1
        succeeded = self.perform(name)
        self.expected = list(action.get('verify', []))
        verification = list(dict.fromkeys([*self.routine['targets'], *action.get('verify', [])]))
        if action.get('wait_for_clear'):
            # A just-cancelled revival can linger; wait without clicking again.
            found = self.wait_for(['Result_FailedClear'], min(action.get('verify_ms', 15000), remaining))
            return 'challenge_failed' if found else None
        end = self.clock() + min(action.get('verify_ms', 10000), remaining) / 1000
        while True:
            self._check()
            actual = self._identify(verification)
            if actual:
                return actual
            if not succeeded or self.clock() >= end:
                self.log('操作未确认，将重新识别实际画面。')
                return None
            self.refresh_after_wait(min(200, max(0, (end-self.clock())*1000)))

    def _clear_entry_hover_once(self, current):
        """One fallback on a confirmed stage map or accepted-bounty list."""
        expected = {'stage': 'map', 'bounty': 'accepted_bounties'}.get(self.routine.get('kind'))
        if (self._entry_hover_cleared or current != expected or self.routine.get('standard_view')
                or 'task_destination' not in self.routine['targets']
                or self.only_actions is not None):
            return False
        if self.before_input is not None:
            self.before_input()
        self._check()
        self._entry_hover_cleared = True
        self.steps += 1
        # A posted message may have moved the game's hover even if reporting
        # fails. Never retain a box or image from before this operation.
        self.invalidate()
        try:
            moved, detail = _move_cursor_off_target(self.context.tasker.controller)
        except Exception as error:
            self._check()
            raise NavigationError('后台移开悬停失败，未继续操作地图。', reason='transition_failed') from error
        self._check()
        if not moved:
            raise NavigationError('后台移开悬停失败：' + str(detail), reason='transition_failed')
        self.log('关卡入口未识别：已后台移开悬停，用新截图重识别一次。')
        return True

    def run(self):
        if self.deadline is None:
            self.deadline = self.clock() + self.routine.get('max_seconds', 120)
        current = None
        while True:
            self._check()
            current = current or self.locate()
            if current in self.routine['targets']: return current
            if self.steps >= self.routine.get('max_steps', 20):
                raise NavigationError('界面导航达到最大操作步数。', reason='budget')
            action = self._action(current)
            if action is None:
                if self._clear_entry_hover_once(current):
                    current = None
                    continue
                raise NavigationError('已确认界面中没有本任务可执行的入口。', reason='unreachable')
            if (action == 'Nav_ChooseBounty' and not self.recognize(action).hit
                    and self._clear_entry_hover_once(current)):
                current = None
                continue
            current = self._execute(action)
            # Verify a NEW frame; never reuse an old click box after transition.
