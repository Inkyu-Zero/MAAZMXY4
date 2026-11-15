"""Input-only native-box speed lease on one explicitly selected Windows HWND.

Send number-row 1/2 twice per switch. Never capture or recognize an outcome.
Window/process identity and input release remain owned by this session.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET

from box_speed import SPEED_KEYS, SpeedLease, validate_speed, validate_recording, verify_proof


def initialize_runtime(root):
    native=Path(root)/'runtimes/win-x64/native'
    if native.is_dir():os.environ.setdefault('MAAFW_BINARY_PATH',str(native))
    # Import native modules on the JSONL owner thread before it blocks on stdin.
    from maa.controller import Win32Controller
    from maa.define import LoggingLevelEnum
    from maa.tasker import Tasker
    Tasker.set_stdout_level(LoggingLevelEnum.Off)
    Tasker.set_log_dir(Path(root)/'debug/box-speed'/str(os.getpid()))


def verify_playback_proof(proof,hwnd,sha256):
    if not isinstance(proof,dict) or proof.get('state')!='ready' or proof.get('speed')!=2:
        raise ValueError('盒子 2 倍速操作尚未准备完成，未加速脚本。')
    # Identity-only construction performs no capture, connection, or input.
    target=NativeSpeedTransport(Path('.'),hwnd,threading.Event())
    try:return verify_proof(proof,hwnd,sha256,target.identity,target.exe_path)
    finally:target.close()


class NativeSpeedTransport:
    def __init__(self, root, hwnd, cancelled):
        from window_geometry import Win32Geometry
        if type(hwnd) is not int or hwnd <= 0:
            raise ValueError('倍速需要明确选择游戏窗口。')
        self.root,self.hwnd,self.cancelled=Path(root),hwnd,cancelled
        self.geometry=Win32Geometry()
        self.identity=self.geometry.inspect(hwnd)
        self.input_lock=threading.Lock()
        self.process=None
        self.input_controller=None
        self.key_may_be_down=None
        kernel=self.geometry.kernel
        kernel.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
        kernel.QueryFullProcessImageNameW.restype=wintypes.BOOL
        self.process=kernel.OpenProcess(0x00101000,False,self.identity['pid'])
        if not self.process:raise RuntimeError('无法确认盒子进程，未切换倍速。')
        try:
            name=ctypes.create_unicode_buffer(32768);length=wintypes.DWORD(len(name))
            if not kernel.QueryFullProcessImageNameW(self.process,0,name,ctypes.byref(length)):
                raise RuntimeError('无法读取盒子程序路径，未切换倍速。')
            kernel.GetProcessTimes.argtypes=[wintypes.HANDLE,*([ctypes.POINTER(wintypes.FILETIME)]*4)]
            kernel.GetProcessTimes.restype=wintypes.BOOL
            created,exited,kernel_time,user_time=(wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(self.process,ctypes.byref(created),ctypes.byref(exited),ctypes.byref(kernel_time),ctypes.byref(user_time)):
                raise RuntimeError('无法读取盒子进程创建时间。')
            self.identity['started_utc_ticks']=504911232000000000+(created.dwHighDateTime<<32)+created.dwLowDateTime
            self.exe_path=str(Path(name.value).resolve())
            descriptor=Path(self.exe_path).parent/'META-INF/AIR/application.xml'
            if not descriptor.is_file() or descriptor.stat().st_size>131072:
                raise ValueError('倍速仅支持现有造梦盒子 AIR 客户端。')
            app=ET.parse(descriptor).getroot()
            if not any(node.tag.rsplit('}',1)[-1]=='id' and node.text=='dusk.zmBox' for node in app.iter()):
                raise ValueError('所选窗口不是造梦盒子，未发送倍速快捷键。')
            self.require_identity()
        except Exception:
            self.close()
            raise

    def alive(self):
        return bool(self.process) and self.geometry.kernel.WaitForSingleObject(self.process,0)==258

    def require_identity(self):
        if not self.alive():raise RuntimeError('原盒子进程已结束。')
        current=self.geometry.identity(self.hwnd)
        if any(current[key]!=self.identity[key] for key in ('pid','class_name')):
            raise RuntimeError('盒子窗口身份已改变，未向其他窗口发送倍速输入。')

    def _key(self,key,down):
        self.require_identity()
        send=self.input_controller.post_key_down if down else self.input_controller.post_key_up
        if not send(key).wait().succeeded:
            raise RuntimeError('盒子未响应倍速输入。')

    def send_speed(self,speed):
        validate_speed(speed)
        if not self.alive():
            if speed==1:return  # A terminated box cannot retain speed.
            raise RuntimeError('原盒子进程已结束。')
        with self.input_lock:
            if speed==2 and self.cancelled.is_set():raise InterruptedError('倍速准备已取消。')
            key=SPEED_KEYS[speed]
            self.key_may_be_down=key
            try:
                self._key(key,True)
                time.sleep(.05)
            finally:
                self._key(key,False)
                self.key_may_be_down=None

    def prepare(self):
        from maa.controller import Win32Controller
        from maa.define import MaaWin32InputMethodEnum
        self.require_identity()
        # No Resource, Tasker, FramePool or OCR model belongs to this worker.
        self.input_controller=Win32Controller(self.hwnd,screencap_method=0,mouse_method=0,
            keyboard_method=MaaWin32InputMethodEnum.SendMessage)
        if not self.input_controller.post_connection().wait().succeeded:
            raise RuntimeError('无法准备盒子后台倍速输入。')

    def apply(self,speed):
        if speed==1 and not self.alive():return
        # Each press has its own identity/cancellation fence and guaranteed up.
        # No toast or screenshot is used to claim that the box accepted it.
        for attempt in range(2):
            self.send_speed(speed)
            if attempt==0:time.sleep(.05)
        # Existing AlertBox overlay can intercept map clicks for 2.1 seconds.
        # This bounded settling wait is not a success check or a per-round wait.
        if speed==2:
            if self.cancelled.wait(2.2):raise InterruptedError('倍速准备已取消。')
        else:time.sleep(2.2)

    def close(self):
        if self.key_may_be_down is not None and self.alive():
            self._key(self.key_may_be_down,False)
            self.key_may_be_down=None
        self.input_controller=None
        if self.process:
            self.geometry.kernel.CloseHandle(self.process)
            self.process=None


class SpeedJob:
    """JSONL owner starts/stops a task-wide lease without blocking stdin."""
    def __init__(self,root,params,*,transport_factory=NativeSpeedTransport):
        from script_library import resolve_recording_reference
        from recording_playback import read_recording
        self.speed=validate_speed(params.get('speed',1))
        path=resolve_recording_reference(Path(root)/'recordings',params['file'])
        recording=read_recording(path,expected_sha256=params['sha256'],strict=True)
        validate_recording(recording,self.speed)
        self.root,self.params,self.factory=Path(root),dict(params),transport_factory
        self.cancel=threading.Event();self.state,self.error='starting',''
        self.transport=None;self.lease=None;self.proof={}
        self.thread=threading.Thread(target=self._run,name='box-speed',daemon=True)
        self.thread.start()

    def _run(self):
        try:
            if self.speed==1:
                self.state='ready';self.cancel.wait();return
            self.transport=self.factory(self.root,self.params['hwnd'],self.cancel)
            if any(self.params.get(key)!=self.transport.identity.get(key) for key in ('pid','started_utc_ticks')):
                raise RuntimeError('准备倍速时盒子进程已变化，未发送输入。')
            if str(Path(self.params.get('exe_path','')).resolve()).casefold()!=str(Path(self.transport.exe_path).resolve()).casefold():
                raise RuntimeError('准备倍速时盒子程序路径已变化。')
            self.transport.prepare()
            if self.cancel.is_set():return
            self.lease=SpeedLease(self.speed,self.transport.apply)
            self.lease.start()
            self.proof=dict(hwnd=self.params['hwnd'],pid=self.transport.identity['pid'],
                started_utc_ticks=self.transport.identity['started_utc_ticks'],
                exe_path=self.transport.exe_path,speed=self.speed,sha256=self.params['sha256'])
            self.state='ready'
            while not self.cancel.wait(.5):
                if not self.transport.alive():self.state='lost';break
        except Exception as error:
            self.error,self.state=str(error),'failed'
        finally:
            if self.state not in ('failed','lost'):self.state='restoring'
            try:
                if self.lease:self.lease.close()
            except Exception as error:
                self.error,self.state='恢复 1 倍速操作未完成：'+str(error),'failed'
            finally:
                try:
                    if self.transport:self.transport.close()
                except Exception as error:
                    self.error,self.state='倍速连接清理失败：'+str(error),'failed'
                if self.state not in ('failed','lost'):self.state='stopped'

    def snapshot(self):
        state=self.state
        if state=='ready' and self.transport:
            if not self.transport.alive():state='lost'
            else:
                try:self.transport.require_identity()
                except Exception as error:
                    self.error=str(error);state='failed';self.cancel.set()
        if self.cancel.is_set() and self.thread.is_alive():state='stopping'
        return dict(state=state,error=self.error,restored=self.lease is None or not self.lease.may_be_enabled,
            capture_active=False,**self.proof)

    def stop(self):
        self.cancel.set()
        # The input-only worker observes cancellation between both presses and
        # during toast settling, then its one lease owner restores exactly once.
        self.thread.join(.1)
        return self.snapshot()
