"""Bounded login through the existing box UI; no password/cache/token access."""
import json
import re
import time
from mouse_input import finish_mouse_operation

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction
from script_task import _check, _new_state, capture, write_status, Cancelled, white_frame


def optional_name(value):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError('账号和角色名称须为不超过 100 字的文本。')
    return value.strip()


def login_overrides(account, role, slot):
    account, role = optional_name(account), optional_name(role)
    if type(slot) is not int or not 1 <= slot <= 8:
        raise ValueError('请选择第 1～8 个已有存档。')

    def ocr(pattern, roi, threshold=.8):
        return dict(recognition=dict(type='OCR', param=dict(expected=[pattern], roi=roi, threshold=threshold)))

    def click(parts, index=0, delay=500):
        return dict(recognition=dict(type='And', param=dict(all_of=parts, box_index=index)),
                    action=dict(type='Click', param=dict(target=True)), next=[], post_delay=delay)

    game = ocr('^造梦西游4$', [80,510,360,130], threshold=.75)
    login_parts = [game]
    nodes = {}
    if account:
        pattern = '^' + re.escape(account) + '$'
        selected = ocr(pattern, [440,125,340,80])
        nodes['GameManagement_AccountSelected'] = dict(**selected, action=dict(type='DoNothing'), next=[])
        nodes['GameManagement_SelectAccount'] = click([ocr(pattern, [440,125,340,390]), game], delay=300)
        login_parts.append(selected)
    nodes['GameManagement_LoginBox'] = click([*login_parts, 'GameManagement_BoxLogin'])
    if role:
        pattern = '^' + re.escape(role) + r'(?:\s*[Ll][Vv][.· :]*[0-9]+)?$'
        roi = [328 if slot%2 else 655, 150+118*((slot-1)//2), 250,105]
        nodes['GameManagement_ChooseRole'] = click(['GameManagement_Slot'+str(slot), ocr(pattern, roi)], index=1)
    return nodes


def restore(context, params, *, clock=time.monotonic, sleep=time.sleep):
    slot = params.get('save_slot')
    patch = login_overrides(params.get('account_name'), params.get('role_name'), slot)
    check_account = 'GameManagement_AccountSelected' in patch
    slot_node = 'GameManagement_ChooseRole' if 'GameManagement_ChooseRole' in patch else f'GameManagement_Slot{slot}'
    if not context.override_pipeline(patch):raise ValueError('恢复识别参数未载入。')
    deadline=clock()+120
    account_opened=False; submitted=False; role_confirmed=False; last_action=None; last_time=0
    last_frame=None;last_phase=None
    failed_actions={}
    def phase(message):
        nonlocal last_phase
        if message!=last_phase:
            print('[game_management] '+message,flush=True);last_phase=message
    def perform(name, *, match=None):
        nonlocal last_action,last_time
        _check(context)
        if name==last_action and clock()-last_time<2:return False
        if match is None:
            result=context.run_task('GameManagement_Invoke',{'GameManagement_Invoke':dict(
                action=dict(type='DoNothing'),next=[name],timeout=3000,post_delay=0)})
            succeeded=result is not None and result.status.succeeded
        else:
            # A second OCR pass can see the map after the save page closes.
            # Dispatch from the verified slot on this frame, before recapturing.
            if not match.hit or match.box is None:
                raise ValueError('未取得指定存档的位置，已停止。')
            x,y,w,h=match.box
            if w<=0 or h<=0:raise ValueError('指定存档的位置无效，已停止。')
            result=context.run_action(name,box=[x+w//2,y+h//2,1,1])
            succeeded=result is not None and result.success
        _check(context)
        last_action,last_time=name,clock()
        if result is None:raise ValueError('恢复操作未确认：'+name)
        if not succeeded:
            failed_actions[name]=failed_actions.get(name,0)+1
            if failed_actions[name]>=3:raise ValueError('恢复操作未确认：'+name)
            phase('恢复画面可能已切换，重新识别后再决定操作：'+name)
            return False
        failed_actions.pop(name,None)
        finish_mouse_operation(context.tasker.controller)
        return True
    while clock()<deadline:
        _check(context); frame=capture(context);last_frame=frame
        # The same 940×590 layout can normalize to 1140×720 after a window
        # resize; allow this small variation while retaining the aspect guard.
        if abs(frame.shape[1]-1147)>7 or frame.shape[0]!=720:
            raise ValueError(f'盒子画面比例或截图尺寸不符合登录布局，当前截图为 {frame.shape[1]}×{frame.shape[0]}，'
                             '预期约 1147×720。请还原盒子窗口，将游戏区域调回 940∶590 的比例后重新连接。')
        def recognize(name):
            detail=context.run_recognition(name,frame)
            if detail is None:raise ValueError('缺少恢复节点：'+name)
            return detail
        def hit(name):
            return recognize(name).hit
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
            phase('已进入存档页，检查指定的已有存档。')
            match=recognize(slot_node)
            if not match.hit:raise ValueError('指定存档为空或角色名称不符，已停止。')
            if perform(slot_node,match=match):role_confirmed=True
        elif hit('GameManagement_StartGame'):
            phase('已识别开始游戏页面。')
            perform('GameManagement_StartGame')
        elif not submitted and hit('GameManagement_BoxLogin'):
            phase('已识别盒子登录页，核对已保存账号。' if check_account else '使用盒子当前账号。')
            if account_opened:
                if hit('GameManagement_SelectAccount'):
                    if perform('GameManagement_SelectAccount'):account_opened=False
            elif not check_account or hit('GameManagement_AccountSelected'):
                if perform('GameManagement_LoginBox'):submitted=True
            elif not account_opened:
                if perform('GameManagement_OpenAccounts'):account_opened=True
        elif account_opened and not submitted:
            if hit('GameManagement_SelectAccount'):
                if perform('GameManagement_SelectAccount'):account_opened=False
        end=min(clock()+.3,deadline)
        while clock()<end:
            _check(context)
            remaining=end-clock()
            if remaining<=0:break
            sleep(min(.05,remaining))
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
