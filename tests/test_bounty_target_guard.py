"""Only the selected, accepted bounty detail may authorize a stage click.

The first fixture uses OCR text and bounding boxes observed during the
2026-10-27 wrong-bounty incident.  These are recognition-configuration tests;
they do not replace an actual Maa OCR or game-window check.
"""
import json
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
from navigation.catalog import task_routine
PIPELINES = ROOT / 'assets/resource/base/pipeline'
TASKS = ('守卫太宗', '守卫宝烛', '追捕飞贼', '遁地大盗')
DESCRIPTIONS = {
    '守卫太宗': ['太子亡后阴魂不散', '保护太宗'],
    '守卫宝烛': ['香花宝烛被盗', '击败恶鼠'],
    '追捕飞贼': ['飞天大盗重出江湖', '诚邀高手相助'],
    '遁地大盗': ['遁地大盗被困密室', '速去捉拿'],
}

# 720-pixel reference frame: accepted wrong bounty and its own detail panel.
WRONG_BOUNTY_FRAME = (
    ('完成悬赏任务，获得高额奖励', (208, 180, 280, 24)),
    ('守卫宝烛', (222, 223, 98, 29)),
    ('已接受', (431, 230, 39, 21)),
    ('守卫宝烛', (665, 185, 98, 30)),
    ('前往副本', (587, 593, 94, 29)),
)


def frame_for(task):
    canonical = '遁地大盗' if task in ('捉拿大盗','捉拿大盜') else task
    body = tuple((text,(520,240+i*27,len(text)*20,24)) for i,text in enumerate(DESCRIPTIONS.get(canonical,[])))
    return tuple((task if text == '守卫宝烛' else text, box)
                 for text, box in WRONG_BOUNTY_FRAME) + body


def evaluate_config(recognition, observations):
    """Evaluate the pipeline's And/OCR constraints against known OCR boxes."""
    if recognition == 'Result_NoRevivePrompt':
        # These OCR fixtures are clear quest panels. The native modal suite
        # separately supplies positive/negative foreground pixels.
        return (0, 0, 1, 1)
    if recognition['type'] == 'And':
        results = [evaluate_config(item if isinstance(item,str) else item['recognition'], observations)
                   for item in recognition['param']['all_of']]
        if any(result is None for result in results):
            return None
        return results[recognition['param'].get('box_index',0)]
    if recognition['type'] == 'Or':
        return next((result for item in recognition['param']['any_of']
                     if (result := evaluate_config(item['recognition'], observations)) is not None), None)
    if recognition['type'] != 'OCR':
        raise AssertionError('unexpected recognition type')
    params = recognition['param']
    x, y, width, height = params['roi']
    for text, box in observations:
        bx, by, bw, bh = box
        if (x <= bx and y <= by and bx + bw <= x + width
                and by + bh <= y + height
                and any(re.search(pattern, text) for pattern in params['expected'])):
            return box
    return None


class BountyTargetGuardTests(unittest.TestCase):
    def test_main_quest_seen_in_195847_log_cannot_be_a_bounty_destination(self):
        frame=(('完成主线任务，获得高额奖励',(208,180,271,24)),
               ('日月星河',(221,221,101,35)),('已接受',(422,221,63,36)),
               ('日月星河',(665,185,100,30)),('前往副本',(670,590,100,38)))
        for task in TASKS:
            with self.subTest(task=task):
                self.assertIsNone(evaluate_config(self.recognition(task),frame))

    def recognition(self, task):
        nodes={n:v for p in PIPELINES.glob('*.json') for n,v in json.loads(p.read_text(encoding='utf8')).items()}
        class Context:
            def get_node_data(self,name):return nodes[name]
            def override_pipeline(self,patch):nodes.update(patch);return True
        wrapper=json.loads((PIPELINES/('悬赏（'+task+'）.json')).read_text(encoding='utf8'))
        params=next(n['action']['param']['custom_action_param'] for n in wrapper.values()
            if n.get('action',{}).get('param',{}).get('custom_action_param',{}).get('phase')=='begin')
        task_routine(Context(),params)
        node = nodes['Bounty_Target']
        self.assertEqual(node['action'], {'type':'LongPress','param':{'duration':100}})
        def expand(rec):
            for field in ('all_of','any_of'):
                if field in rec.get('param',{}):
                    rec['param'][field]=[item if item=='Result_NoRevivePrompt' else
                        dict(recognition=expand(nodes[item]['recognition'] if isinstance(item,str) else item['recognition']))
                        for item in rec['param'][field]]
            return rec
        return expand(node['recognition'])

    def test_wrong_bounty_detail_is_rejected_even_when_the_list_has_the_right_name(self):
        for task in TASKS:
            with self.subTest(task=task):
                # A matching left-hand list entry does not establish that it is
                # the selected task whose button would be clicked.
                wrong = '守卫太宗' if task == '守卫宝烛' else '守卫宝烛'
                frame = frame_for(wrong) + ((task, (222, 270, 98, 29)),)
                self.assertIsNone(evaluate_config(self.recognition(task), frame))

    def test_correct_title_acceptance_and_button_return_button_box(self):
        for task in TASKS:
            with self.subTest(task=task):
                frame = frame_for(task)
                self.assertEqual(evaluate_config(self.recognition(task), frame),
                                 (587, 593, 94, 29))

    def test_acceptance_button_title_and_selected_bounty_tab_are_all_required(self):
        for task in TASKS:
            frame = frame_for(task)
            for omitted in ('已接受', '前往副本', DESCRIPTIONS[task][0], '完成悬赏任务，获得高额奖励'):
                with self.subTest(task=task, omitted=omitted):
                    self.assertIsNone(evaluate_config(self.recognition(task),
                                      tuple(item for item in frame if item[0] != omitted)))

    def test_unapproved_title_suffix_is_rejected(self):
        for task in TASKS:
            with self.subTest(task=task):
                frame = frame_for(task + '奖励')
                self.assertIsNone(evaluate_config(self.recognition(task), frame))

    def test_cached_ocr_fallback_recognizes_only_the_observed_status_variant(self):
        for task in TASKS:
            with self.subTest(task=task):
                checks = self.recognition(task)['param']['all_of']
                status = checks[1]['recognition']
                self.assertEqual(status['type'], 'Or')
                original, fallback = [item['recognition'] for item in status['param']['any_of']]
                self.assertEqual(original['param']['expected'], ['^已接受$'])
                self.assertTrue(fallback['param']['only_rec'])
                self.assertEqual(fallback['param']['roi'], [420, 220, 66, 40])
                frame = tuple(('已搜受' if text == '已接受' else text, box)
                              for text, box in frame_for(task))
                self.assertEqual(evaluate_config(self.recognition(task), frame), (587, 593, 94, 29))
                unrelated = tuple(('未接受' if text == '已搜受' else text, box) for text, box in frame)
                self.assertIsNone(evaluate_config(self.recognition(task), unrelated))

    def test_body_is_required_but_title_is_not_used_as_detail_identity(self):
        for task in TASKS:
            frame=frame_for(task)
            no_title=tuple(item for item in frame if item[1][0]!=665)
            self.assertEqual(evaluate_config(self.recognition(task),no_title),(587,593,94,29))
            for phrase in DESCRIPTIONS[task]:
                self.assertIsNone(evaluate_config(self.recognition(task),tuple(item for item in frame if item[0]!=phrase)))
            moved=tuple((text,(220,300,box[2],box[3])) if text in DESCRIPTIONS[task] else (text,box) for text,box in frame)
            self.assertIsNone(evaluate_config(self.recognition(task),moved))

    def test_observed_traditional_ocr_variant_in_description(self):
        for task in ('追捕飞贼','遁地大盗'):
            frame=tuple((text.replace('盗','盜'),box) for text,box in frame_for(task))
            self.assertEqual(evaluate_config(self.recognition(task),frame),(587,593,94,29))

    def test_known_renamed_bounty_belongs_only_to_its_own_profile(self):
        for name in ('捉拿大盗','捉拿大盜'):
            self.assertEqual(evaluate_config(self.recognition('遁地大盗'),frame_for(name)),(587,593,94,29))
            self.assertIsNone(evaluate_config(self.recognition('守卫太宗'),frame_for(name)))
            self.assertIsNone(evaluate_config(self.recognition('遁地大盗'),frame_for(name+'奖励')))


if __name__ == '__main__':
    unittest.main()
