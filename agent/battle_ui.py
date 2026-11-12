"""Share one modal decision for identical pixels across native guard callers."""
import json
from navigation.recognition import _probe, modal_present, clear_cache
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
            if isinstance(mode, str) and mode.startswith('"'):
                mode = json.loads(mode)
            if mode == 'present':
                return (CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={'modal': True})
                        if modal_present(context, argv.image) else None)
            if mode == 'revive':
                if not modal_present(context, argv.image):
                    return None
                result = _probe(context, argv.image, 'Result_ReviveEvidence')
                return (CustomRecognition.AnalyzeResult(box=result.box, detail={'revival_prompt': True})
                        if result.hit else None)
            if mode == 'stage' and _probe(context, argv.image, 'Nav_Probe_hub_world_entry').hit:
                return None  # Town and battle share the bottom settings icon.
            if mode in ('map', 'hub'):
                if _probe(context, argv.image, 'Nav_MapOverlay' if mode == 'map' else 'Nav_HubOverlay').hit:
                    return None
            elif modal_present(context, argv.image):
                return None
            return CustomRecognition.AnalyzeResult(box=(0, 0, 1, 1), detail={'clear': True})
        except Exception as error:
            print('浮窗检查失败，禁止操作底层界面：' + str(error), flush=True)
            return None
