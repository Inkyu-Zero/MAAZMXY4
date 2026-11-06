"""Geometry-only policy and ownership tests; no real window is touched."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from window_geometry import WindowLease, STANDARD_BASE, SIZING_STYLE, validate_base


class Geometry:
    def __init__(self):
        self.info = dict(hwnd=42, pid=17, class_name='Game', width=1200, height=750, x=100, y=50)
        self.bits = SIZING_STYLE | 0x800000
        self.claimed = False
        self.living = True
        self.fail_resize = False
        self.changes = []

    def inspect(self, hwnd):
        assert hwnd == 42
        return dict(self.info)

    def claim(self, hwnd, pid):
        if self.claimed:
            raise RuntimeError('busy')
        self.claimed = True
        return object()

    def release(self, claim):
        self.claimed = False

    def alive(self, claim):
        return self.living

    def style(self, hwnd):
        return self.bits

    def set_style(self, hwnd, bits):
        self.changes.append(('style', bits))
        self.bits = bits

    def resize(self, hwnd, base):
        self.changes.append(('size', tuple(base)))
        if not self.fail_resize:
            self.info.update(width=base[0], height=base[1])


class WindowGeometryTests(unittest.TestCase):
    def test_standard_recording_and_old_script_dimensions(self):
        for base in (STANDARD_BASE, (1140,720), (1156,720)):
            api = Geometry()
            with WindowLease(42, base, api=api) as lease:
                self.assertEqual(tuple(lease.ensure()[k] for k in ('width','height')), base)
                self.assertFalse(api.bits & SIZING_STYLE)
                self.assertEqual((api.info['x'],api.info['y']), (100,50))
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    WindowLease(42, base, api=api)
                self.assertTrue(api.claimed)
            self.assertEqual(api.bits, SIZING_STYLE | 0x800000)
            self.assertFalse(api.claimed)

    def test_correct_external_resize_and_preserve_unowned_style_bits(self):
        api=Geometry()
        with WindowLease(42, api=api) as lease:
            paused=lease.paused_seconds
            api.info['width']=999
            lease.ensure()
            self.assertGreater(lease.paused_seconds, paused)
            self.assertEqual(api.info['width'],1148)
            api.bits |= 0x200000
        self.assertEqual(api.bits, SIZING_STYLE | 0xa00000)

    def test_recycled_identity_is_never_restored_or_resized(self):
        for field,value in (('pid',18),('class_name','Other')):
            api=Geometry();lease=WindowLease(42,api=api)
            api.info[field]=value
            before=len(api.changes)
            with self.assertRaises(RuntimeError):lease.ensure()
            lease.close();lease.close()
            self.assertEqual(len(api.changes),before)
            self.assertFalse(api.claimed)

    def test_failed_adjustment_releases_claim_and_style(self):
        api=Geometry();api.fail_resize=True
        with self.assertRaisesRegex(RuntimeError,'无法将'):
            WindowLease(42,api=api)
        self.assertFalse(api.claimed)
        self.assertEqual(api.bits&SIZING_STYLE,SIZING_STYLE)

    def test_invalid_base_has_no_side_effects(self):
        for base in ([1148,737], [True,720], [90000,720], None):
            with self.assertRaises(ValueError):validate_base(base)

    def test_minimized_original_window_still_gets_styles_restored(self):
        api=Geometry();lease=WindowLease(42,api=api)
        api.identity=lambda hwnd:dict(pid=17,class_name='Game')
        api.inspect=lambda hwnd:(_ for _ in ()).throw(RuntimeError('minimized'))
        with self.assertRaises(RuntimeError):lease.ensure()
        lease.close()
        self.assertEqual(api.bits&SIZING_STYLE,SIZING_STYLE)
        self.assertFalse(api.claimed)


if __name__=='__main__':unittest.main()
