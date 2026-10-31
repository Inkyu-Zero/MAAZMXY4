"""Verify that normal bounty routes cannot click another bounty's button.

The first fixture uses OCR text and bounding boxes observed during the
2026-10-27 wrong-bounty incident.  These are recognition-configuration tests;
they do not replace an actual Maa OCR or game-window check.
"""
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
PIPELINES = ROOT / 'assets/resource/base/pipeline'
TASKS = ('守卫太宗', '守卫宝烛', '追捕飞贼', '遁地大盗')

# 720-pixel reference frame: accepted wrong bounty and its own detail panel.
WRONG_BOUNTY_FRAME = (
    ('守卫宝烛', (222, 223, 98, 29)),
    ('已接受', (431, 230, 39, 21)),
    ('守卫宝烛', (665, 185, 98, 30)),
    ('前往副本', (587, 593, 94, 29)),
)


def frame_for(task):
    return tuple((task if text == '守卫宝烛' else text, box)
                 for text, box in WRONG_BOUNTY_FRAME)


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
        return results[recognition['param']['box_index']]
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
    def recognition(self, task):
        pipeline = json.loads((PIPELINES / f'悬赏（{task}）.json').read_text(encoding='utf8'))
        node = pipeline[f'悬赏（{task}）-点击前往副本']
        self.assertEqual(node['action']['type'], 'Click')
        return node['recognition']

    def test_wrong_bounty_is_rejected_even_when_target_is_in_left_list(self):
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

    def test_missing_title_acceptance_or_button_fails_closed(self):
        for task in TASKS:
            frame = frame_for(task)
            for omitted in ('已接受', task, '前往副本'):
                with self.subTest(task=task, omitted=omitted):
                    self.assertIsNone(evaluate_config(self.recognition(task),
                                      tuple(item for item in frame if item[0] != omitted)))

    def test_partial_and_unrelated_labels_do_not_match(self):
        for task in TASKS:
            with self.subTest(task=task):
                frame = frame_for(task + '奖励')
                self.assertIsNone(evaluate_config(self.recognition(task), frame))

    def test_cached_ocr_fallback_recognizes_only_the_observed_status_variant(self):
        for task in TASKS:
            with self.subTest(task=task):
                checks = self.recognition(task)['param']['all_of']
                status = checks[2]['recognition']
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

    def test_ocr_traditional_variant_of_thief_name_matches(self):
        frame = tuple(('遁地大盜' if text == '守卫宝烛' and box[0] == 665 else text, box)
                      for text, box in WRONG_BOUNTY_FRAME)
        self.assertEqual(evaluate_config(self.recognition('遁地大盗'), frame),
                         (587, 593, 94, 29))

    def test_renamed_thief_still_requires_exact_name_and_acceptance(self):
        for name in ('捉拿大盗','捉拿大盜'):
            self.assertEqual(evaluate_config(self.recognition('遁地大盗'),frame_for(name)),(587,593,94,29))
            self.assertIsNone(evaluate_config(self.recognition('守卫太宗'),frame_for(name)))
            self.assertIsNone(evaluate_config(self.recognition('遁地大盗'),frame_for(name+'奖励')))


if __name__ == '__main__':
    unittest.main()
