"""The native resource owns one similarity threshold, including inline probes."""
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def recognizers(value):
    if isinstance(value, dict):
        if value.get('type') == 'TemplateMatch' or value.get('recognition') == 'TemplateMatch':
            yield value.get('param', value)
        for child in value.values():
            yield from recognizers(child)
    elif isinstance(value, list):
        for child in value:
            yield from recognizers(child)


class RecognitionDefaultsTests(unittest.TestCase):
    def test_all_standard_templates_inherit_point_83_and_difference_metrics_remain_calibrated(self):
        resource = ROOT/'assets/resource/base'
        default = json.loads((resource/'default_pipeline.json').read_text(encoding='utf8'))
        self.assertEqual(default['TemplateMatch']['recognition']['param']['threshold'], .83)
        files = list((resource/'pipeline').glob('*.json')) + [ROOT/'tools'/name for name in
                 ('navigation_library.json','script_task_profiles.json','bounty_task_template.json')]
        standard = difference = 0
        for path in files:
            for params in recognizers(json.loads(path.read_text(encoding='utf8'))):
                if params.get('method', 5) == 1:
                    difference += 1
                    self.assertIn(params['threshold'], (.03, .045), path)
                else:
                    standard += 1
                    expected = .80 if params.get('template') == 'navigation/settings/continue.png' else .83
                    self.assertEqual(params.get('threshold', .83), expected, path)
        self.assertGreater(standard, 100)
        self.assertGreater(difference, 0)

    def test_every_task_entry_uses_a_300ms_press_with_release_dwell(self):
        nodes = {}
        for path in (ROOT/'assets/resource/base/pipeline').glob('*.json'):
            nodes.update(json.loads(path.read_text(encoding='utf8')))
        checked = 0
        for node in nodes.values():
            action = node.get('action', {})
            if not isinstance(action, dict):
                continue
            params = action.get('param', {}).get('custom_action_param', {})
            if not isinstance(params, dict) or not params.get('round_flow'):
                continue
            target = nodes[params['navigation']['target_node']]
            self.assertEqual(target['action']['type'], 'LongPress')
            self.assertEqual(target['action']['param']['duration'], 300)
            self.assertEqual(target['pre_delay'], 0)
            self.assertEqual(target['post_delay'], 2000 if params['navigation']['kind']=='bounty' else 1000)
            checked += 1
        self.assertEqual(checked, 11+62)
