"""Small native-task actions for saved-script preflight and recovery outcomes.

Maa owns recognition, navigation and input through the project's Pipeline. This
module never drives a second scheduler or restarts/rebinds a controller. A round
pins the saved library file and its hash in memory; the next round resolves the
current binding again. The front end owns bounded process recovery.
"""
import hashlib
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from uuid import UUID, uuid4

from maa.agent.agent_server import AgentServer
from maa.custom_action import CustomAction

from script_library import resolve_recording_reference
from script_studio.model import MAX_FILE_BYTES, RecordingDocument
from task_catalog import custom_parameters
from navigation.catalog import task_graph
from navigation import NavigationError
from navigation.runtime import GraphNavigator
from navigation.stage_view import restore_stage_view, wait_stage_ready

ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = ROOT / 'recordings'
STATUS_DIRECTORY = ROOT / 'debug/task-runs'
_rounds = {}
_lock = threading.RLock()


class Cancelled(RuntimeError):
    pass


class CaptureUnavailable(RuntimeError):
    pass


class RecordingChanged(ValueError):
    pass


def _parameters(argv):
    value = getattr(argv, 'custom_action_param', {})
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError('脚本任务参数必须是对象。')
    return value


def _key(argv):
    value = getattr(getattr(argv, 'task_detail', None), 'task_id', None)
    if type(value) is not int or value <= 0:
        raise ValueError('缺少原生任务标识。')
    return value


def _check(context):
    if context.tasker.stopping:
        raise Cancelled('任务已取消。')


def status_path(parameters):
    ident = parameters.get('run_id') or str(uuid4())
    if not isinstance(ident, str) or str(UUID(ident)) != ident:
        raise ValueError('脚本任务运行标识必须为标准 UUID。')
    expected = STATUS_DIRECTORY / (ident + '.json')
    if (expected.resolve().parent != STATUS_DIRECTORY.resolve()
            or not expected.resolve().is_relative_to(ROOT.resolve())):
        raise ValueError('脚本任务状态目录无效。')
    supplied = parameters.get('status_path')
    if supplied is not None and Path(supplied).resolve() != expected.resolve():
        raise ValueError('脚本任务状态只能保存在本工程调试目录。')
    instance = parameters.get('instance_id', '')
    if not isinstance(instance, str) or len(instance) > 128:
        raise ValueError('脚本任务实例标识无效。')
    return ident, expected, instance


def _new_state(parameters):
    ident, path, instance = status_path(parameters)
    return dict(version=1, run_id=ident, instance_id=instance, status='running', reason='',
                path=path, recording=None, phase=parameters.get('phase'), started_at=time.time())


def write_status(state, status, reason='', **extra):
    payload = {key: value for key, value in state.items() if key not in ('path', 'recording', 'reference')}
    payload.update(status=status, reason=reason, updated_at=time.time(), **extra)
    path = state['path']
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.task-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf8', newline='\n') as stream:
            json.dump(payload, stream, ensure_ascii=False, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    state.update(status=status, reason=reason)
    print('[script_task] %s: %s' % (status, reason or '已确认'), flush=True)


def _stop_status(state, status, reason, **extra):
    try:
        write_status(state, status, reason, **extra)
    except Exception as error:
        # Failure to report must still return False to Maa, never count a round.
        print('[script_task] 无法保存失败状态：' + str(error), flush=True)


def inspect_recording(reference):
    path = resolve_recording_reference(RECORDINGS, reference)
    with path.open('rb') as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('绑定脚本超过16MiB。')
    data = json.loads(raw.decode('utf-8-sig'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('脚本含非有限数值。')))
    document = RecordingDocument.from_dict(data)
    document.to_dict(strict=True)
    if document.diagnostics:
        raise ValueError('绑定脚本存在操作诊断，请先在脚本工具中修复。')
    if not data['actions']:
        raise ValueError('绑定脚本没有可回放的操作。')
    if min(document.base) != 720:
        raise ValueError('任务脚本必须使用新工程的720基准坐标。')
    if any(action.get('op') == 'scroll' and ('x' in action or 'y' in action) for action in data['actions']):
        raise ValueError('带坐标滚轮尚不支持当前控制器回放。')
    return dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(), base=list(document.base),
                name=data.get('name', path.stem), duration_ms=document.duration_ms)


def capture(context):
    _check(context)
    controller = context.tasker.controller
    try:
        result = controller.post_screencap().wait()
    except Exception as error:
        _check(context)
        raise CaptureUnavailable('截图作业不可用。') from error
    _check(context)
    if not getattr(result, 'succeeded', False):
        raise CaptureUnavailable('截图作业失败。')
    try:
        frame = result.get() if hasattr(result, 'get') else controller.cached_image
    except Exception as error:
        raise CaptureUnavailable('控制器截图读取失败。') from error
    if frame is None or getattr(frame, 'ndim', 0) != 3 or frame.shape[2] < 3:
        raise CaptureUnavailable('控制器没有返回有效截图。')
    return frame


def white_frame(frame):
    """Only an almost uniform white client is a positively known Flash fault."""
    if frame is None or getattr(frame, 'ndim', 0) != 3 or min(frame.shape[:2]) < 100:
        return False
    height, width = frame.shape[:2]
    sample = frame[height//20:height-height//20:4, width//20:width-width//20:4, :3]
    return bool((sample >= 248).all(axis=2).mean() >= .995 and sample.std() <= 3)


def confirm_white_screen(context, wait=time.sleep):
    for index in range(3):
        if not white_frame(capture(context)):
            return False
        if index < 2:
            end = time.monotonic() + .2
            while time.monotonic() < end:
                _check(context)
                wait(min(.02, max(0, end-time.monotonic())))
    return True


def _override(context, patch):
    if not context.override_pipeline(patch):
        raise ValueError('Maa拒绝本轮任务参数。')


def begin_round(context, parameters, state):
    _check(context)
    parameters = deepcopy(parameters)
    if parameters.get('navigation') is not None:
        # The option editor already merges overrides. Read that same native
        # node; do not invent a second options store or a task-name whitelist.
        settings = context.get_node_data(parameters.get('settings_node', '脚本任务设置'))
        for key, value in custom_parameters(settings or {}).items():
            if key in ('exit_mode', 'failure_retry', 'recovery', 'afk_mode'):
                parameters[key] = deepcopy(value)
    reference = parameters.get('file', '@task')
    if reference == '@task':
        reference = '@task:' + parameters['task_binding_id']
    playback_node = parameters.get('playback_node')
    if parameters.get('navigation') is not None and isinstance(playback_node, str):
        prepared = custom_parameters(context.get_node_data(playback_node) or {})
        if prepared.get('file'):
            reference = prepared['file']
    else:
        prepared = {}
    recording = inspect_recording(reference)
    if prepared.get('expected_sha256') is not None and prepared['expected_sha256'] != recording['sha256']:
        raise RecordingChanged('本轮脚本在任务准备后已修改，未开始导航。')
    filename = Path(recording['path']).name
    expected_file = parameters.get('expected_recording_file')
    expected_hash = parameters.get('expected_recording_sha256')
    if expected_file is not None or expected_hash is not None:
        if expected_file != filename or expected_hash != recording['sha256']:
            raise RecordingChanged('本轮绑定或脚本已修改，自动恢复不会切换到另一份录制。')
    state.update(recording=recording, reference=reference, recording_file=filename,
                 recording_sha256=recording['sha256'], parameters=parameters,
                 failure_retries=0, battle_failed=False)
    policy = parameters.get('failure_retry', {})
    afk = parameters.get('afk_mode', False)
    if type(afk) is not bool:
        raise ValueError('挂机模式必须为布尔值。')
    if afk:
        policy = dict(enabled=True, max_retries=-1)
    if not isinstance(policy, dict) or type(policy.get('enabled', False)) is not bool:
        raise ValueError('失败自动重试配置无效。')
    retries = policy.get('max_retries', 3)
    if isinstance(retries, str) and retries.isascii() and retries.isdecimal():
        retries = int(retries)
    if type(retries) is not int or not (0 <= retries <= 20 or afk and retries == -1):
        raise ValueError('每轮失败重试次数必须为0～20；0表示不自动重开。')
    state['failure_retry'] = dict(enabled=afk or policy.get('enabled', False) and retries > 0, max_retries=retries)
    routes = parameters.get('routes', {})
    mode = parameters.get('exit_mode', next(iter(routes), None))
    if routes and mode not in routes:
        raise ValueError('请选择该任务支持的脚本完成方式。')
    frame = capture(context)
    height, width = frame.shape[:2]
    if min(height, width) != 720 or any(abs(a-b) > 2 for a,b in zip(recording['base'], (width,height))):
        raise ValueError('脚本与当前720截图尺寸不一致，请使用相同游戏布局重新录制。')
    playback = dict(file=recording['path'], expected_sha256=recording['sha256'],
                    strict_task=True, expected_short_side=720, expected_base=recording['base'], speed=1)
    if not isinstance(playback_node, str) or not playback_node:
        raise ValueError('任务缺少脚本回放节点配置。')
    patch = {playback_node: {'action': {'param': {'custom_action_param': playback}}}}
    guard = parameters.get('result_guard')
    if guard is not None:
        playback['result_guard'] = deepcopy(guard)
        default_next = ([parameters['route_node']] if parameters.get('route_node') else
                        context.get_node_data(playback_node).get('next', []))
        playback['result_guard']['default_next'] = deepcopy(default_next)
        patch[playback_node]['next'] = deepcopy(default_next)
    if routes:
        route_node = parameters.get('route_node')
        targets = routes[mode]
        if not isinstance(route_node, str) or not isinstance(targets, list) or not all(isinstance(n, str) for n in targets):
            raise ValueError('任务完成路线配置无效。')
        patch[route_node] = {'next': targets}
    _override(context, patch)
    write_status(state, 'running', 'round_started', file=Path(recording['path']).name, exit_mode=mode)


def _navigator(context, state, *, targets=None):
    graph = task_graph(context, state['parameters'], targets=targets)
    return GraphNavigator(context, graph, capture=lambda: capture(context),
        check=lambda: _check(context), log=lambda message: print('[navigation] ' + message, flush=True))


def navigate_round(context, state):
    _check(context)
    current = inspect_recording(state['reference'])
    if current['path'] != state['recording']['path'] or current['sha256'] != state['recording']['sha256']:
        raise RecordingChanged('本轮重试期间录制已改变，未开始下一次回放。')
    navigator = _navigator(context, state)
    try:
        return navigator.run()
    except NavigationError as error:
        parameters = state['parameters']
        navigation = parameters.get('navigation', {})
        # A map can keep its dragged position between ordinary rounds too.
        # Viewport restoration is navigation, independent of process restart.
        if error.reason == 'unreachable' and navigation.get('kind') == 'stage':
            target = navigation.get('target_node')
            if wait_stage_ready(context,navigator,target):
                return navigator.run()
            if restore_stage_view(context,navigator,target):
                return navigator.run()
        raise


def return_targets(state):
    allowed = ['task_map', 'map_a', 'map_b']
    if state['parameters'].get('navigation', {}).get('kind') == 'stage':
        allowed.append('task_destination')
    return allowed


def confirm_return(context, state):
    allowed = return_targets(state)
    actual = _navigator(context, state, targets=allowed).locate()
    if actual not in allowed:
        raise ValueError('尚未确认返回地图，本轮不计次也不重试。')


def retry_failed_round(context, state, node_name):
    _check(context)
    if not state.get('battle_failed'):
        raise ValueError('没有已确认的挑战失败，不执行失败重试。')
    confirm_return(context, state)
    policy = state['failure_retry']
    if not policy['enabled']:
        write_status(state, 'fatal', 'battle_failed', message='挑战失败，已返回地图；失败自动重试已关闭，本轮未计次。')
        return False
    if policy['max_retries'] != -1 and state['failure_retries'] >= policy['max_retries']:
        write_status(state, 'fatal', 'battle_retry_exhausted', message='挑战失败重试次数已用尽，本轮未计次。')
        return False
    next_node = state['parameters'].get('navigation_entry')
    if not isinstance(next_node, str) or not next_node or not context.override_next(node_name, [next_node]):
        raise ValueError('缺少本轮导航重试入口。')
    state['failure_retries'] += 1
    state['battle_failed'] = False
    write_status(state, 'running', 'battle_retry',
        message=f"失败轮次未计次，开始第{state['failure_retries']}次重试。",
        failure_retries=state['failure_retries'])
    return True


def recover_failed_return(context, state):
    """Retry only the recognized defeat-to-map edge, never general navigation."""
    # The original Pipeline already attempted one click; allow two more.
    remaining = 2 - state.get('return_recovery_attempts', 0)
    if remaining <= 0:
        return False
    navigator = _navigator(context, state, targets=return_targets(state))
    # Retain all scene/overlay probes but expose only the guarded Return Map
    # action. A world map, modal, revival prompt or unknown frame has no route.
    navigator.graph['edges'] = [edge for edge in navigator.graph['edges']
        if edge['source'] == 'challenge_failed' and edge['entry'] == 'Result_FailedReturn'
        and edge['target'] in navigator.graph['targets']]
    for edge in navigator.graph['edges']:
        edge.update(max_attempts=remaining, timeout_ms=1500)
    navigator.graph.update(max_steps=remaining, max_seconds=15, settle_ms=1000)
    checked_step = -1
    def check():
        nonlocal checked_step
        _check(context)
        # GraphNavigator increments steps before dispatch, so verify the pinned
        # recording initially and before every additional input attempt.
        if checked_step != navigator.steps:
            current = inspect_recording(state['reference'])
            if current['path'] != state['recording']['path'] or current['sha256'] != state['recording']['sha256']:
                raise RecordingChanged('返回地图重试期间录制已改变，已停止。')
            checked_step = navigator.steps
    navigator.check = check
    write_status(state, 'running', 'battle_return_retrying',
                 message=f'返回地图确认超时，开始受保护的返回重试，最多追加{remaining}次。')
    try:
        navigator.run()
        return True
    except (NavigationError, TimeoutError) as error:
        print('[script_task] 返回地图恢复未完成：' + str(error), flush=True)
        return False
    finally:
        state['return_recovery_attempts'] = state.get('return_recovery_attempts', 0) + navigator.steps


def managed_failure_restart(state):
    management = state.get('parameters', {}).get('game_management')
    return isinstance(management, dict) and management.get('restart_on_failure') is True


def exhausted_battle_reason(state):
    """Only terminal battle failure may hand the current round to process recovery."""
    policy = state['failure_retry']
    if not policy['enabled']:
        return 'battle_failed'
    if policy['max_retries'] != -1 and state['failure_retries'] >= policy['max_retries']:
        return 'battle_retry_exhausted'
    return None


def begin_recovery(context, parameters, state):
    _check(context)
    slot = parameters.get('save_slot', 1)
    slots = parameters.get('resume_slots', {})
    if type(slot) is not int or str(slot) not in slots:
        raise ValueError('请选择该任务支持的已有存档。')
    state['save_slot'] = slot
    node = parameters.get('resume_choice_node')
    guards = parameters.get('resume_guards', [])
    if not isinstance(node, str) or not node or not isinstance(guards, list):
        raise ValueError('任务恢复选项配置无效。')
    _override(context, {node: {'next': [*guards, slots[str(slot)]]}})
    write_status(state, 'running', 'recovery_login', save_slot=slot)


def fail_round(context, state, reason, node_name=None):
    _check(context)
    recording = state.get('recording')
    if recording is not None:
        current = inspect_recording(state['reference'])
        if current['path'] != recording['path'] or current['sha256'] != recording['sha256']:
            write_status(state, 'fatal', 'recording_changed')
            return False
    if (node_name and state.get('battle_failed') and state.get('parameters', {}).get('navigation')
            and recover_failed_return(context, state)):
        # Reuse the existing map/retry checkpoint. It alone consumes a battle
        # retry and resumes this round's navigation, never the brush counter.
        return retry_failed_round(context, state, node_name)
    if confirm_white_screen(context):
        write_status(state, 'recoverable', 'white_screen')
    elif state.get('battle_failed') and managed_failure_restart(state):
        write_status(state, 'recoverable', 'battle_return_failed',
                     message='已确认战败，但返回流程已失败；交由游戏管理重启后继续本轮，本轮未计次。')
    elif managed_failure_restart(state):
        # A normal task may exhaust its own navigation/return fallbacks without
        # a battle defeat. Confirm a known, unblocked scene before restarting.
        _navigator(context, state).locate()
        write_status(state, 'recoverable', 'task_failed',
                     message='任务流程已宣告失败，当前场景已核验；重启后继续本轮，本轮未计次。')
    else:
        write_status(state, 'fatal', reason or 'unrecognized_screen')
    return False


@AgentServer.custom_action('script_task_state')
class ScriptTaskState(CustomAction):
    def run(self, context, argv):
        state, key = None, None
        terminal = False
        try:
            parameters = _parameters(argv)
            key = _key(argv)
            phase = parameters.get('phase')
            if phase in ('begin', 'recovery_begin'):
                state = _new_state(parameters)
                with _lock:
                    _rounds[key] = state
                if phase == 'begin':
                    begin_round(context, parameters, state)
                else:
                    begin_recovery(context, parameters, state)
                return True
            with _lock:
                state = _rounds.get(key)
            if state is None:
                raise ValueError('缺少本轮初始化状态。')
            _check(context)
            if phase == 'navigate':
                navigate_round(context, state)
                return True
            if phase == 'battle_failure':
                if not state.get('battle_failed'):
                    state['return_recovery_attempts'] = 0
                state['battle_failed'] = True
                reason = exhausted_battle_reason(state)
                if reason and managed_failure_restart(state):
                    write_status(state, 'recoverable', reason,
                                 message='挑战失败且游戏内重试已关闭或次数已用尽；重启后继续本轮，本轮未计次。')
                    terminal = True
                    return False
                write_status(state, 'running', 'battle_failed_returning', message='识别到挑战失败，已停止录制；本次失败不计入次数。')
                return True
            if phase == 'retry_after_failure':
                retry = retry_failed_round(context, state, argv.node_name)
                terminal = not retry
                return retry
            terminal = True
            if phase in ('success', 'ready'):
                if phase == 'success' and state.get('battle_failed'):
                    raise ValueError('挑战失败未结束，不得计为成功。')
                if phase == 'success' and state['parameters'].get('navigation') is not None:
                    confirm_return(context, state)
                write_status(state, phase)
                return True
            if phase == 'fatal':
                write_status(state, 'fatal', str(parameters.get('reason') or 'blocked_dialog'))
                return False
            if phase == 'failure':
                result = fail_round(context, state, str(parameters.get('reason') or 'unrecognized_screen'), argv.node_name)
                terminal = not result
                return result
            raise ValueError('未知脚本任务检查点。')
        except Cancelled:
            terminal = True
            if state is not None:
                _stop_status(state, 'cancelled', 'user_cancelled')
            return False
        except (CaptureUnavailable, RecordingChanged) as error:
            terminal = True
            reason = 'capture_unavailable' if isinstance(error, CaptureUnavailable) else 'recording_changed'
            if state is not None:
                _stop_status(state, 'fatal', reason, message=str(error))
            return False
        except (NavigationError, TimeoutError) as error:
            terminal = True
            if state is not None:
                reason = getattr(error, 'reason', 'timeout')
                if phase == 'navigate' and managed_failure_restart(state) and reason in ('transition_failed', 'budget', 'unreachable', 'timeout'):
                    _stop_status(state, 'recoverable', 'task_failed', navigation_failure=reason, message=str(error))
                else:
                    _stop_status(state, 'fatal', 'navigation_' + reason, message=str(error))
            return False
        except Exception as error:
            terminal = True
            print('[script_task] 已停止：' + str(error), flush=True)
            if state is not None:
                _stop_status(state, 'fatal', 'configuration_or_recording_error', message=str(error))
            return False
        finally:
            if terminal and key is not None:
                with _lock:
                    _rounds.pop(key, None)
