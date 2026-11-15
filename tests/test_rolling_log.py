from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from rolling_log import append_log, archive_log


class RollingLogTests(unittest.TestCase):
    def test_rolls_old_blocks_and_preserves_utf8_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.log'
            rows = [f'日志 {i}\n' for i in range(20)]
            for row in rows: append_log(path, row, block_bytes=40)
            archives = sorted(path.parent.glob('run.bak.*.log'))
            self.assertTrue(archives)
            self.assertTrue(all(p.stat().st_size <= 40 for p in archives))
            self.assertEqual(''.join(p.read_text(encoding='utf8') for p in [*archives, path]), ''.join(rows))

    def test_locked_rotation_keeps_appending_without_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.log'; path.write_text('previous\n', encoding='utf8')
            with patch.object(Path, 'rename', side_effect=PermissionError('writer still holds block')):
                append_log(path, 'new\n', block_bytes=8)
            self.assertEqual(path.read_text(encoding='utf8'), 'previous\nnew\n')

    def test_oversize_single_record_stays_intact_then_rolls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.log'
            append_log(path, 'x' * 100, block_bytes=20)
            append_log(path, 'next', block_bytes=20)
            self.assertEqual(path.read_text(), 'next')
            self.assertEqual(next(path.parent.glob('*.bak.*.log')).read_text(), 'x' * 100)

    def test_non_log_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError): append_log(Path(directory) / 'recording.json', 'data')

    def test_round_archives_keep_previous_contents_and_distinct_names(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.log'
            append_log(path, 'first round\n'); first = archive_log(path)
            append_log(path, 'second round\n'); second = archive_log(path)
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(), 'first round\n')
            self.assertEqual(second.read_text(), 'second round\n')
            self.assertFalse(path.exists())
            self.assertIsNone(archive_log(path))


if __name__ == '__main__': unittest.main()
