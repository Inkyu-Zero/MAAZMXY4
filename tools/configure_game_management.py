"""Reuse existing verified game login/save/map recognizers for managed sessions."""
from copy import deepcopy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def configure(root=ROOT):
    folder = root/'assets/resource/base/pipeline'
    source = json.loads((folder/'悬赏（守卫宝烛）.json').read_text(encoding='utf8'))
    nodes = {}
    for target, suffix in [('StartGame','恢复登录页'),('SavePage','恢复存档页'),
                           ('MapReady','恢复地图就绪'),('ResumeMap','恢复地图暂停'),
                           *[(f'Slot{i}',f'恢复存档{i}') for i in range(1,5)]]:
        original = source['悬赏（守卫宝烛）-'+suffix]
        nodes['GameManagement_'+target] = {key:deepcopy(original[key]) for key in ('recognition','action','post_delay')}
        nodes['GameManagement_'+target].update(next=[],timeout=3000)
    def ocr(expected,roi):return dict(type='OCR',param=dict(expected=[expected],roi=roi,threshold=.75))
    # The current save dialog has four rows of two tiles. Every added slot
    # requires an occupied Lv. line inside its own tile and the save header;
    # game_management_login additionally requires the exact configured role.
    for slot in range(5,9):
        roi=[328 if slot%2 else 655,154+118*((slot-1)//2),230,56]
        nodes['GameManagement_Slot'+str(slot)]=dict(recognition=dict(type='And',param=dict(all_of=[
            dict(recognition=ocr('[Ll][Vv][.· :]*[0-9]+',roi)), 'GameManagement_SavePage'],box_index=0)),
            action=dict(type='Click',param=dict(target=True)),next=[],post_delay=400,timeout=3000)
    nodes['GameManagement_Loading']=dict(recognition=dict(type='And',param=dict(all_of=[
        dict(recognition=ocr('正在加载',[330,480,500,140])),
        dict(recognition=ocr('[0-9]+%',[330,480,500,140]))])),
        action=dict(type='DoNothing'),next=[],post_delay=0)
    game4 = ocr('^造梦西游4$',[80,510,360,130])
    game5 = ocr('^造梦西游5$',[420,510,380,130])
    # The original AIR stage is 940x590; all coordinates use Maa short side 720.
    # LoginLayer lays out its ComboBox at (370,110), scale 2.5. Clicking its
    # interior is authorized only while both game buttons and field labels match.
    box = dict(type='And',param=dict(all_of=[dict(recognition=game4),dict(recognition=game5),
        dict(recognition=ocr('^账号$',[350,240,170,110])),dict(recognition=ocr('^密码$',[350,310,170,110]))]))
    nodes['GameManagement_BoxLogin'] = dict(recognition=box,action=dict(type='DoNothing'),next=[],post_delay=0)
    nodes['GameManagement_OpenAccounts'] = dict(recognition=deepcopy(box),
        action=dict(type='Click',param=dict(target=[604,165,4,4])),next=[],post_delay=300)
    nodes['GameManagement_Blocking'] = dict(recognition=ocr('验证码|密码错误|账号.*不正确|登录失败|登陆失败|安全验证|异地登录|账号不存在',
        [0,0,1147,720]),action=dict(type='DoNothing'),next=[],post_delay=0)
    nodes['GameManagement_Resume'] = dict(action=dict(type='Custom',param=dict(custom_action='game_management_login')),
        next=[],post_delay=0)
    (folder/'game_management.json').write_text(json.dumps(nodes,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    resources=root/'agent/data/navigation/resources.json'
    manifest=json.loads(resources.read_text(encoding='utf8'))
    if 'game_management.json' not in manifest['pipelines']:manifest['pipelines'].append('game_management.json')
    resources.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8',newline='\n')
    interface_path=root/'assets/interface.json'
    interface=json.loads(interface_path.read_text(encoding='utf8'))
    adventure=json.loads((folder/'生存大冒险.json').read_text(encoding='utf8'))
    entries={name for name,node in adventure.items() if node.get('action',{}).get('param',{}).get('custom_action')=='adventure_init'}
    for task in interface['task']:
        if task['entry'] not in entries:continue
        task['description']=task.get('description','').split('\n\n游戏管理：')[0]+(
            '\n\n游戏管理：答题只在确认整局结算后执行内存或定时重启。异常中断、窗口丢失或连续三分钟无进展时保留日志并停止，'
            '不会自动重新开局或消耗新的冒险次数；“任务失败后自动重启”不对答题中断生效。')
    interface_path.write_text(json.dumps(interface,ensure_ascii=False,indent=4)+'\n',encoding='utf8',newline='\n')


if __name__ == '__main__':configure()
