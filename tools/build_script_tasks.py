"""Build declarative UI profiles; one shared native round action owns the lifecycle."""
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'agent'))
from mouse_input import ENTRY_PRESS_MS

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf8', newline='\n')

def node(recognition=None, action=None, next=(), **options):
    options.setdefault('pre_delay', 0)
    return dict(recognition=recognition or dict(type='DirectHit'), action=action or dict(type='DoNothing'),
                next=list(next), post_delay=0, **options)

def custom(name, **params):
    return dict(type='Custom', param=dict(custom_action=name, custom_action_param=params))

def parameters(name, entry, binding, play, target, *, kind, repeat=None, title=None):
    navigation=dict(kind=kind,target_node=target,label=name)
    if title: navigation.update(title=title[0],title_aliases=title[1:])
    flow=dict(scene_node='Nav_InStage', load_timeout_ms=15000)
    if repeat: flow['repeat_node']=repeat
    return dict(phase='begin',file='@task:'+binding,task_binding_id=binding,playback_node=play,
                navigation=navigation, settings_node='脚本任务设置', exit_mode='battle_only',
                failure_retry=dict(enabled=False,max_retries=3),round_flow=flow,
                result_guard=dict(failed_next=['Result_Failed'],succeeded_next=['Result_Succeeded'],
                                  revive_next=['Result_RevivePrompt'],error_next=['Result_MonitorStopped']))

def round_node(entry, params, repeat=False):
    return node(action=custom('script_task_state',**params),next=[entry,'停止运行'] if repeat else (),max_hit=1)

def play_node(binding=None):
    params=dict(script_binding=dict(recorded_return_next=['Round_PlayDone']))
    if binding: params['file']='@task:'+binding
    return node(recognition=shared_stage(),action=custom('battle_play',**params),next=['Round_PlayDone'])


def shared_stage():
    return dict(type='And',param=dict(all_of=['Nav_InStage']))

def build_bounties(root=ROOT):
    from task_catalog import task_key
    folder=root/'assets/resource/base/pipeline'
    template=json.loads((root/'tools/bounty_task_template.json').read_text(encoding='utf8'))
    rec=template['recognizers']
    common={'Round_PlayDone':node(),
            'Nav_BountyDetailTitle':node(dict(type='OCR',param=dict(expected=['^__unconfigured_bounty__$'],roi=[500,165,430,75]))),
            'Bounty_Target':node(rec['target'],dict(type='LongPress',param=dict(duration=ENTRY_PRESS_MS))),
            'Bounty_ResumeStage':node(rec['resume_stage'],dict(type='Click')),
            'Bounty_StageSettings':node(rec['settings_stage']),
            'Bounty_StagePause':node(rec['paused_stage']),
            'Bounty_Play':play_node(),
            'Bounty_OpenSettings':node(rec['open_settings'],dict(type='Click'))}
    common['Bounty_Target']['post_delay']=2000
    common['Bounty_ResumeStage']['post_delay']=350
    common['Bounty_OpenSettings']['post_delay']=500
    write(folder/'bounty_common.json',common)
    interface=json.loads((root/'assets/interface.json').read_text(encoding='utf8'))
    for task in interface['task']:
        name,entry=task['name'],task['entry']
        if not name.startswith('悬赏（'): continue
        native=entry.endswith('-刷取次数')
        binding=task_key(name,entry) if native else 'bounty-candle'
        params=parameters(name,entry,binding,'Bounty_Play','Bounty_Target',kind='bounty',
                          repeat=entry if native else None,title=template['titles'][name[3:-1]])
        params['round_flow']['resume_node']='Bounty_ResumeStage'
        if not native:
            params['exit_mode']='recorded_return'
            params['execution']=dict(binding_id=binding,resume_entry='GameManagement_Resume',settings_node='脚本任务设置')
        write(folder/(name+'.json'),{entry:round_node(entry,params,native)})

def build(root=ROOT):
    from task_catalog import task_key
    build_bounties(root)
    profiles=json.loads((root/'tools/script_task_profiles.json').read_text(encoding='utf8'))
    folder=root/'assets/resource/base/pipeline'
    for name,profile in profiles.items():
        entry,play,target,scene,repeat=(profile[k] for k in ('entry','playback_node','target_node','scene_node','repeat_node'))
        binding=task_key(name,entry)
        params=parameters(name,entry,binding,play,target,kind='stage',repeat=repeat)
        target_definition=deepcopy(profile['target']);target_definition['next']=[]
        # No delay between recognition and dispatch: a dialog can appear there.
        target_definition['pre_delay']=0
        # Explicit base dwell; the round applies the confirmed task speed.
        # Release dwell remains separate from background hover cleanup.
        target_definition['action']=dict(type='LongPress',param=dict(duration=ENTRY_PRESS_MS))
        target_definition['post_delay']=1000
        icon=target_definition['recognition']['param']['all_of'][1]
        if profile.get('view_roi'):
            def set_roi(value):
                if isinstance(value,dict):
                    if value.get('type') == 'TemplateMatch':
                        value['param']['roi'] = list(profile['view_roi'])
                    for child in value.values(): set_roi(child)
                elif isinstance(value,list):
                    for child in value: set_roi(child)
            set_roi(icon)
        target_definition['recognition']=dict(type='And',param=dict(
            all_of=['Nav_ClearMap',icon],box_index=1))
        # Preserve legacy node identifiers as aliases, without scene landmarks.
        scene_rec=shared_stage()
        graph={entry:round_node(entry,params,True),
               repeat:node(action=custom('loop_count',label=name),max_hit=1),
               target:target_definition,scene:node(scene_rec),play:play_node(binding)}
        write(folder/(name+'.json'),graph)
    from mainline_tasks import build as build_mainline
    build_mainline(root)
    configure_box_speed(root)


def configure_box_speed(root=ROOT):
    from task_catalog import task_catalog
    supported={task['entry'] for task in task_catalog(root,bindings={}) if task['management_profile'] or task['input_only']}
    path=root/'assets/interface.json'
    interface=json.loads(path.read_text(encoding='utf8'))
    cases=[]
    for speed in (1,2):
        cases.append(dict(name='1 倍（默认）' if speed==1 else '2 倍',pipeline_override={
            '脚本任务设置':dict(action=dict(param=dict(custom_action_param=dict(box_speed=speed)))),
            'GenericScriptPlayback':dict(attach=dict(box_speed=speed))}))
    interface['option']['盒子与脚本倍速']=dict(type='select',label='盒子与脚本倍速',default_case='1 倍（默认）',cases=cases,
        description='默认 1 倍保持现有行为。2 倍会同步启用盒子原生倍速与脚本倍速，结束或停止后恢复 1 倍。\n进入造梦 4 并关闭盒子高帧率后使用；联机副本不适用。只在切换倍速时截图核验。录制内不能包含数字行 1/2 倍速快捷键。')
    for task in interface['task']:
        if task['entry'] not in supported:continue
        options=[name for name in task.get('option',[]) if name!='盒子与脚本倍速']
        options.insert(1 if options and options[0] in ('挂机模式','持续播放') else 0,'盒子与脚本倍速')
        task['option']=options
        if task['entry']=='GenericScriptPlayback':
            task['description']=task['description'].replace('全程不截图、不识别，实时预览暂停',
                '回放期间不截图、不识别，实时预览暂停；选择 2 倍时仅在切换阶段截图核验')
    write(path,interface)

if __name__=='__main__':
    build()
