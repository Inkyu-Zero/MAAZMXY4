"""Bounded native-box speed handshake on one explicitly selected Windows HWND.

Only number-row 1/2 messages are sent. OCR belongs to speed switching; this
module never navigates, clicks, changes box settings, or changes script files.
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET

from box_speed import SPEED_KEYS, SpeedLease, SpeedRejected, validate_speed, validate_recording, verify_proof


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
        raise ValueError('盒子 2 倍速尚未确认，未加速脚本。')
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
        self.controller=self.resource=self.tasker=self.input_controller=None
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
        from maa.resource import Resource
        from maa.custom_action import CustomAction
        self.require_identity()
        # Reuse Maa's background key transport, separately from capture so a
        # slow screenshot cannot queue the emergency reset behind itself.
        self.input_controller=Win32Controller(self.hwnd,screencap_method=0,mouse_method=0,
            keyboard_method=MaaWin32InputMethodEnum.SendMessage)
        if not self.input_controller.post_connection().wait().succeeded:raise RuntimeError('无法准备盒子后台倍速输入。')
        self.resource=Resource()
        bundle=self.root/'resource/base'
        if not bundle.is_dir():bundle=self.root/'assets/resource/base'
        if not self.resource.post_bundle(bundle).wait().succeeded:raise RuntimeError('倍速核验资源加载失败。')
        transport=self
        class Probe(CustomAction):
            def run(self,context,argv):
                try:return transport._verify(context)
                except Exception as error:
                    transport.probe_error=str(error)
                    transport.probe_exception=error
                    return False
        self.resource.register_custom_action('__box_speed_probe',Probe())
        if not self.resource.override_pipeline({'__BoxSpeedProbe':dict(pre_delay=0,post_delay=0,next=[],
                action=dict(type='Custom',param=dict(custom_action='__box_speed_probe')))}):
            raise RuntimeError('无法建立倍速核验流程。')
        self.prepare_capture()

    def prepare_capture(self):
        from maa.controller import Win32Controller
        from maa.define import MaaWin32ScreencapMethodEnum
        from maa.tasker import Tasker
        self.require_identity()
        self.controller=Win32Controller(self.hwnd,screencap_method=MaaWin32ScreencapMethodEnum.FramePool,
                                        mouse_method=0,keyboard_method=0)
        if not self.controller.post_connection().wait().succeeded:raise RuntimeError('无法准备盒子倍速核验截图。')
        self.tasker=Tasker()
        if not self.tasker.bind(self.resource,self.controller):raise RuntimeError('无法绑定倍速核验控制器。')
        # Warm resources/capture before emitting the box's short-lived toast.
        if not self.controller.post_screencap().wait().succeeded:raise RuntimeError('盒子截图不可用，未开启倍速。')

    def suspend_capture(self):
        # A live FramePool captures continuously even when nobody requests a
        # screenshot. Destroy its owner between switches, retain only OCR data
        # and the input-only controller while the task is playing.
        self.tasker=self.controller=None

    def _verify(self,context):
        expected=f'^设置\\s*{self.requested}\\s*倍速$'
        for attempt in range(3):
            self.require_identity()
            if self.requested==2 and self.cancelled.is_set():return False
            if attempt and not self.controller.post_screencap().wait().succeeded:
                continue
            frame=self.controller.cached_image
            if frame is None:continue
            height,width=frame.shape[:2]
            cx,cy=width//2,height//2
            roi=[max(0,cx-220),max(0,cy-25),min(440,width),min(90,height-cy+25)]
            def recognize(name,patterns):
                return context.run_recognition(name,frame,{name:dict(recognition=dict(type='OCR',param=dict(
                    expected=patterns,roi=roi,threshold=.8)),action=dict(type='DoNothing'),next=[])})
            result=recognize('__BoxSpeedAck',[expected])
            if result and result.hit:
                # Older box toasts move upward; do not accept an old 1x toast
                # left above a newer 2x toast after an ignored reset key.
                _,y,_,h=result.box
                if cy-14<=y+h/2<=cy+55:return True
            blocked=recognize('__BoxSpeedUnsupported',['60帧不支持倍速调整','进入游戏后生效'])
            if blocked and blocked.hit:
                raise SpeedRejected('盒子拒绝倍速：请进入造梦 4，并关闭盒子“高帧率”后重试。')
            if attempt<2:time.sleep(.15)
        raise RuntimeError(f'未确认盒子已切换为 {self.requested} 倍速；请关闭输入框或弹窗后重试。')

    def apply(self,speed):
        if speed==1 and not self.alive():return
        if self.controller is None:
            if speed==1:self.send_speed(1)  # Restore before a potentially slow capture warm-up.
            self.prepare_capture()
        try:self._apply_and_confirm(speed)
        finally:self.suspend_capture()

    def _apply_and_confirm(self,speed):
        self.send_speed(speed)
        sent=time.monotonic()
        # Source AlertBox takes 300ms to appear; old toasts shift out of the
        # center. This wait exists only at speed switches, never in navigation.
        time.sleep(.35)
        self.requested,self.probe_error,self.probe_exception=speed,'',None
        if not self.controller.post_screencap().wait().succeeded:
            raise RuntimeError('倍速切换后截图失败，无法确认盒子状态。')
        if not self.tasker.post_task('__BoxSpeedProbe').wait().succeeded:
            if isinstance(self.probe_exception,SpeedRejected):raise self.probe_exception
            raise RuntimeError(self.probe_error or '盒子倍速核验失败。')
        # AlertBox is interactive and occupies the map center for 1500ms,
        # then fades for 600ms. Let that native overlay leave before ordinary
        # navigation/the next task receives input. No extra capture is needed.
        remaining=max(0,sent+2.2-time.monotonic())
        if speed==2:
            if self.cancelled.wait(remaining):raise InterruptedError('倍速准备已取消。')
        elif remaining:time.sleep(remaining)

    def close(self):
        if self.key_may_be_down is not None and self.alive():
            self._key(self.key_may_be_down,False)
            self.key_may_be_down=None
        self.tasker=self.resource=self.controller=self.input_controller=None
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
                self.error,self.state='无法确认恢复 1 倍速：'+str(error),'failed'
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
            capture_active=self.transport is not None and getattr(self.transport,'controller',None) is not None,**self.proof)

    def stop(self):
        self.cancel.set()
        # Restore promptly even if the capture callback is still unwinding.
        # The adapter input lock/cancellation fence prevents a later 2x key.
        if self.state=='starting' and self.transport and self.lease and self.lease.may_be_enabled:
            self.transport.send_speed(1)
        self.thread.join(.1)
        return self.snapshot()
