"""Geometry-only policy and ownership tests; no real window is touched."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from window_geometry import WindowLease, STANDARD_BASE, SIZING_STYLE, validate_base, prepare_window


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
    def test_playback_prepares_script_aspect_and_owns_only_sizing_bits(self):
        for base in (STANDARD_BASE, (1140,720), (1156,720)):
            api = Geometry()
            api.info.update(width=2000,height=720)
            with WindowLease(42, base, api=api) as lease:
                self.assertEqual(tuple(lease.ensure()[k] for k in ('width','height')), base)
                self.assertFalse(api.bits & SIZING_STYLE)
                self.assertEqual((api.info['x'],api.info['y']), (100,50))
                with self.assertRaisesRegex(RuntimeError,'busy'):
                    WindowLease(42, base, api=api)
                self.assertTrue(api.claimed)
            self.assertEqual(api.bits, SIZING_STYLE | 0x800000)
            self.assertFalse(api.claimed)

    def test_external_resize_stops_without_adjusting_again(self):
        api=Geometry()
        with WindowLease(42, api=api) as lease:
            before=list(api.changes)
            api.info['width']=999
            with self.assertRaisesRegex(RuntimeError,'播放期间.*尺寸'):
                lease.ensure()
            self.assertEqual(api.changes,before)
            self.assertEqual(api.info['width'],999)
            api.bits |= 0x200000
        self.assertEqual(api.bits, SIZING_STYLE | 0xa00000)
        self.assertEqual(api.info['width'],999)

    def test_compatible_initial_sizes_are_preserved_across_scaling(self):
        for size in ((940,590),(1175,738),(1410,885),(1645,1032),(1880,1180),(2350,1475)):
            with self.subTest(size=size):
                api=Geometry();api.info.update(width=size[0],height=size[1])
                with WindowLease(42,api=api) as lease:
                    self.assertEqual(tuple(lease.ensure()[k] for k in ('width','height')),size)
                self.assertFalse(any(c[0]=='size' for c in api.changes))

    def test_one_pixel_rounding_is_accepted_but_large_mismatch_is_not(self):
        class Rounded(Geometry):
            def resize(self,hwnd,base):
                super().resize(hwnd,base)
                self.info['width']+=self.offset
        for offset in (-1,1,8):
            with self.subTest(offset=offset):
                api=Rounded();api.offset=offset;api.info.update(width=2000,height=720)
                if abs(offset)<=2:
                    with WindowLease(42,(1145,720),api=api) as lease:
                        self.assertEqual(lease.ensure()['width'],1145+offset)
                    self.assertEqual(sum(c[0]=='size' for c in api.changes),1)
                else:
                    with self.assertRaisesRegex(RuntimeError,'无法将'):
                        WindowLease(42,(1145,720),api=api)

    def test_legacy_prepare_handshake_does_not_resize_or_lock(self):
        api=Geometry()
        before=dict(api.info)
        with patch('window_geometry.Win32Geometry',return_value=api):
            self.assertEqual(prepare_window(42),before)
        self.assertFalse(api.changes)
        self.assertFalse(api.claimed)

    def test_script_preparation_corrects_aspect_without_locking_navigation(self):
        api=Geometry();api.info.update(width=1048,height=720)
        with WindowLease(42,api=api,lock_size=False) as lease:
            self.assertTrue(lease.changed)
            self.assertEqual(api.bits&SIZING_STYLE,SIZING_STYLE)
            self.assertEqual(lease.client_size,(1048,657))
        self.assertFalse(api.claimed)
        self.assertEqual(api.changes,[('size',(1048,657))])

    def test_maximized_preparation_uses_restored_client_not_desktop_size(self):
        api=Geometry();api.info.update(width=2560,height=1350)
        def restore(hwnd):
            api.info.update(width=1075,height=675)
            return True
        api.restore=restore
        with WindowLease(42,api=api,lock_size=False) as lease:
            self.assertEqual(lease.client_size,(1075,675))
        self.assertFalse(api.changes)

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
        api=Geometry();api.fail_resize=True;api.info['width']=2000
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
