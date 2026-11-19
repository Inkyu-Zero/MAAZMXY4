"""Result monitoring observes memory frames and never sends input."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from battle_outcomes import BattleOutcomeMonitor


POLICY = dict(failed_next=['Failed'], succeeded_next=['Won'], default_next=['Exit'], interval_ms=200)


class Pixels:
    ndim, shape = 3, (720, 1147, 3)
    def __init__(self, hits=()): self.hits = frozenset(hits)
    def copy(self): return Pixels(self.hits)
    def tobytes(self): return repr(sorted(self.hits)).encode()


class MemoryContext:
    def __init__(self):
        self.frame, self.capture_ok, self.samples = Pixels(), True, 0
        self.observed = threading.Event()
        self.missing = False
        self.tasker = SimpleNamespace(controller=self, stopping=False)
    def clone(self): return self
    def post_screencap(self):
        self.samples += 1
        return SimpleNamespace(wait=lambda:SimpleNamespace(succeeded=self.capture_ok))
    @property
    def cached_image(self): return self.frame
    def run_recognition(self, name, frame):
        if self.missing: return None
        if frame.hits: self.observed.set()
        if name=='Nav_ModalPresent':
            return SimpleNamespace(hit=bool(frame.hits & {'Nav_CommonConfirm','Result_RevivePrompt'}))
        return SimpleNamespace(hit=name in frame.hits)


class BattleOutcomeTests(unittest.TestCase):
    def test_unknown_modal_can_settle_only_after_playback_cleanup(self):
        context=MemoryContext();context.frame=Pixels(['Nav_CommonConfirm'])
        monitor=BattleOutcomeMonitor(context,{**POLICY,'revive_next':['Decline']})
        monitor.probe()
        self.assertEqual(getattr(monitor.error,'reason',None),'unrecognized_dialog')
        monitor.close()
        context.frame=Pixels(['Result_RevivePrompt'])
        monitor.recheck_unknown_dialog(wait=lambda _:None)
        self.assertIsNone(monitor.error)
        self.assertEqual(monitor.branch,['Decline'])
        self.assertEqual(context.samples,2)

    def test_unknown_recheck_is_bounded_and_cancellation_stops_capture(self):
        for cancel in (False,True):
            context=MemoryContext();context.frame=Pixels(['Nav_CommonConfirm'])
            monitor=BattleOutcomeMonitor(context,{**POLICY,'revive_next':['Decline']})
            monitor.probe();monitor.close();context.tasker.stopping=cancel
            monitor.recheck_unknown_dialog(wait=lambda _:None)
            self.assertEqual(context.samples,1 if cancel else 3)
            self.assertEqual(getattr(monitor.error,'reason',None),'unrecognized_dialog')
            self.assertIsNone(monitor.branch)

    def test_disappeared_unknown_dialog_does_not_resume_partial_recording(self):
        context = MemoryContext(); context.frame = Pixels(['Nav_CommonConfirm'])
        monitor = BattleOutcomeMonitor(context, {**POLICY, 'revive_next': ['Decline']})
        monitor.probe(); monitor.close()
        context.frame = Pixels()
        monitor.recheck_unknown_dialog(wait=lambda _: None)
        self.assertEqual(context.samples, 3)
        self.assertEqual(getattr(monitor.error, 'reason', None), 'unrecognized_dialog')
        self.assertIsNone(monitor.branch)

    def test_unknown_diagnostic_saves_the_original_decision_frame(self):
        from unittest.mock import patch
        context = MemoryContext(); context.frame = Pixels(['Nav_CommonConfirm'])
        monitor = BattleOutcomeMonitor(context, {**POLICY, 'revive_next': ['Decline']})
        monitor.probe(); monitor.close()
        decision_frame = monitor.error.frame
        context.frame = Pixels(['Result_RevivePrompt'])
        with patch('battle_outcomes.save_unknown_frame') as save:
            monitor.recheck_unknown_dialog(wait=lambda _: None)
            save.assert_called_once_with(decision_frame)
        self.assertEqual(context.samples, 2)

    def test_common_confirmation_stops_before_a_result_behind_it(self):
        context=MemoryContext()
        context.frame=Pixels(['Nav_CommonConfirm','Result_Failed'])
        monitor=BattleOutcomeMonitor(context,POLICY)
        monitor.start()
        self.assertIsNotNone(monitor.error)
        self.assertIsNone(monitor.branch)
        self.assertIsNone(monitor._thread)
        monitor.close()

    def test_unrecognized_modal_is_distinct_from_known_blocker(self):
        for is_blocked in (False, True):
            context=MemoryContext()
            context.frame=Pixels(['Nav_CommonConfirm']+(['Nav_BlockedDialog'] if is_blocked else []))
            monitor=BattleOutcomeMonitor(context,{**POLICY,'revive_next':['Decline','Failed']})
            monitor.start()
            self.assertIsNotNone(monitor.error)
            self.assertEqual(getattr(monitor.error,'reason',None),None if is_blocked else 'unrecognized_dialog')
            self.assertIsNone(monitor.branch)
            monitor.close()

    def test_known_sell_confirmation_remains_terminal(self):
        context=MemoryContext();context.frame=Pixels(['Nav_CommonConfirm','Nav_See_profile_sell'])
        monitor=BattleOutcomeMonitor(context,{**POLICY,'revive_next':['Decline','Failed']})
        monitor.start()
        self.assertIsNotNone(monitor.error)
        self.assertNotEqual(getattr(monitor.error,'reason',None),'unrecognized_dialog')
        monitor.close()

    def test_missing_blocker_recognition_cannot_be_classified_as_unknown(self):
        context=MemoryContext();context.frame=Pixels(['Nav_CommonConfirm'])
        original=context.run_recognition
        context.run_recognition=lambda name,frame: None if name=='Nav_BlockedDialog' else original(name,frame)
        monitor=BattleOutcomeMonitor(context,{**POLICY,'revive_next':['Decline','Failed']})
        monitor.start()
        self.assertIsNotNone(monitor.error)
        self.assertNotEqual(getattr(monitor.error,'reason',None),'unrecognized_dialog')
        monitor.close()
        context=MemoryContext();context.frame=Pixels(['Nav_CommonConfirm'])
        original=context.run_recognition
        context.run_recognition=lambda name,frame: None if name=='Nav_See_profile_sell' else original(name,frame)
        monitor=BattleOutcomeMonitor(context,{**POLICY,'revive_next':['Decline','Failed']})
        monitor.start()
        self.assertIsNotNone(monitor.error)
        self.assertNotEqual(getattr(monitor.error,'reason',None),'unrecognized_dialog')
        monitor.close()

    def test_revive_prompt_interrupts_before_underlying_result_and_never_sends_input(self):
        context=MemoryContext()
        context.frame=Pixels(['Result_RevivePrompt','Result_Failed','Result_Succeeded'])
        observed=[]
        recognize=context.run_recognition
        context.run_recognition=lambda n,f:(observed.append(n),recognize(n,f))[1]
        monitor=BattleOutcomeMonitor(context,{**POLICY,'revive_next':['Decline','Failed']})
        monitor.start()
        self.assertEqual(monitor.branch,['Decline','Failed'])
        self.assertEqual(observed,['Nav_ModalPresent','Result_RevivePrompt'])
        self.assertIsNone(monitor.error)
        self.assertIsNone(monitor._thread)
        monitor.close()

    def test_revive_detection_is_opt_in_and_requires_explicit_safe_branch(self):
        for value in (None,[],[''], 'Decline'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                BattleOutcomeMonitor(MemoryContext(),{**POLICY,'revive_next':value})

    def test_initial_outcome_and_same_frame_detection_need_no_worker(self):
        for name, branch in [('Result_Failed','Failed'), ('Result_Succeeded','Won')]:
            with self.subTest(name=name):
                context = MemoryContext()
                context.frame = Pixels([name])
                seen = []
                recognize = context.run_recognition
                context.run_recognition = lambda n,f:(seen.append(f), recognize(n,f))[1]
                monitor = BattleOutcomeMonitor(context, POLICY)
                monitor.start()
                self.assertEqual(monitor.branch, [branch])
                self.assertIs(seen[0], seen[1])
                self.assertIsNot(seen[0], context.frame)
                self.assertIsNone(monitor._thread)
                monitor.close()
    def test_worker_detects_mid_recording_failure_and_closes(self):
        context = MemoryContext()
        monitor = BattleOutcomeMonitor(context, POLICY)
        monitor.start()
        try:
            context.frame = Pixels(['Result_Failed'])
            self.assertTrue(context.observed.wait(2))
        finally:
            monitor.close()
        self.assertEqual(monitor.branch, ['Failed'])
        self.assertFalse(monitor._thread.is_alive())
    def test_single_missing_frame_is_retried_before_stopping_playback(self):
        context=MemoryContext();context.frame=Pixels(['Result_Failed'])
        def capture():
            context.samples+=1
            return SimpleNamespace(wait=lambda:SimpleNamespace(succeeded=context.samples>=2))
        context.post_screencap=capture
        monitor=BattleOutcomeMonitor(context,POLICY)
        monitor.start()
        self.assertEqual(context.samples,2)
        self.assertIsNone(monitor.error)
        self.assertEqual(monitor.branch,['Failed'])
        monitor.close()

    def test_stop_during_missing_frame_prevents_further_captures(self):
        context=MemoryContext()
        def capture():
            context.samples+=1
            context.tasker.stopping=True
            return SimpleNamespace(wait=lambda:SimpleNamespace(succeeded=False))
        context.post_screencap=capture
        monitor=BattleOutcomeMonitor(context,POLICY)
        monitor.start()
        self.assertEqual(context.samples,1)
        self.assertIsNone(monitor.error)
        self.assertIsNone(monitor.branch)
        monitor.close()

    def test_capture_invalid_resource_and_ambiguous_result_stop_without_retry(self):
        for mode in ('failed_capture', 'bad_frame', 'missing', 'ambiguous'):
            with self.subTest(mode=mode):
                context = MemoryContext()
                if mode == 'failed_capture': context.capture_ok = False
                if mode == 'bad_frame': context.frame = None
                if mode == 'missing': context.missing = True
                if mode == 'ambiguous': context.frame = Pixels(['Result_Failed','Result_Succeeded'])
                monitor = BattleOutcomeMonitor(context, POLICY)
                monitor.start()
                self.assertIsNotNone(monitor.error)
                self.assertTrue(monitor.interrupted)
                self.assertIsNone(monitor.branch)
                monitor.close()
    def test_cancelled_task_does_not_capture_or_spawn_worker(self):
        context = MemoryContext()
        context.tasker.stopping = True
        monitor = BattleOutcomeMonitor(context, POLICY)
        monitor.start()
        self.assertEqual(context.samples, 0)
        self.assertIsNone(monitor._thread)
        monitor.close()
    def test_broken_reverse_call_is_reported_and_never_silently_kills_worker(self):
        class BrokenTasker:
            @property
            def stopping(self): raise RuntimeError('broken Agent channel')
        context=MemoryContext()
        context.tasker=BrokenTasker()
        monitor=BattleOutcomeMonitor(context,POLICY)
        monitor.start()
        self.assertIsInstance(monitor.error,RuntimeError)
        self.assertIsNone(monitor._thread)
        monitor.close()
    def test_policy_requires_explicit_bounded_branches(self):
        for patch in ({'failed_next':[]}, {'default_next':None}, {'interval_ms':1}, {'unknown':True},
                      {'error_next':[]}, {'error_next':'Fatal'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                BattleOutcomeMonitor(MemoryContext(), {**POLICY, **patch})


if __name__ == '__main__': unittest.main()
