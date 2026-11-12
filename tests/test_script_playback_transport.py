"""Real studio IPC/native input to an owned invisible test window, never a game.

No screen pixels, hooks, foreground activation or daily client are used. The
window message log proves actual mouse coordinates and held-key cleanup.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
from window_geometry import WindowLease, Win32Geometry, SIZING_STYLE


class HiddenWindow:
    def __init__(self, dpi_context=-1):
        self.dpi_context=dpi_context
        self.events=[];self.ready=threading.Event();self.hwnd=0
        self.thread=threading.Thread(target=self.run,daemon=True);self.thread.start()
        if not self.ready.wait(5):raise RuntimeError('hidden window did not start')

    def run(self):
        self.api=ctypes.WinDLL('user32',use_last_error=True)
        self.api.SetThreadDpiAwarenessContext.argtypes=[ctypes.c_void_p]
        self.api.SetThreadDpiAwarenessContext.restype=ctypes.c_void_p
        # Explicitly create the receiver in its own DPI coordinate space.
        if not self.api.SetThreadDpiAwarenessContext(ctypes.c_void_p(self.dpi_context)):
            raise ctypes.WinError(ctypes.get_last_error())
        proc=ctypes.WINFUNCTYPE(ctypes.c_ssize_t,wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM)
        class WNDCLASS(ctypes.Structure):
            _fields_=[('style',wintypes.UINT),('proc',proc),('cls',ctypes.c_int),('win',ctypes.c_int),
                ('instance',wintypes.HINSTANCE),('icon',wintypes.HICON),('cursor',wintypes.HANDLE),
                ('brush',wintypes.HBRUSH),('menu',wintypes.LPCWSTR),('name',wintypes.LPCWSTR)]
        self.api.DefWindowProcW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
        self.api.DefWindowProcW.restype=ctypes.c_ssize_t
        @proc
        def callback(hwnd,msg,wp,lp):
            if msg in (0x100,0x101,0x102,0x200,0x201,0x202,0x204,0x205,0x207,0x208,0x20a,0x20e):
                self.events.append((msg,int(wp),int(lp)))
            if msg==0x10:
                self.api.DestroyWindow(hwnd);return 0
            if msg==2:self.api.PostQuitMessage(0)
            return self.api.DefWindowProcW(hwnd,msg,wp,lp)
        name='MAAZMXY4.InputOnlyTest.'+str(time.time_ns())
        wc=WNDCLASS();wc.proc=callback;wc.name=name
        self.api.RegisterClassW.argtypes=[ctypes.POINTER(WNDCLASS)]
        if not self.api.RegisterClassW(ctypes.byref(wc)):raise ctypes.WinError(ctypes.get_last_error())
        self.api.CreateWindowExW.argtypes=[wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD,
            ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int,wintypes.HWND,wintypes.HMENU,wintypes.HINSTANCE,ctypes.c_void_p]
        self.api.CreateWindowExW.restype=wintypes.HWND
        self.api.DestroyWindow.argtypes=[wintypes.HWND]
        self.hwnd=self.api.CreateWindowExW(0,name,'invisible offline fixture',0x00cf0000,
                                         0,0,900,600,None,None,None,None)
        self.ready.set()
        message=wintypes.MSG()
        while self.api.GetMessageW(ctypes.byref(message),None,0,0)>0:
            self.api.TranslateMessage(ctypes.byref(message));self.api.DispatchMessageW(ctypes.byref(message))
        self.api.UnregisterClassW(name,None)

    def close(self):
        self.api.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
        self.api.PostMessageW(self.hwnd,0x10,0,0);self.thread.join(3)


@unittest.skipUnless(os.name=='nt' and os.environ.get('MAA_AGENT_NATIVE_TESTS')=='1','opt-in native offline test')
class PlaybackTransportTests(unittest.TestCase):
    def setUp(self):
        self.window=HiddenWindow();self.addCleanup(self.window.close)
        self.tmp=tempfile.TemporaryDirectory(prefix='input-only-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'recordings').mkdir()
        self.recording=self.root/'recordings/test.json'
        env=dict(os.environ)
        self.err=(self.root/'worker.err').open('w',encoding='utf8');self.addCleanup(self.err.close)
        self.process=subprocess.Popen([sys.executable,'-B','-E','-s','-u','-X','utf8',str(ROOT/'agent/script_studio_service.py'),'--root',str(self.root)],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.err,text=True,encoding='utf8',env=env,
            cwd=self.root,creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self.close_worker)
        self.sequence=0
        self.geometry=Win32Geometry()
        self.original=self.geometry.style(self.window.hwnd)
        self.user=ctypes.WinDLL('user32');self.user.GetForegroundWindow.restype=wintypes.HWND
        self.foreground=self.user.GetForegroundWindow()

    def close_worker(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            self.process.wait(5)
        self.process.stdout.close()
        evidence=os.environ.get('PLAYBACK_TEST_EVIDENCE')
        if evidence:
            import shutil
            target=Path(evidence)/self._testMethodName
            target.mkdir(parents=True,exist_ok=True)
            for path in self.root.rglob('*.log'):
                shutil.copy2(path,target/path.name)

    def call(self,method,params=None):
        self.sequence+=1
        self.process.stdin.write(json.dumps(dict(id=self.sequence,method=method,params=params or {}))+'\n');self.process.stdin.flush()
        line=self.process.stdout.readline()
        try:response=json.loads(line)
        except ValueError:raise AssertionError('Worker stdout is not JSON: '+repr(line))
        self.assertEqual(response['id'],self.sequence);self.assertTrue(response['ok'],response)
        return response['result']

    def start(self,actions,duration,mouse='SendMessage'):
        self.recording.write_text(json.dumps(dict(version=1,name='native test',base=[1140,720],actions=actions,duration_ms=duration)),encoding='utf8')
        self.call('playback_start',dict(hwnd=self.window.hwnd,file='test.json',mouse=mouse,
            sha256=hashlib.sha256(self.recording.read_bytes()).hexdigest()))

    def wait(self,predicate,seconds=5):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if predicate():return
            time.sleep(.02)
        self.fail('offline IPC condition timed out; '+str(self.call('playback_status'))+' events='+str(self.window.events)
                  +(self.root/'worker.err').read_text(encoding='utf8')[-5000:])

    def target_geometry(self):
        api=self.geometry.api
        api.GetWindowDpiAwarenessContext.argtypes=[wintypes.HWND]
        api.GetWindowDpiAwarenessContext.restype=ctypes.c_void_p
        previous=api.SetThreadDpiAwarenessContext(api.GetWindowDpiAwarenessContext(self.window.hwnd))
        try:
            rect,origin=wintypes.RECT(),wintypes.POINT()
            self.assertTrue(api.GetClientRect(self.window.hwnd,ctypes.byref(rect)))
            self.assertTrue(api.ClientToScreen(self.window.hwnd,ctypes.byref(origin)))
            return rect.right-rect.left,rect.bottom-rect.top,origin.x,origin.y
        finally:api.SetThreadDpiAwarenessContext(previous)

    def parked_event(self):
        physical=self.geometry.inspect(self.window.hwnd)
        width,height,_,_=self.target_geometry()
        x,y=round(20*width/physical['width']),round(20*height/physical['height'])
        return (0x200,0,(y<<16)|x)

    def test_dpi_click_and_wheel_use_the_receiver_coordinate_space(self):
        self.check_dpi_coordinates('SendMessage')

    def test_post_message_click_and_wheel_use_the_receiver_coordinate_space(self):
        self.check_dpi_coordinates('PostMessage')

    def check_dpi_coordinates(self,mouse):
        self.start([dict(t=0,op='down',x=570,y=360),dict(t=30,op='up',x=570,y=360),
                    dict(t=30,op='scroll',dx=120,dy=-120),dict(t=20,op='keydown',key=68),
                    dict(t=10000,op='keyup',key=68)],10080,mouse)
        self.wait(lambda:any(msg==0x100 for msg,_,_ in self.window.events))
        width,height,ox,oy=self.target_geometry()
        for message in (0x201,0x202,0x20a,0x20e):
            value=next(event[2] for event in self.window.events if event[0]==message)
            actual=(ctypes.c_short(value&0xffff).value,ctypes.c_short(value>>16).value)
            offset=(ox,oy) if message in (0x20a,0x20e) else (0,0)
            expected=(round(width/2)+offset[0],round(height/2)+offset[1])
            for a,b in zip(actual,expected):self.assertLessEqual(abs(a-b),1,(message,actual,expected))
        self.assertEqual(self.call('playback_stop')['state'],'stopped')

    def test_real_ipc_mouse_keys_size_lock_and_stop(self):
        self.start([dict(t=0,op='down',x=123,y=321),dict(t=25,op='up',x=125,y=322),
                    dict(t=20,op='keydown',key=68),dict(t=10000,op='keyup',key=68)],10045)
        self.wait(lambda:any(msg==0x100 for msg,_,_ in self.window.events))
        info=self.geometry.inspect(self.window.hwnd)
        self.assertLessEqual(abs(round(info['width']*720/info['height'])-1140),8)
        self.assertFalse(self.geometry.style(self.window.hwnd)&SIZING_STYLE)
        down=next(e for e in self.window.events if e[0]==0x201)
        width,height,_,_=self.target_geometry()
        for a,b in zip((down[2]&0xffff,(down[2]>>16)&0xffff),
                       (round(123*width/1140),round(321*height/720))):
            self.assertLessEqual(abs(a-b),1)
        parked_event=self.parked_event()
        parked=lambda:[e for e in self.window.events if e==parked_event]
        self.assertEqual(parked(),[])
        result=self.call('playback_stop')
        self.assertEqual(result['state'],'stopped',result)
        self.wait(lambda:any(msg==0x101 and wp==68 for msg,wp,_ in self.window.events))
        self.assertEqual(self.geometry.style(self.window.hwnd),self.original)
        self.assertEqual(self.user.GetForegroundWindow(),self.foreground)
        self.wait(lambda:len(parked())==1)
        self.assertLess(self.window.events.index(next(e for e in self.window.events if e[:2]==(0x101,68))),
                        self.window.events.index(parked()[0]))
        # Native logging must contain neither captures nor scaling fallback.
        self.err.flush()
        debug='\n'.join(p.read_text(encoding='utf8',errors='replace') for p in self.root.rglob('*.log'))
        self.assertIn('No screencap method selected',debug)
        self.assertNotIn('MaaControllerPostScreencap',debug)
        self.assertNotIn('ControllerAgent::handle_screencap',debug)
        self.assertNotIn('Invalid image target size',debug)

    def test_eof_releases_inputs_and_window_lock(self):
        self.start([dict(t=0,op='keydown',key=68),dict(t=10000,op='keyup',key=68)],10000)
        self.wait(lambda:any(msg==0x100 for msg,_,_ in self.window.events))
        self.close_worker()
        self.wait(lambda:any(msg==0x101 and wp==68 for msg,wp,_ in self.window.events))
        self.assertEqual(self.geometry.style(self.window.hwnd),self.original)

    def test_completed_playback_and_observation_only_prepare(self):
        before=self.geometry.inspect(self.window.hwnd)
        result=self.call('window_prepare',dict(hwnd=self.window.hwnd))
        self.assertEqual((result['width'],result['height']),(before['width'],before['height']))
        self.assertEqual(self.geometry.style(self.window.hwnd),self.original)
        self.start([dict(t=20,op='click',x=333,y=222)],80)
        states=[]
        def finished():
            states.append(self.call('playback_status'));return states[-1]['state']!='running'
        self.wait(finished)
        self.assertEqual(states[-1]['state'],'succeeded',states)
        self.assertTrue(any(msg==0x201 for msg,_,_ in self.window.events))
        self.assertTrue(any(msg==0x202 for msg,_,_ in self.window.events))
        self.wait(lambda:sum(e==self.parked_event() for e in self.window.events)==1)
        self.assertEqual(self.geometry.style(self.window.hwnd),self.original)

    def test_text_scroll_and_secondary_mouse_use_same_saved_actions(self):
        self.start([dict(t=0,op='down',x=111,y=222,button='right'),
                    dict(t=20,op='up',x=111,y=222,button='right'),
                    dict(t=20,op='text',text='甲A'),dict(t=20,op='scroll',dx=120,dy=-120)],300)
        self.wait(lambda:self.call('playback_status')['state']!='running')
        self.assertEqual(self.call('playback_status')['state'],'succeeded')
        self.assertEqual([wp for msg,wp,_ in self.window.events if msg==0x102],[ord('甲'),ord('A')])
        self.assertTrue(any(msg==0x204 for msg,_,_ in self.window.events))
        self.assertTrue(any(msg==0x205 for msg,_,_ in self.window.events))
        self.assertEqual([(msg,(wp>>16)&0xffff) for msg,wp,_ in self.window.events if msg in (0x20a,0x20e)],
                         [(0x20a,(-120)&0xffff),(0x20e,120)])

    def test_stop_interrupts_long_text_and_releases_modifier(self):
        self.start([dict(t=0,op='keydown',key=17),dict(t=20,op='text',text='A'*1000),
                    dict(t=20,op='keyup',key=17)],40)
        self.wait(lambda:any(msg==0x102 for msg,_,_ in self.window.events))
        started=time.monotonic();result=self.call('playback_stop')
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(result['state'],'stopped')
        self.assertLess(sum(msg==0x102 for msg,_,_ in self.window.events),1000)
        self.assertTrue(any(msg==0x101 and wp==17 for msg,wp,_ in self.window.events))
        self.assertEqual(self.geometry.style(self.window.hwnd),self.original)


if __name__=='__main__':unittest.main()

