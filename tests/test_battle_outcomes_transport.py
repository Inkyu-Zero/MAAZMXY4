"""Opt-in background recognition through a real, isolated Maa Agent channel."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT=Path(__file__).resolve().parents[1]


def native_case():
    import numpy as np
    from maa.agent_client import AgentClient
    from maa.controller import CustomController
    from maa.resource import Resource
    from maa.tasker import Tasker
    from fixture_pixels import read_png_bgr
    failed=read_png_bgr(ROOT/'tests/fixtures/navigation/challenge-failed.png')
    class PixelsOnly(CustomController):
        frames=0
        def connect(self): return True
        def request_uuid(self): return 'background-outcome-agent-memory'
        def screencap(self):
            self.frames+=1
            return failed.copy() if self.frames>=3 else np.zeros_like(failed)
        def forbidden(self,*args): raise AssertionError('Device input is forbidden')
        click=swipe=touch_down=touch_move=touch_up=key_down=key_up=input_text=start_app=stop_app=forbidden
    with tempfile.TemporaryDirectory(prefix='outcome-agent-') as temporary:
        directory=Path(temporary).resolve()
        log=ROOT/'.runtime-cache/native-outcome-transport'
        log.mkdir(parents=True,exist_ok=True)
        Tasker.set_log_dir(log)
        (directory/'pipeline').mkdir()
        (directory/'image').mkdir()
        import shutil
        definitions=json.loads((ROOT/'assets/resource/base/pipeline/battle_outcomes.json').read_text(encoding='utf8'))
        pipeline={name:definitions[name] for name in ('Result_Failed','Result_Succeeded','Nav_ModalPresent')}
        pipeline.update(json.loads((ROOT/'assets/resource/base/pipeline/navigation_common_ui.json').read_text(encoding='utf8')))
        def templates(value):
            if isinstance(value,dict):
                for key,child in value.items():
                    if key=='template':yield from ([child] if isinstance(child,str) else child)
                    else:yield from templates(child)
            elif isinstance(value,list):
                for child in value:yield from templates(child)
        for path in set(templates(pipeline)):
            target=directory/'image'/path
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(ROOT/'assets/resource/base/image'/path,target)
        pipeline['Probe']={'action':{'type':'Custom','param':{'custom_action':'outcome_probe'}},'post_delay':0}
        (directory/'pipeline/probe.json').write_text(json.dumps(pipeline),encoding='utf8')
        server=directory/'server.py'
        server.write_text(textwrap.dedent('''\
            import sys,time
            sys.path.insert(0,sys.argv[2])
            from maa.agent.agent_server import AgentServer
            from maa.custom_action import CustomAction
            from battle_outcomes import BattleOutcomeMonitor
            @AgentServer.custom_action('outcome_probe')
            class Probe(CustomAction):
                def run(self,context,argv):
                    monitor=BattleOutcomeMonitor(context,dict(failed_next=['Defeat'],succeeded_next=['Won'],default_next=['Exit'],interval_ms=200))
                    try:
                        monitor.start()
                        deadline=time.monotonic()+4
                        while not monitor.interrupted and time.monotonic()<deadline:
                            time.sleep(.02)
                        return monitor.branch==['Defeat'] and monitor.error is None
                    finally: monitor.close()
            AgentServer.start_up(sys.argv[1])
            AgentServer.join()
            AgentServer.shut_down()
            '''),encoding='utf8')
        controller,resource,tasker=PixelsOnly(),Resource(),Tasker()
        assert controller.post_connection().wait().succeeded
        controller.set_screenshot_use_raw_size(True)
        assert resource.post_bundle(directory).wait().succeeded
        assert tasker.bind(resource,controller)
        client=AgentClient()
        client.set_timeout(6000)
        assert client.bind(resource)
        with (directory/'server.log').open('wb') as output:
            process=subprocess.Popen([sys.executable,'-B','-E','-s','-u',str(server),client.identifier,str(ROOT/'agent')],
                cwd=directory,stdout=output,stderr=output,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                assert client.connect()
                job=tasker.post_task('Probe').wait()
                report=dict(succeeded=job.succeeded,alive=client.alive,frames=controller.frames)
                print('RESULT '+json.dumps(report),flush=True)
                if not job.succeeded:
                    print((directory/'server.log').read_text(encoding='utf8',errors='replace')[-3000:],flush=True)
            finally:
                process.terminate()
                process.wait(timeout=5)
    os._exit(0)


@unittest.skipUnless(os.environ.get('MAA_AGENT_NATIVE_TESTS')=='1','opt-in Agent IPC memory test')
class OutcomeTransportTests(unittest.TestCase):
    def test_background_monitor_can_capture_and_recognize_through_agent_rpc(self):
        environment=dict(os.environ)
        environment.setdefault('MAAFW_BINARY_PATH',str(ROOT/'runtimes/win-x64/native'))
        result=subprocess.run([sys.executable,'-B','-X','utf8',str(Path(__file__).resolve()),'--native-case'],
            capture_output=True,text=True,encoding='utf8',errors='replace',env=environment,timeout=45)
        self.assertEqual(result.returncode,0,result.stdout[-3000:]+result.stderr[-1000:])
        lines=[line[7:] for line in result.stdout.splitlines() if line.startswith('RESULT ')]
        self.assertEqual(len(lines),1,result.stdout[-3000:])
        report=json.loads(lines[0])
        output=ROOT/'.runtime-cache/native-outcome-transport.json'
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,indent=2),encoding='utf8')
        self.assertTrue(report['succeeded'],result.stdout[-3000:])
        self.assertTrue(report['alive'])
        self.assertGreaterEqual(report['frames'],3)


if __name__=='__main__':
    if '--native-case' in sys.argv: native_case()
    else: unittest.main()
