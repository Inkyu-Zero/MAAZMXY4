"""Shared round behavior: one observation per transition, real state/retry policy."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4
from test_bounty_candle import load_runtime, Context, Frame, ROOT
from task_catalog import task_catalog
from navigation.runtime import Navigator

runtime=load_runtime()

class RoundTests(unittest.TestCase):
    def run_round(self, task, *, results=('map',), mode='recorded_return', retry=False, afk=False,
                  restart=False, entry_scene='stage', return_after=1, cancel=False, changed=False,
                  observed_failure=False, observed_unknown_dialog=False):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);library=root/'recordings';library.mkdir()
            recording=library/'test.json'
            data=dict(version=1,name='fixture',base=[1148,720],duration_ms=2,
                      actions=[dict(t=0,op='keydown',key=65),dict(t=1,op='keyup',key=65)])
            recording.write_text(json.dumps(data),encoding='utf8')
            context=Context();context.scene='map';context.inputs=[];context.captures=[]
            context.tasker.controller.cached_image=Frame()
            context.play_count=0;context.return_count=0;clock=[0.0]
            profile=task['management_profile'];params=deepcopy(profile['parameters'])
            params.update(file=str(recording),exit_mode=mode,run_id=str(uuid4()),game_management=dict(restart_on_failure=restart))
            play=params['playback_node'];target=params['navigation']['target_node'];scene=params['round_flow']['scene_node']
            context.nodes[play]['action']['param']['custom_action_param']['file']=str(recording)
            context.nodes['脚本任务设置']['action']['param']['custom_action_param'].update(exit_mode=mode,afk_mode=afk,
                failure_retry=dict(enabled=retry,max_retries=1))
            def capture():
                context.captures.append((context.scene,len(context.inputs)))
                return Frame()
            def recognition(name,frame):
                possible={'map':{'Nav_ClearMap','Nav_TaskMap'},'quest':{'Nav_QuestPanel','Nav_SelectBountyTab'},
                    'accepted':{'Nav_AcceptedBounties','Nav_ChooseBounty'},'detail':{target,'Nav_TaskTarget'},
                    'stage':{scene,play,'Bounty_OpenSettings'},'settings':{'Nav_SettingsMenu','Nav_SettingsReturn','Bounty_StageSettings'},
                    'paused':{'Bounty_StagePause','Bounty_ResumeStage'},
                    'failed':{'Result_Failed','Result_FailedClear','Result_FailedReturn'},
                    'succeeded':{'Result_Succeeded','Result_SucceededReturn'},'unknown':set()}
                if context.scene=='map':
                    possible['map'].add('Nav_OpenQuest')
                    if params['navigation']['kind']=='stage':possible['map'].update([target,'Nav_TaskTarget'])
                return SimpleNamespace(hit=name in possible[context.scene],box=[100,100,30,20])
            def action(name,*,box):
                context.inputs.append(name)
                if name==target:context.scene=entry_scene
                elif name=='Nav_OpenQuest':context.scene='quest'
                elif name=='Nav_SelectBountyTab':context.scene='accepted'
                elif name=='Nav_ChooseBounty':context.scene='detail'
                elif name=='Bounty_ResumeStage':context.scene='stage'
                elif name=='Bounty_OpenSettings':context.scene='settings'
                elif name=='Result_FailedReturn':
                    context.return_count+=1
                    if cancel:context.tasker.stopping=True
                    if changed:recording.write_text('{}',encoding='utf8')
                    if context.return_count>=return_after:context.scene='map'
                elif name in ('Nav_SettingsReturn','Result_SucceededReturn'):context.scene='map'
                else:raise AssertionError(name)
                return SimpleNamespace(success=True)
            context.run_recognition=recognition;context.run_action=action
            def navigator(ctx,state,*,targets=None,frame=None):
                routine=runtime.task_routine(ctx,state['parameters'],targets=targets)
                return Navigator(ctx,routine,capture=capture,frame=frame,check=lambda:runtime._check(ctx),
                    clock=lambda:clock[0],wait=lambda delay:clock.__setitem__(0,clock[0]+delay))
            player=ModuleType('battle_player')
            class Player:
                def run(self,ctx,argv):
                    context.inputs.append('play');context.play_count+=1
                    context.scene=results[min(context.play_count-1,len(results)-1)]
                    if observed_failure and context.play_count==1:
                        context.nodes[play]['next']=[dict(name='Result_Failed')]
                    elif observed_unknown_dialog:
                        context.nodes[play]['next']=[dict(name='Result_MonitorUnrecognizedDialog')]
                    else:context.nodes[play]['next']=[dict(name='Round_PlayDone')]
                    return True
            player.BattlePlay=Player
            with patch.multiple(runtime,ROOT=root,RECORDINGS=library,STATUS_DIRECTORY=root/'debug/task-runs'), \
                    patch.object(runtime,'_navigator',side_effect=navigator), \
                    patch.dict(Navigator._modal_state.__globals__,modal_present=lambda *_:False), \
                    patch.dict(sys.modules,{'battle_player':player}),redirect_stdout(io.StringIO()):
                result=runtime.ScriptTaskState().run(context,SimpleNamespace(custom_action_param=params,node_name=profile['entry'],task_detail=SimpleNamespace(task_id=10)))
            status=json.loads((root/'debug/task-runs'/(params['run_id']+'.json')).read_text(encoding='utf8'))
            return result,status,context

    def tasks(self):
        return [t for t in task_catalog(ROOT,bindings={}) if t['management_profile']]

    def test_all_eleven_profiles_reuse_verified_frames_and_preserve_bindings(self):
        for task in self.tasks():
            with self.subTest(task=task['name']):
                ok,status,ctx=self.run_round(task)
                self.assertTrue(ok);self.assertEqual(status['status'],'success')
                self.assertEqual(ctx.play_count,1)
                self.assertEqual(len(ctx.captures),len(ctx.inputs))
                self.assertEqual(len({n for _,n in ctx.captures}),len(ctx.captures))

    def test_existing_map_and_settings_return_are_checked_before_opening_settings(self):
        task=self.tasks()[0]
        for result,expected in [('map',[]),('settings',['Nav_SettingsReturn']),('stage',['Bounty_OpenSettings','Nav_SettingsReturn'])]:
            with self.subTest(result=result):
                ok,status,ctx=self.run_round(task,results=[result],mode='battle_only')
                self.assertTrue(ok)
                self.assertEqual([n for n in ctx.inputs if n in ('Nav_SettingsReturn','Bounty_OpenSettings')],expected)

    def test_failed_round_retries_without_reinitializing_or_counting_success_early(self):
        for afk in (False,True):
            ok,status,ctx=self.run_round(self.tasks()[0],results=['failed','map'],retry=True,afk=afk,restart=True)
            self.assertTrue(ok);self.assertEqual(ctx.play_count,2)
            self.assertEqual(status['failure_retries'],1);self.assertEqual(status['status'],'success')

    def test_retry_exhaustion_hands_off_only_when_enabled(self):
        for restart in (False,True):
            ok,status,ctx=self.run_round(self.tasks()[0],results=['failed'],retry=True,restart=restart)
            self.assertFalse(ok);self.assertEqual(ctx.play_count,2)
            self.assertEqual(status['reason'],'battle_retry_exhausted')
            self.assertEqual(status['status'],'recoverable' if restart else 'fatal')

    def test_disabled_retry_returns_without_playing_again(self):
        ok,status,ctx=self.run_round(self.tasks()[0],results=['failed'])
        self.assertFalse(ok);self.assertEqual(ctx.play_count,1)
        self.assertEqual(ctx.scene,'map');self.assertEqual(status['reason'],'battle_failed')

    def test_pause_settings_and_unknown_loading_do_not_play(self):
        task=next(t for t in self.tasks() if t['management_profile']['parameters']['navigation']['kind']=='stage')
        for scene,reason in [('paused','stage_paused'),('settings','stage_settings_open'),('unknown','navigation_transition_failed')]:
            ok,status,ctx=self.run_round(task,entry_scene=scene)
            self.assertFalse(ok);self.assertEqual(ctx.play_count,0);self.assertEqual(status['reason'],reason)

    def test_return_retry_is_bounded_and_cancel_stops_more_input(self):
        task=self.tasks()[0]
        ok,status,ctx=self.run_round(task,results=['failed'],return_after=2)
        self.assertFalse(ok);self.assertEqual(ctx.return_count,2);self.assertEqual(ctx.scene,'map')
        ok,status,ctx=self.run_round(task,results=['failed'],return_after=100)
        self.assertFalse(ok);self.assertEqual(ctx.return_count,3)
        ok,status,ctx=self.run_round(task,results=['failed'],return_after=100,cancel=True)
        self.assertFalse(ok);self.assertEqual(ctx.return_count,1);self.assertEqual(status['status'],'cancelled')

    def test_recording_change_stops_before_an_additional_return_click(self):
        ok,status,ctx=self.run_round(self.tasks()[0],results=['failed'],return_after=100,changed=True)
        self.assertFalse(ok);self.assertEqual(ctx.return_count,1)
        self.assertEqual(status['reason'],'recording_changed')

    def test_unknown_dialog_survives_input_cleanup_and_does_not_count_success(self):
        for restart in (False,True):
            ok,status,ctx=self.run_round(self.tasks()[0],results=['map'],afk=True,restart=restart,
                                         observed_unknown_dialog=True)
            self.assertFalse(ok)
            self.assertEqual(ctx.play_count,1)
            self.assertEqual(status['status'],'recoverable' if restart else 'fatal')
            self.assertEqual(status['reason'],'task_failed' if restart else 'unrecognized_dialog')
            self.assertEqual(status['failure_retries'],0)

    def test_native_observed_defeat_survives_input_cleanup_returning_to_map(self):
        ok,status,ctx=self.run_round(self.tasks()[0],results=['map'],retry=True,observed_failure=True)
        self.assertTrue(ok);self.assertEqual(ctx.play_count,2)
        self.assertEqual(status['failure_retries'],1)

if __name__=='__main__':unittest.main()
