"""Candidate activation, verification and failure bounds without device input."""
from copy import deepcopy
from contextlib import contextmanager
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'agent'))
from navigation import NavigationError
from navigation.runtime import Navigator


class Context:
    def __init__(self):
        self.hits = {'start'}
        self.calls, self.inputs, self.frames = [], [], []
        self.click = lambda: setattr(self, 'hits', {'goal'})
        self.tasker = SimpleNamespace(get_node_detail=lambda _:None)
    def capture(self):
        frame = frozenset(self.hits)
        self.frames.append(frame)
        return frame
    def run_recognition(self, name, frame):
        self.calls.append(name)
        hit=name in frame or name=='Go' and bool(frame & {'start','other'}) or name=='Result_DeclineRevive' and 'Result_RevivePrompt' in frame
        return SimpleNamespace(hit=hit,box=(len(self.frames),0,1,1))
    def run_action(self, entry, box):
        self.inputs.append(dict(name=entry,box=box))
        self.click()
        return SimpleNamespace(success=True)


class NavigationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.context, self.now = Context(), 0
        self.routine = dict(targets=['goal'], destination='goal', kind=None,
            max_seconds=10, max_steps=5, locate_attempts=3, settle_ms=10,
            screens={n:dict(detector=n,label=n) for n in ['goal','start','other','revive_prompt']},
            actions={'Go':{'from':['start','other'], 'verify':['goal'], 'label':'go','verify_ms':100},
                     'Result_DeclineRevive':{'from':['revive_prompt'], 'verify':['start'], 'label':'decline','verify_ms':100}},
            routes={'goal':['Go']}, fallback=['start','other'])
        self.routine['screens']['revive_prompt']['exit']='Result_DeclineRevive'
        self.guard=patch('navigation.runtime.modal_present',side_effect=lambda c,f:'modal' in f)
        self.guard.start();self.addCleanup(self.guard.stop)
    def nav(self, check=lambda:None):
        def wait(t):self.now+=t
        return Navigator(self.context,self.routine,capture=self.context.capture,check=check,
                         wait=wait,clock=lambda:self.now,log=lambda _:None)
    def test_ready_target_never_scans_library_or_sends_input(self):
        self.context.hits={'goal'}
        for i in range(200):
            self.routine['screens'][str(i)]=dict(detector=str(i),label=str(i))
            self.routine['fallback'].append(str(i))
        self.assertEqual(self.nav().run(),'goal')
        self.assertEqual(self.context.calls,['goal'])
        self.assertFalse(self.context.inputs)
    def test_click_uses_current_guard_box_and_one_post_action_frame(self):
        self.assertEqual(self.nav().run(),'goal')
        self.assertEqual(self.context.inputs,[dict(name='Go',box=(1,0,1,1))])
        self.assertEqual(len(self.context.frames),2)
    def test_verification_frame_is_reused_by_the_next_decision(self):
        nav=self.nav()
        self.assertEqual(nav.run(),'goal')
        self.assertEqual(nav.locate(),'goal')
        self.assertTrue(nav.recognize('goal').hit)
        self.assertEqual(len(self.context.frames),2)
        self.assertEqual(self.context.calls,['goal','start','Nav_CachedModal','Go','goal'])
    def test_old_match_cannot_be_used_after_an_input(self):
        nav=self.nav();nav.locate();hit=nav.recognize('Go')
        self.assertTrue(nav.perform('Go',hit))
        self.assertIsNone(nav._frame)
        self.assertTrue(nav.recognize('goal').hit)
        with self.assertRaises(NavigationError):nav.perform('Go',hit)
        self.assertEqual(len(self.context.inputs),1)
    def test_action_failure_also_discards_the_old_frame(self):
        nav=self.nav();nav.locate()
        self.context.run_action=lambda *args,**kwargs:SimpleNamespace(success=False)
        self.assertFalse(nav.perform('Go'))
        self.assertIsNone(nav._frame)
        self.assertEqual(nav._recognized,{})

    def test_guard_covers_native_action_and_releases_on_action_exception(self):
        events=[]
        @contextmanager
        def guard(controller):
            events.append('begin')
            try: yield
            finally: events.append('end')
        def action(*args, **kwargs):
            events.append('input')
            raise RuntimeError('input failed')
        self.context.run_action=action
        nav=self.nav();nav.locate()
        with patch('navigation.runtime.hold_background_mouse',guard), self.assertRaises(NavigationError):
            nav.perform('Go')
        self.assertEqual(events,['begin','input','end'])

    def test_guard_start_failure_prevents_navigation_input(self):
        nav=self.nav();nav.locate()
        with patch('navigation.runtime.hold_background_mouse',side_effect=RuntimeError('guard unavailable')), \
             self.assertRaisesRegex(NavigationError,'guard unavailable'):
            nav.perform('Go')
        self.assertFalse(self.context.inputs)

    def test_guard_cleanup_failure_has_a_nonrecoverable_reason(self):
        from background_mouse_guard import MouseGuardError
        @contextmanager
        def guard(_):
            yield
            self.now=2  # Cleanup may cross the ordinary navigation deadline.
            raise MouseGuardError('cleanup timeout 1460')
        nav=self.nav()
        nav.deadline=1
        with patch('navigation.runtime.hold_background_mouse',guard), self.assertRaises(NavigationError) as error:
            nav.perform('Go')
        self.assertEqual(error.exception.reason,'input_guard_failed')
        self.assertEqual(len(self.context.inputs),1)
        self.assertIsNone(nav._frame)
    def test_seed_frame_does_not_request_another_capture(self):
        nav=Navigator(self.context,self.routine,capture=self.context.capture,check=lambda:None,
                      frame=frozenset({'goal'}),log=lambda _:None)
        self.assertEqual(nav.run(),'goal')
        self.assertFalse(self.context.frames)
    def test_failed_click_retries_are_bounded_without_touching_round_counter(self):
        self.context.click=lambda:None
        with self.assertRaises(NavigationError) as error:self.nav().run()
        self.assertEqual(error.exception.reason,'unreachable')
        self.assertEqual(len(self.context.inputs),2)
        self.assertNotIn('刷取次数',str(self.context.inputs))
    def test_unexpected_known_screen_uses_only_its_declared_action(self):
        self.context.click=lambda:setattr(self.context,'hits',{'other'} if len(self.context.inputs)==1 else {'goal'})
        self.assertEqual(self.nav().run(),'goal')
        self.assertEqual(len(self.context.inputs),2)
    def test_unknown_frame_is_bounded_and_has_no_input(self):
        self.context.hits=set()
        with self.assertRaises(NavigationError) as error:self.nav().run()
        self.assertEqual(error.exception.reason,'unknown')
        self.assertEqual(len(self.context.frames),3)
        self.assertFalse(self.context.inputs)
    def test_modal_obscures_target_and_never_clicks_background(self):
        self.context.hits={'goal','modal'}
        with self.assertRaises(NavigationError) as error:self.nav().run()
        self.assertEqual(error.exception.reason,'unrecognized_dialog')
        self.assertIn('Nav_BlockedDialog',self.context.calls)
        self.assertFalse(self.context.inputs)
        self.assertEqual(len(self.context.frames), 3)
    def test_unready_revive_rechecks_and_uses_the_new_frame(self):
        self.context.hits = {'modal'}
        capture = self.context.capture
        def settle():
            if self.context.frames:
                self.context.hits = {'modal', 'Result_RevivePrompt'}
            return capture()
        self.context.capture = settle
        nav = self.nav()
        self.assertEqual(nav.locate(), 'revive_prompt')
        self.assertEqual(len(self.context.frames), 2)
        self.assertEqual(nav.recognize('Result_RevivePrompt').box, (2, 0, 1, 1))
        self.assertFalse(self.context.inputs)
    def test_known_blocked_modal_still_stops_without_input(self):
        self.context.hits={'goal','modal','Nav_BlockedDialog'}
        with self.assertRaises(NavigationError) as error:self.nav().run()
        self.assertEqual(error.exception.reason,'blocked')
        self.assertFalse(self.context.inputs)
    def test_unavailable_blocker_recognition_does_not_become_unknown_dialog(self):
        self.context.hits={'goal','modal'}
        original=self.context.run_recognition
        self.context.run_recognition=lambda name,frame: None if name=='Nav_BlockedDialog' else original(name,frame)
        with self.assertRaises(ValueError):self.nav().run()
        self.assertFalse(self.context.inputs)
    def test_revival_has_only_decline_then_verified_result(self):
        self.context.hits={'goal','modal','Result_RevivePrompt'}
        self.assertEqual(self.nav().run(),'goal')
        self.assertEqual(self.context.inputs[0]['name'],'Result_DeclineRevive')
    def test_cancellation_prevents_any_input(self):
        def cancel():raise InterruptedError('cancelled')
        with self.assertRaises(InterruptedError):self.nav(cancel).run()
        self.assertFalse(self.context.inputs)
    def test_deadline_is_not_reset_by_a_second_run(self):
        self.context.hits={'goal'};nav=self.nav();nav.run();self.now=11
        with self.assertRaises(TimeoutError):nav.run()
    def test_failure_return_restriction_cannot_open_another_screen(self):
        nav=self.nav();nav.only_actions=['Result_DeclineRevive']
        with self.assertRaises(NavigationError):nav.run()
        self.assertFalse(self.context.inputs)

    def test_ambiguous_results_stop_before_returning(self):
        for state,node in [('challenge_failed','Result_Failed'),('challenge_succeeded','Result_Succeeded')]:
            self.routine['screens'][state]=dict(detector=node,label=state)
            self.routine['fallback'].append(state)
        self.context.hits={'Result_Failed','Result_Succeeded'}
        with self.assertRaises(NavigationError) as error:self.nav().run()
        self.assertEqual(error.exception.reason,'ambiguous')
        self.assertFalse(self.context.inputs)

    def stage_nav(self, target='StageEntry', province='town4', ready=False):
        self.context=Context()
        self.context.tasker.controller=object()
        self.context.hits={'map','Nav_Province_'+province} | ({target} if ready else set())
        self.routine=dict(kind='stage',province=province,targets=['task_destination'],
            destination='map',max_seconds=10,max_steps=5,locate_attempts=1,settle_ms=10,
            screens={'task_destination':dict(detector=target,label='stage'),
                     'map':dict(detector='map',label='map')},
            actions={},routes={},fallback=[])
        return self.nav()

    def test_every_authored_stage_rechecks_one_new_frame_after_hover_clear(self):
        stages=json.loads((Path(__file__).resolve().parents[1]/'agent/data/navigation/routines.json')
                          .read_text(encoding='utf8'))['stages']
        for target,province in stages.items():
            with self.subTest(target=target):
                nav=self.stage_nav(target,province)
                def move(controller):
                    self.assertIs(controller,self.context.tasker.controller)
                    self.context.hits.add(target)
                    return True,'memory-only move'
                with patch('navigation.runtime._move_cursor_off_target',create=True,side_effect=move) as moved:
                    self.assertEqual(nav.run(),'task_destination')
                    moved.assert_called_once()
                self.assertEqual(len(self.context.frames),2)
                self.assertEqual(self.context.calls.count(target),2)
                self.assertEqual(nav.steps,1)
                self.assertEqual(self.now,0, 'hover cleanup must not add a delay')
                self.assertTrue(nav.perform(target))
                self.assertEqual(self.context.inputs,[dict(name=target,box=(2,0,1,1))])
                self.assertEqual(len(self.context.frames),2)

    def test_visible_stage_has_no_hover_move_wait_or_additional_capture(self):
        nav=self.stage_nav(ready=True)
        with patch('navigation.runtime._move_cursor_off_target',create=True) as moved:
            self.assertEqual(nav.run(),'task_destination');moved.assert_not_called()
        self.assertEqual(self.context.calls,['StageEntry'])
        self.assertEqual(len(self.context.frames),1)
        self.assertEqual(self.now,0)
        self.assertEqual(nav.steps,0)

    def test_missing_stage_moves_only_once_even_when_navigation_is_retried(self):
        nav=self.stage_nav()
        with patch('navigation.runtime._move_cursor_off_target',create=True,return_value=(True,'memory')) as moved:
            for _ in range(3):
                with self.assertRaises(NavigationError) as error:nav.run()
                self.assertEqual(error.exception.reason,'unreachable')
            moved.assert_called_once()
        self.assertEqual(len(self.context.frames),2)
        self.assertFalse(self.context.inputs)

    def test_failed_hover_message_invalidates_old_frame_and_does_not_retry(self):
        nav=self.stage_nav()
        with patch('navigation.runtime._move_cursor_off_target',create=True,return_value=(False,'lost window')) as moved:
            with self.assertRaises(NavigationError) as error:nav.run()
            self.assertEqual(error.exception.reason,'transition_failed')
            self.assertIsNone(nav._frame)
            self.assertEqual(nav._recognized,{})
            with self.assertRaises(NavigationError):nav.run()
            moved.assert_called_once()
        self.assertFalse(self.context.inputs)

    def test_hover_clear_honors_input_owner_cancellation_and_return_only_scope(self):
        for mode in ('revision','cancel','return_only','already_returning','bounty','unknown','modal'):
            with self.subTest(mode=mode):
                nav=self.stage_nav()
                def stop():raise InterruptedError('cancelled or changed recording')
                if mode=='revision':nav.before_input=stop
                if mode=='cancel':nav.check=stop
                if mode=='return_only':nav.only_actions=[]
                if mode=='already_returning':nav.routine['targets']=['map']
                if mode=='bounty':nav.routine['kind']='screen'
                if mode=='unknown':self.context.hits=set()
                if mode=='modal':self.context.hits.add('modal')
                with patch('navigation.runtime._move_cursor_off_target',create=True) as moved:
                    try:nav.run()
                    except (NavigationError,InterruptedError):pass
                    moved.assert_not_called()
                self.assertFalse(self.context.inputs)

    def test_new_modal_after_hover_clear_prevents_entry_click(self):
        nav=self.stage_nav()
        def move(_):
            self.context.hits.update({'StageEntry','modal'})
            return True,'memory'
        with patch('navigation.runtime._move_cursor_off_target',create=True,side_effect=move):
            with self.assertRaises(NavigationError) as error:nav.run()
        self.assertEqual(error.exception.reason,'unrecognized_dialog')
        self.assertFalse(self.context.inputs)

    def test_bounty_row_miss_shares_once_only_recovery_and_success_uses_same_frame(self):
        for missing in (False,True):
            with self.subTest(missing=missing):
                self.setUp()
                self.context.tasker.controller=object()
                self.context.hits={'accepted_bounties'}
                if not missing:self.context.hits.add('Nav_ChooseBounty')
                self.routine.update(kind='bounty',targets=['task_destination'],fallback=['accepted_bounties'])
                self.routine['screens'].update(task_destination=dict(detector='goal',label='goal'),
                    accepted_bounties=dict(detector='accepted_bounties',label='bounties'))
                self.routine['actions']['Nav_ChooseBounty']=dict(label='choose bounty',verify=['task_destination'])
                def move(_):
                    self.context.hits.add('Nav_ChooseBounty');return True,'cleared'
                with patch('navigation.runtime._move_cursor_off_target',side_effect=move) as moved:
                    self.assertEqual(self.nav().run(),'task_destination')
                    self.assertEqual(moved.call_count,int(missing))
                self.assertEqual(len(self.context.frames),2+int(missing))
                self.assertEqual(self.context.inputs,[dict(name='Nav_ChooseBounty',box=(1+int(missing),0,1,1))])


if __name__=='__main__':unittest.main()
