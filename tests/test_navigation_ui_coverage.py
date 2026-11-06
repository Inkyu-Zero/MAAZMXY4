"""Public asset provenance and foreground guards across existing task routes."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent'))


def read(path):
    return json.loads((ROOT / path).read_text(encoding='utf8'))


class NavigationUICoverageTests(unittest.TestCase):
    def test_small_window_menu_regression_fixture_has_provenance(self):
        base=ROOT/'tests/fixtures/navigation/regressions/settings-small-window'
        data=json.loads(base.with_suffix('.json').read_text(encoding='utf8'))
        self.assertEqual(hashlib.sha256(base.with_suffix('.png').read_bytes()).hexdigest(),data['sha256'])
        self.assertEqual(len(data['source_sha256']),64)
        self.assertEqual(data['retained_roi'],[400,110,370,535])

    def test_new_sources_and_fixtures_have_exact_provenance(self):
        for name in ('common-ui', 'settings-ui'):
            data = read('agent/data/navigation/' + name + '.json')
            self.assertTrue(data['source']['url'].startswith('https://sda.4399.com/'))
            self.assertEqual(len(data['source']['sha256']), 64)
            for asset in data['assets']:
                path = ROOT / 'assets/resource/base/image' / asset['path']
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), asset['sha256'])
                self.assertEqual(len(asset['source_frame_sha256']), 64)
        for folder, count in [('common-dialog', 6), ('settings', 3), ('result-roles', 42)]:
            base = ROOT / 'tests/fixtures/navigation' / folder
            data = json.loads((base / 'cases.json').read_text(encoding='utf8'))
            self.assertEqual(len(data['cases']), count)
            self.assertEqual({p.name for p in base.glob('*.png')}, {c['file'] for c in data['cases']})
            for case in data['cases']:
                self.assertEqual(hashlib.sha256((base / case['file']).read_bytes()).hexdigest(), case['sha256'])

    def test_common_confirm_is_readonly_and_uses_three_opaque_anchored_patches(self):
        node = read('assets/resource/base/pipeline/navigation_common_ui.json')['Nav_CommonConfirm']
        self.assertEqual(node['action'], {'type': 'DoNothing'})
        self.assertFalse(node['next'])
        parts = node['recognition']['param']['all_of']
        self.assertEqual(len(parts), 3)
        for part in parts:
            self.assertEqual(part['recognition']['type'], 'TemplateMatch')
            self.assertFalse(part['recognition']['param'].get('green_mask'))
        for part in parts[1:]:
            self.assertEqual(part['recognition']['param']['roi'], parts[0]['sub_name'])

    def test_task_entry_settings_and_returns_keep_foreground_guards(self):
        stage_count = settings_count = 0
        clear_map = read('assets/resource/base/pipeline/navigation.json')['Nav_ClearMap']
        self.assertIn('Result_NoRevivePrompt', clear_map['recognition']['param']['all_of'])
        self.assertIn('Nav_NoMapOverlay', clear_map['recognition']['param']['all_of'])
        for path in (ROOT / 'assets/resource/base/pipeline').glob('*.json'):
            for name, node in json.loads(path.read_text(encoding='utf8')).items():
                if name.endswith('-关卡入口'):
                    stage_count += 1
                    self.assertEqual(node['recognition']['param']['all_of'][0], 'Nav_ClearMap')
                    self.assertEqual(node['recognition']['param']['box_index'], 1)
                if name.endswith('-点击设置') or name=='Bounty_OpenSettings':
                    settings_count += 1
                    self.assertIn('Result_NoRevivePrompt', node['recognition']['param']['all_of'])
                    self.assertEqual(node.get('pre_delay', 0), 0)
                if name.endswith('-返回地图') or name=='Bounty_ReturnSettings':
                    self.assertEqual(node['recognition'], dict(type='And', param=dict(all_of=['Nav_SettingsReturn'])))
        self.assertEqual(stage_count, 7)
        self.assertEqual(settings_count, 1)

    def test_shared_navigation_clicks_are_guarded_except_known_sell_cancel(self):
        nodes = read('assets/resource/base/pipeline/navigation.json')
        exemptions = {'Nav_Go_profile_sell_profile'}
        for name, node in nodes.items():
            if node['action']['type'] == 'Click' and name not in exemptions:
                parts=node['recognition']['param']['all_of']
                self.assertTrue('Result_NoRevivePrompt' in parts or 'Nav_ClearMap' in parts,name)

    def test_all_profiles_use_the_same_guarded_return_actions(self):
        from task_catalog import task_catalog
        profiles=[t['management_profile'] for t in task_catalog(ROOT,bindings={}) if t['management_profile']]
        self.assertEqual(len(profiles),11)
        for profile in profiles:
            self.assertIn('round_flow',profile['parameters'])
        nodes=read('assets/resource/base/pipeline/navigation_settings.json')
        self.assertIn('Result_NoRevivePrompt',nodes['Nav_SettingsReturn']['recognition']['param']['all_of'])


if __name__ == '__main__':
    unittest.main()
