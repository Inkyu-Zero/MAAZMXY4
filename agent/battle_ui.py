"""Share one modal decision for identical pixels across native guard callers."""
import json
from navigation.recognition import FrameRecognition, clear_cache
from maa.custom_recognition import CustomRecognition



class NoRevivePrompt(CustomRecognition):
    def __init__(self):
        super().__init__()
        clear_cache()

    def analyze(self, context, argv):
        # Named And children do not inherit a node's inverse flag. Keep this
        # small adapter for negation; native Maa still performs the matching.
        mode = getattr(argv, 'custom_recognition_param', '')
        try:
            observation = FrameRecognition(context, argv.image)
            if isinstance(mode, str) and mode.startswith('"'):
                mode = json.loads(mode)
            if mode == 'present':
                return (CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={'modal': True})
                        if observation.probe('Nav_ModalPresent').hit else None)
            if mode == 'revive':
                if not observation.probe('Nav_ModalPresent').hit:
                    return None
                result = observation.probe('Result_ReviveEvidence')
                return (CustomRecognition.AnalyzeResult(box=result.box, detail={'revival_prompt': True})
                        if result.hit else None)
            if mode == 'stage':
                if observation.any_of('Nav_StageOverlay', ('Nav_Probe_hub_world_entry', 'Nav_ModalPresent')):
                    return None  # Town and battle share the bottom settings icon.
            elif mode in ('map', 'hub'):
                if observation.probe('Nav_MapOverlay' if mode == 'map' else 'Nav_HubOverlay').hit:
                    return None
            elif observation.probe('Nav_ModalPresent').hit:
                return None
            return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={'clear': True})
        except Exception as error:
            print('浮窗检查失败，禁止操作底层界面：' + str(error), flush=True)
            return None
