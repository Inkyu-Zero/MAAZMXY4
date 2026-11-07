import sys
import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
from box_speed import SpeedLease, SpeedRejected, validate_recording, validate_speed, verify_proof
from box_speed_native import SpeedJob


class BoxSpeedPolicyTests(unittest.TestCase):
    def test_only_native_speeds_are_accepted(self):
        for value in (1,2):self.assertEqual(validate_speed(value),value)
        for value in (True,False,0,3,1.5,'2',None,float('nan')):
            with self.assertRaises(ValueError):validate_speed(value)

    def test_default_has_no_native_input_or_confirmation_cost(self):
        calls=[]
        with SpeedLease(1,calls.append) as lease:
            self.assertEqual(lease.start(),1)
        self.assertEqual(calls,[])

    def test_one_task_enables_once_and_always_resets_after_failure(self):
        calls=[]
        with self.assertRaisesRegex(RuntimeError,'script failed'):
            with SpeedLease(2,calls.append) as lease:
                self.assertEqual(lease.start(),2)
                raise RuntimeError('script failed')
        self.assertEqual(calls,[2,1])
        lease.close()
        self.assertEqual(calls,[2,1])

    def test_failed_enable_confirmation_still_attempts_native_reset(self):
        calls=[]
        def apply(speed):
            calls.append(speed)
            if speed==2:raise RuntimeError('not confirmed')
        with self.assertRaisesRegex(RuntimeError,'not confirmed'):
            with SpeedLease(2,apply):self.fail('Playback cannot start')
        self.assertEqual(calls,[2,1])

    def test_explicit_rejection_does_not_issue_another_unsupported_command(self):
        calls=[]
        def apply(speed):calls.append(speed);raise SpeedRejected('60 fps')
        with self.assertRaises(SpeedRejected):
            with SpeedLease(2,apply):self.fail('Rejected speed cannot start playback')
        self.assertEqual(calls,[2])

    def test_failed_reset_retains_ownership_and_reports_failure(self):
        calls=[]
        def apply(speed):
            calls.append(speed)
            if speed==1 and len(calls)==2:raise RuntimeError('reset not confirmed')
        lease=SpeedLease(2,apply);lease.start()
        with self.assertRaisesRegex(RuntimeError,'reset not confirmed'):lease.close()
        self.assertTrue(lease.may_be_enabled)
        self.assertFalse(lease.ready)
        lease.close()
        self.assertFalse(lease.may_be_enabled)
        self.assertEqual(calls,[2,1,1])

    def test_box_shortcuts_are_rejected_only_in_managed_double_speed(self):
        for op in ('key','keydown'):
            for key in (49,50):
                recording=dict(actions=[dict(op=op,key=key)])
                validate_recording(recording,1)
                with self.assertRaises(ValueError):validate_recording(recording,2)
        validate_recording(dict(actions=[dict(op='keydown',key=98),dict(op='keydown',key=65)]),2)

    def test_confirmation_is_bound_to_process_window_and_recording(self):
        proof=dict(state='ready',speed=2,hwnd=42,pid=123,started_utc_ticks=456,exe_path='box.exe',sha256='a'*64)
        identity=dict(pid=123,started_utc_ticks=456)
        self.assertEqual(verify_proof(proof,42,'a'*64,identity,'box.exe'),2)
        for changed in (dict(hwnd=43),dict(pid=124),dict(started_utc_ticks=457),dict(sha256='b'*64),dict(state='stopped'),dict(speed=2.0)):
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                verify_proof(dict(proof,**changed),42,'a'*64,identity,'box.exe')


class SpeedJobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);(self.root/'recordings').mkdir()
        file=self.root/'recordings/test.json'
        file.write_text(json.dumps(dict(version=1,name='fixture',base=[1147,720],duration_ms=10,
            actions=[dict(t=0,op='keydown',key=65),dict(t=10,op='keyup',key=65)])),encoding='utf8')
        self.params=dict(hwnd=42,pid=123,started_utc_ticks=456,exe_path='box.exe',speed=2,file=file.name,sha256=hashlib.sha256(file.read_bytes()).hexdigest())
        self.calls=[];self.dead=False;self.fail_reset=False;self.gate=None
        owner=self
        class Transport:
            identity=dict(pid=123,started_utc_ticks=456);exe_path='box.exe'
            def __init__(self,root,hwnd,cancelled):self.cancelled=cancelled;owner.calls.append(('target',hwnd))
            def prepare(self):owner.calls.append(('prepare',))
            def apply(self,speed):
                owner.calls.append(('apply',speed))
                if speed==2 and owner.gate:
                    owner.gate.wait(2)
                    if self.cancelled.is_set():raise InterruptedError('cancelled')
                if speed==1 and owner.fail_reset:raise RuntimeError('reset missing')
            def send_speed(self,speed):owner.calls.append(('send',speed))
            def alive(self):return not owner.dead
            def require_identity(self):
                if owner.dead:raise RuntimeError('dead')
            def close(self):owner.calls.append(('close',))
        self.factory=Transport

    def wait(self,job,state):
        end=time.monotonic()+3
        while job.snapshot()['state']!=state and time.monotonic()<end:time.sleep(.005)
        self.assertEqual(job.snapshot()['state'],state,job.snapshot())

    def test_ready_job_keeps_speed_until_explicit_task_end(self):
        job=SpeedJob(self.root,self.params,transport_factory=self.factory)
        try:
            self.wait(job,'ready');self.assertEqual(job.snapshot()['pid'],123)
            self.assertEqual(self.calls,[('target',42),('prepare',),('apply',2)])
        finally:job.stop();job.thread.join(3)
        self.assertEqual(job.snapshot()['state'],'stopped')
        self.assertEqual(self.calls[-2:],[('apply',1),('close',)])

    def test_cancellation_during_enable_restores_even_without_acknowledgement(self):
        self.gate=threading.Event();job=SpeedJob(self.root,self.params,transport_factory=self.factory)
        end=time.monotonic()+2
        while ('apply',2) not in self.calls and time.monotonic()<end:time.sleep(.005)
        self.assertIn(('apply',2),self.calls)
        job.stop();self.assertIn(('send',1),self.calls)
        self.gate.set();job.thread.join(3)
        self.assertIn(('apply',1),self.calls)
        self.assertNotEqual(job.snapshot()['state'],'ready')
        self.assertTrue(job.snapshot()['restored'])

    def test_reset_failure_is_reported_after_worker_completion(self):
        job=SpeedJob(self.root,self.params,transport_factory=self.factory)
        self.wait(job,'ready');self.fail_reset=True;job.stop();job.thread.join(3)
        self.assertEqual(job.snapshot()['state'],'failed')
        self.assertFalse(job.snapshot()['restored'])
        self.assertIn('恢复 1 倍速',job.snapshot()['error'])

    def test_dead_original_process_never_becomes_a_ready_proof_for_replacement(self):
        job=SpeedJob(self.root,self.params,transport_factory=self.factory)
        self.wait(job,'ready');self.dead=True;self.wait(job,'lost');job.thread.join(3)
        self.assertEqual(self.calls.count(('target',42)),1)

    def test_changed_recording_fails_before_native_transport_creation(self):
        self.params['sha256']='0'*64
        with self.assertRaises(ValueError):SpeedJob(self.root,self.params,transport_factory=self.factory)
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
