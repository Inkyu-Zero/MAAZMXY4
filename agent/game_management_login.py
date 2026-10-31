"""Bounded login through the existing box UI; no password/cache/token access."""
import json
import re
import time

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from script_task import _check, _new_state, capture, write_status, Cancelled, white_frame


def login_overrides(account, role, slot):
    if not isinstance(account,str) or not account.strip() or len(account)>100:
        raise ValueError('请设置盒子历史账号名称。')
    if not isinstance(role,str) or not role.strip() or len(role)>100 or type(slot) is not int or not 1<=slot<=8:
        raise ValueError('请设置已有存档及角色核对名称。')
    def ocr(text,roi):return dict(type='OCR',param=dict(expected=['^'+re.escape(text)+'$'],roi=roi,threshold=.8))
    game4=dict(type='OCR',param=dict(expected=['^造梦西游4$'],roi=[80,510,360,130],threshold=.75))
    selected=ocr(account,[440,125,340,80])
    role_roi=[328 if slot%2 else 655, 150+118*((slot-1)//2), 250,105]
    role_match=ocr(role,role_roi)
    # Real save tiles render the role and Lv. together as one OCR line.
    # Match the configured literal name, optionally followed by a level, rather
    # than accepting a substring that could select a different character.
    role_match['param']['expected']=['^'+re.escape(role)+r'(?:\s*[Ll][Vv][.· :]*[0-9]+)?$']
    return {
        'GameManagement_SelectAccount':dict(recognition=dict(type='And',param=dict(all_of=[
            dict(recognition=ocr(account,[440,125,340,390])),dict(recognition=game4)],box_index=0)),
            action=dict(type='Click',param=dict(target=True)),next=[],post_delay=300),
        'GameManagement_AccountSelected':dict(recognition=selected,action=dict(type='DoNothing'),next=[]),
        'GameManagement_LoginBox':dict(recognition=dict(type='And',param=dict(all_of=[
            dict(recognition=game4),dict(recognition=selected),'GameManagement_BoxLogin'],box_index=0)),
            action=dict(type='Click',param=dict(target=True)),next=[],post_delay=500),
        'GameManagement_ChooseRole':dict(recognition=dict(type='And',param=dict(all_of=[
            'GameManagement_Slot'+str(slot),dict(recognition=role_match)],box_index=1)),
            action=dict(type='Click',param=dict(target=True)),next=[],post_delay=500),
    }


def restore(context, params, *, clock=time.monotonic, sleep=time.sleep):
    patch=login_overrides(params.get('account_name'),params.get('role_name'),params.get('save_slot'))
    if not context.override_pipeline(patch):raise ValueError('恢复识别参数未载入。')
    deadline=clock()+120
    account_opened=False; submitted=False; role_confirmed=False; last_action=None; last_time=0
    last_frame=None;last_phase=None
    failed_actions={}
    def phase(message):
        nonlocal last_phase
        if message!=last_phase:
            print('[game_management] '+message,flush=True);last_phase=message
    def perform(name):
        nonlocal last_action,last_time
        _check(context)
        if name==last_action and clock()-last_time<2:return False
        # Entry actions bypass recognition in Maa; always dispatch through a wrapper.
        result=context.run_task('GameManagement_Invoke',{'GameManagement_Invoke':dict(
            action=dict(type='DoNothing'),next=[name],timeout=3000,post_delay=0)})
        _check(context)
        last_action,last_time=name,clock()
        if result is None:raise ValueError('恢复操作未确认：'+name)
        if not result.status.succeeded:
            failed_actions[name]=failed_actions.get(name,0)+1
            if failed_actions[name]>=3:raise ValueError('恢复操作未确认：'+name)
            phase('恢复画面可能已切换，重新识别后再决定操作：'+name)
            return False
        failed_actions.pop(name,None)
        return True
    while clock()<deadline:
        _check(context); frame=capture(context);last_frame=frame
        if abs(frame.shape[1]-1147)>2 or frame.shape[0]!=720:
            raise ValueError('盒子画面比例与已适配的 940×590 布局不一致，请恢复窗口比例。')
        def hit(name):
            detail=context.run_recognition(name,frame)
            if detail is None:raise ValueError('缺少恢复节点：'+name)
            return detail.hit
        if hit('GameManagement_Blocking'):
            raise ValueError('登录需要人工处理验证码或账号错误；处理后再继续，未尝试其它账号。')
        if hit('GameManagement_Loading'):
            phase('正在加载游戏，等待画面完成，不发送输入。')
        elif hit('GameManagement_ResumeMap'):
            phase('已识别地图暂停提示。')
            if not role_confirmed and not params.get('allow_existing_map',False):
                raise ValueError('尚未核对目标存档，未操作已有游戏。')
            perform('GameManagement_ResumeMap')
        elif hit('GameManagement_MapReady'):
            if not role_confirmed and not params.get('allow_existing_map',False):
                raise ValueError('恢复过程未核对角色存档。')
            phase('已核对存档并到达地图，恢复完成。');return
        elif role_confirmed:
            # Selecting a save can briefly expose the title behind the closing
            # dialog. It is not a new login request: wait for the verified map.
            phase('已选择并核对存档，等待进入地图，不重复开始游戏或选择存档。')
        elif hit('GameManagement_SavePage'):
            phase('已进入存档页，核对指定位置与角色。')
            if not hit('GameManagement_ChooseRole'):raise ValueError('指定存档为空或角色名称不符，已停止。')
            if perform('GameManagement_ChooseRole'):role_confirmed=True
        elif hit('GameManagement_StartGame'):
            phase('已识别开始游戏页面。')
            perform('GameManagement_StartGame')
        elif not submitted and hit('GameManagement_BoxLogin'):
            phase('已识别盒子登录页，核对已保存账号。')
            if account_opened:
                if hit('GameManagement_SelectAccount'):
                    if perform('GameManagement_SelectAccount'):account_opened=False
            elif hit('GameManagement_AccountSelected'):
                if perform('GameManagement_LoginBox'):submitted=True
            elif not account_opened:
                if perform('GameManagement_OpenAccounts'):account_opened=True
        elif account_opened and not submitted:
            if hit('GameManagement_SelectAccount'):
                if perform('GameManagement_SelectAccount'):account_opened=False
        end=min(clock()+.3,deadline)
        while clock()<end:
            _check(context);sleep(min(.05,end-clock()))
    if white_frame(last_frame):raise TimeoutError('white_screen')
    raise ValueError('等待登录、存档或地图超时；请核对当前画面后重试。')


@AgentServer.custom_action('game_management_login')
class GameManagementLogin(CustomAction):
    def run(self,context,argv):
        state=None
        try:
            params=argv.custom_action_param
            if isinstance(params,str):params=json.loads(params)
            state=_new_state(params);write_status(state,'running','managed_login')
            restore(context,params)
            write_status(state,'ready');return True
        except Cancelled:
            if state is not None:write_status(state,'cancelled','user_cancelled')
        except TimeoutError:
            if state is not None:write_status(state,'recoverable','white_screen')
        except Exception as error:
            print('[game_management] '+str(error),flush=True)
            if state is not None:write_status(state,'fatal','login_requires_attention',message=str(error))
        return False
