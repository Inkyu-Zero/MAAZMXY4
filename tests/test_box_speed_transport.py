"""Real JSONL ownership/EOF recovery, with a fake box instead of game input."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]


class SpeedTransportTests(unittest.TestCase):
    def run_case(self,mode):
        with tempfile.TemporaryDirectory(prefix='box-speed-jsonl-') as temp:
            folder=Path(temp);(folder/'recordings').mkdir()
            recording=folder/'recordings/fixture.json'
            recording.write_text(json.dumps(dict(version=1,name='fixture',base=[1147,720],duration_ms=1,
                actions=[dict(t=0,op='keydown',key=65),dict(t=1,op='keyup',key=65)])),encoding='utf8')
            entry=folder/'server.py'
            entry.write_text('''import sys,json,threading,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import box_speed_native as speed
import script_studio_service as service
mode=sys.argv[2]
lock=threading.Lock()
def record(item):
    with lock:
        with Path('events.jsonl').open('a',encoding='utf8') as stream:stream.write(json.dumps(item)+'\\n')
class Box:
    identity=dict(pid=123,started_utc_ticks=456)
    exe_path='fake-box.exe'
    def __init__(self,root,hwnd,cancelled):self.cancelled=cancelled;record(['bind',hwnd])
    def prepare(self):record(['prepare'])
    def alive(self):return True
    def require_identity(self):pass
    def apply(self,factor):
        record(['apply',factor])
        if mode=='enable_failure' and factor==2:raise RuntimeError('native hint missing')
        if mode=='reset_failure' and factor==1:raise RuntimeError('reset hint missing')
        if mode=='slow_reset' and factor==1:time.sleep(2.4);record(['reset_confirmed'])
    def send_speed(self,factor):record(['send',factor])
    def close(self):record(['close'])
original=speed.SpeedJob
speed.SpeedJob=lambda root,params:original(root,params,transport_factory=Box)
speed.initialize_runtime=lambda root:None
sys.argv=[sys.argv[0],'--root',str(Path.cwd())]
service.main()
''',encoding='utf8')
            with (folder/'stderr.log').open('wb') as error:
                process=subprocess.Popen([sys.executable,'-B','-E','-s','-u','-X','utf8',str(entry),str(ROOT/'agent'),mode],
                    cwd=folder,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=error,text=True,encoding='utf8',
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                sequence=0
                def call(method,params=None):
                    nonlocal sequence
                    sequence+=1;process.stdin.write(json.dumps(dict(id=sequence,method=method,params=params or {}))+'\n');process.stdin.flush()
                    reply=json.loads(process.stdout.readline());self.assertEqual(reply['id'],sequence)
                    return reply
                try:
                    params=dict(speed=2,hwnd=42,pid=123,started_utc_ticks=456,exe_path='fake-box.exe',
                        file=recording.name,sha256=hashlib.sha256(recording.read_bytes()).hexdigest())
                    self.assertTrue(call('box_speed_start',params)['ok'])
                    end=time.monotonic()+5
                    while True:
                        status=call('box_speed_status')['result']
                        if status['state']!='starting' or time.monotonic()>end:break
                        time.sleep(.01)
                    if mode=='enable_failure':
                        self.assertEqual(status['state'],'failed');self.assertTrue(status['restored'])
                    else:
                        self.assertEqual(status['state'],'ready');self.assertEqual(status['hwnd'],42)
                        self.assertFalse(status['capture_active'])
                        self.assertFalse(call('box_speed_start',params)['ok'],'A second lease cannot steal the owner')
                    if mode=='reset_failure':
                        status=call('box_speed_stop')['result']
                        while status['state']=='stopping' and time.monotonic()<end:
                            time.sleep(.01);status=call('box_speed_status')['result']
                        self.assertEqual(status['state'],'failed');self.assertFalse(status['restored'])
                    # EOF must restore a live lease even without an explicit stop RPC.
                    process.stdin.close();process.wait(timeout=6)
                    if mode!='reset_failure':self.assertEqual(process.returncode,0,(folder/'stderr.log').read_text(encoding='utf8'))
                finally:
                    if process.poll() is None:process.kill();process.wait(timeout=5)
                    process.stdin.close();process.stdout.close()
            events=[json.loads(line) for line in (folder/'events.jsonl').read_text(encoding='utf8').splitlines()]
            self.assertEqual(sum(event==['apply',2] for event in events),1)
            self.assertIn(['apply',1],events)
            self.assertEqual(events[-1],['close'])
            return status,events

    def test_eof_restores_one_task_owned_speed(self):self.run_case('success')
    def test_eof_waits_for_native_toast_expiry_before_terminating_worker(self):
        _,events=self.run_case('slow_reset')
        self.assertIn(['reset_confirmed'],events)
    def test_missing_enable_hint_never_returns_a_ready_proof(self):self.run_case('enable_failure')
    def test_reset_failure_crosses_jsonl_as_failure(self):self.run_case('reset_failure')


if __name__=='__main__':unittest.main()
