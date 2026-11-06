"""Build declarative UI profiles; one shared native round action owns the lifecycle."""
from copy import deepcopy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY_PRESS_MS = 300

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf8', newline='\n')

def node(recognition=None, action=None, next=(), **options):
    options.setdefault('pre_delay', 0)
    return dict(recognition=recognition or dict(type='DirectHit'), action=action or dict(type='DoNothing'),
                next=list(next), post_delay=0, **options)

def custom(name, **params):
    return dict(type='Custom', param=dict(custom_action=name, custom_action_param=params))

def parameters(name, entry, binding, play, target, scene, *, kind, repeat=None, title=None):
    navigation=dict(kind=kind,target_node=target,label=name)
    if title: navigation.update(title=title[0],title_aliases=title[1:])
    flow=dict(scene_node=scene, load_timeout_ms=15000)
    if repeat: flow['repeat_node']=repeat
    return dict(phase='begin',file='@task:'+binding,task_binding_id=binding,playback_node=play,
                navigation=navigation, settings_node='脚本任务设置', exit_mode='battle_only',
                failure_retry=dict(enabled=False,max_retries=3),round_flow=flow,
                result_guard=dict(failed_next=['Result_Failed'],succeeded_next=['Result_Succeeded'],
                                  revive_next=['Result_RevivePrompt'],error_next=['Result_MonitorStopped']))

def round_node(entry, params, repeat=False):
    return node(action=custom('script_task_state',**params),next=[entry,'停止运行'] if repeat else (),max_hit=1)

def play_node(scene, binding=None):
    params=dict(script_binding=dict(recorded_return_next=['Round_PlayDone']))
    if binding: params['file']='@task:'+binding
    return node(recognition=scene,action=custom('battle_play',**params),next=['Round_PlayDone'])

def build_bounties(root=ROOT):
    from task_catalog import task_key
    folder=root/'assets/resource/base/pipeline'
    template=json.loads((root/'tools/bounty_task_template.json').read_text(encoding='utf8'))
    rec=template['recognizers']
    stage=dict(type='And',param=dict(all_of=[dict(recognition=dict(type='TemplateMatch',
               param=dict(template=template['scenes']))),'Result_NoRevivePrompt']))
    common={'Round_PlayDone':node(),
            'Nav_BountyDetailTitle':node(dict(type='OCR',param=dict(expected=['^__unconfigured_bounty__$'],roi=[500,165,430,75]))),
            'Bounty_Target':node(rec['target'],dict(type='LongPress',param=dict(duration=ENTRY_PRESS_MS))),
            'Bounty_ResumeStage':node(rec['resume_stage'],dict(type='Click')),
            'Bounty_StageSettings':node(rec['settings_stage']),
            'Bounty_StagePause':node(rec['paused_stage']),
            'Bounty_Play':play_node(stage),
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
        params=parameters(name,entry,binding,'Bounty_Play','Bounty_Target','Bounty_Play',kind='bounty',
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
        params=parameters(name,entry,binding,play,target,scene,kind='stage',repeat=repeat)
        target_definition=deepcopy(profile['target']);target_definition['next']=[]
        # No delay between recognition and dispatch: a dialog can appear there.
        target_definition['pre_delay']=0
        # Native Click holds for 100 ms. Entry targets use an explicit 300 ms
        # press and release dwell before the shared background hover cleanup.
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
        scene_rec=deepcopy(profile['scene']['recognition'])
        if scene_rec.get('type')!='And':
            scene_rec=dict(type='And',param=dict(all_of=[dict(recognition=scene_rec),'Result_NoRevivePrompt']))
        graph={entry:round_node(entry,params,True),
               repeat:node(action=custom('loop_count',label=name),max_hit=1),
               target:target_definition,scene:node(scene_rec),play:play_node(scene_rec,binding)}
        write(folder/(name+'.json'),graph)

if __name__=='__main__':
    import sys
    sys.path.insert(0,str(ROOT/'agent'))
    build()
