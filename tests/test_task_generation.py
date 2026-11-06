"""Public task resources must be reproducible from their declarative inputs."""
import importlib.util
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent'))


class TaskGenerationTests(unittest.TestCase):
    def test_all_shared_round_resources_match_a_clean_generation(self):
        spec = importlib.util.spec_from_file_location('task_generator', ROOT / 'tools/build_script_tasks.py')
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        with tempfile.TemporaryDirectory(prefix='task-generation-') as temporary:
            output = Path(temporary)
            pipeline = output / 'assets/resource/base/pipeline'
            pipeline.mkdir(parents=True)
            for relative in ('assets/interface.json', 'tools/script_task_profiles.json',
                             'tools/bounty_task_template.json'):
                destination = output / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)
            generator.build(output)
            generated = {p.name: p.read_bytes() for p in pipeline.glob('*.json')}
            self.assertEqual(len(generated), 12)
            for name, raw in generated.items():
                with self.subTest(file=name):
                    self.assertEqual(raw, (ROOT / 'assets/resource/base/pipeline' / name).read_bytes())
            generator.build(output)
            self.assertEqual({p.name: p.read_bytes() for p in pipeline.glob('*.json')}, generated)


if __name__ == '__main__': unittest.main()
