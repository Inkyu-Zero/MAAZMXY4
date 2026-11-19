"""Ordinary South/East Province entry must not depend on a nonexistent toggle."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'tests/fixtures/navigation/plain-map'


class PlainMapTests(unittest.TestCase):
    def test_mode_requirement_is_scoped_to_provinces_with_the_switch(self):
        nodes=json.loads((ROOT/'assets/resource/base/pipeline/mainline.json').read_text(encoding='utf8'))
        stages=json.loads((ROOT/'agent/data/navigation/mainline-stages.json').read_text(encoding='utf8'))['stages']
        for stage in stages:
            checks=nodes[f'Mainline_{stage["id"]:03d}_Target']['recognition']['param']['all_of']
            self.assertEqual('GameManagement_NormalMap' in checks,stage['town'] not in ('town1','town2'))

    def test_fixture_provenance_and_privacy_masks(self):
        for case in json.loads((FIXTURES/'cases.json').read_text(encoding='utf8'))['cases']:
            self.assertEqual(len(case['source_sha256']),64)
            self.assertTrue(case['redactions'])
            self.assertEqual(hashlib.sha256((FIXTURES/case['file']).read_bytes()).hexdigest(),case['sha256'])

    @unittest.skipUnless(os.environ.get('NAVIGATION_NATIVE_TESTS')=='1','opt-in memory-only recognition')
    def test_real_map_without_toggle_still_identifies_requested_entry(self):
        sys.path.insert(0,str(ROOT/'tests'))
        from test_revival_anchor import RevivalAnchorNativeTests
        from fixture_pixels import read_png_bgr
        cases=json.loads((FIXTURES/'cases.json').read_text(encoding='utf8'))['cases']
        def inspect(context,controller,events):
            for case in cases:
                frame=read_png_bgr(FIXTURES/case['file'])
                self.assertFalse(context.run_recognition('GameManagement_NormalMap',frame).hit)
                self.assertTrue(context.run_recognition(case['entry'],frame).hit,case['file'])
            self.assertEqual(events,[])
        RevivalAnchorNativeTests().run_memory_case(inspect,read_png_bgr(FIXTURES/cases[0]['file']))


if __name__=='__main__':unittest.main()
