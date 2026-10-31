"""Validate full-client packaging and privacy without installing or launching it."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tools'))
from build_release import collect
import build_release
from build_release import client_files, validate_client, validate_client_resources
import setup_runtime
import runtime_check


class ReleasePackageTests(unittest.TestCase):
    def test_pillow_native_extension_accepts_only_supported_312_windows_names(self):
        relative = 'Lib/site-packages/PIL/_imaging.pyd'
        tagged = self.root/'Lib/site-packages/PIL/_imaging.cp312-win_amd64.pyd'
        self.write('Lib/site-packages/PIL/_imaging.cp313-win_amd64.pyd', b'wrong ABI')
        with self.assertRaises(ValueError):
            build_release.require_paths(self.root, [relative], 'Python')
        self.write(tagged.relative_to(self.root).as_posix(), b'fixture extension')
        build_release.require_paths(self.root, [relative], 'Python')
        with patch.object(runtime_check, 'PY_DIR', str(self.root)), patch.object(runtime_check, 'REQUIRED', [relative]):
            self.assertEqual(runtime_check.check_files(), [])
            tagged.unlink()
            self.assertEqual(runtime_check.check_files(), [relative])
        self.write(relative, b'legacy extension')
        build_release.require_paths(self.root, [relative], 'Python')

    @classmethod
    def setUpClass(cls):
        cls.package_temp = tempfile.TemporaryDirectory()
        cls.package = Path(cls.package_temp.name)/'update.zip'
        with zipfile.ZipFile(cls.package,'w') as archive:
            for source,relative in collect(str(ROOT)): archive.write(source,relative)

    @classmethod
    def tearDownClass(cls): cls.package_temp.cleanup()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def write(self,name,raw):
        path=self.root/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw)

    def list_from_installed_worker(self):
        result=subprocess.run([sys.executable,'-E','-s','-u','-X','utf8',str(self.root/'agent/script_studio_service.py'),'--root',str(self.root)],
            input=json.dumps(dict(id=1,method='list'))+'\n',capture_output=True,text=True,encoding='utf8',timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        reply=json.loads(result.stdout.strip())
        self.assertTrue(reply['ok'],reply)
        return reply['result']

    def test_clean_package_worker_loads_only_unbound_example_without_assets(self):
        with zipfile.ZipFile(self.package) as archive: archive.extractall(self.root)
        self.assertFalse((self.root/'assets').exists())
        listing=self.list_from_installed_worker()
        self.assertEqual([row['file'] for row in listing['items']], ['示例-移动与攻击.json'])
        self.assertTrue(listing['items'][0]['is_default'])
        self.assertEqual(listing['task_bindings'], {})
        for task in listing['tasks']:
            for parameters in task['playback_parameters'].values():
                if str(parameters.get('file','')).startswith('@task:'):
                    self.assertEqual(parameters['file'],'@task:'+task['id'])
        help_result=subprocess.run([sys.executable,str(self.root/'agent/task_asset_sync.py'),'--help'],capture_output=True,timeout=20)
        self.assertEqual(help_result.returncode,0,help_result.stderr)

    def test_packaged_tasks_have_all_recorded_return_routes_and_reject_stale_resources(self):
        with zipfile.ZipFile(self.package) as archive: archive.extractall(self.root)
        checked = validate_client_resources(self.root)
        self.assertIn('烛时（钟晷极地）', checked)
        self.assertEqual(len(checked), 10)
        path = self.root/'resource/base/pipeline/烛时（钟晷极地）.json'
        data = json.loads(path.read_text(encoding='utf8'))
        del data['烛时（钟晷极地）-回放战斗']['action']['param']['custom_action_param']['script_binding']
        path.write_text(json.dumps(data),encoding='utf8')
        with self.assertRaisesRegex(ValueError, '缺少录制返回确认'):
            validate_client_resources(self.root)

    def client_fixture(self):
        frontend = self.root/'frontend'
        for name in build_release.FRONTEND_REQUIRED: self.write('frontend/'+name,b'fixture binary')
        manifest=json.loads((ROOT/'frontend/upstream.json').read_text(encoding='utf8'))
        sources=['frontend/upstream.json','tools/build_frontend.ps1',*manifest['patches']]
        sources += [p.relative_to(ROOT).as_posix() for p in (ROOT/manifest['overlay_directory']).rglob('*') if p.is_file()]
        built={k:manifest[k] for k in ('repository','commit','archive_sha256','patches')}
        built['source_sha256']={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
        built['binary_sha256']={name:hashlib.sha256((frontend/name).read_bytes()).hexdigest()
                               for name in ('MFAAvalonia.dll','libs/MFAAvalonia.Core.dll')}
        self.write('frontend/script-studio-build.json',json.dumps(built).encode('utf8'))
        for name in build_release.PYTHON_REQUIRED:
            if name in ('DLLs', 'Lib/site-packages/MaaAgentBinary'):
                (self.root/'python'/name).mkdir(parents=True, exist_ok=True)
            else:
                self.write('python/'+name,b'fixture interpreter')
        self.write('python/Lib/site-packages/MaaAgentBinary/fixture.dll', b'fixture binary')
        launcher = self.root/build_release.LAUNCHER_NAME
        self.write(build_release.LAUNCHER_NAME, b'fixture launcher; never executed')
        self.launcher_hash = hashlib.sha256(launcher.read_bytes()).hexdigest()
        return frontend, self.root/'python', launcher

    def test_full_client_rejects_old_frontend_even_with_current_resource_version(self):
        frontend,runtime,launcher=self.client_fixture()
        p=frontend/'script-studio-build.json';built=json.loads(p.read_text(encoding='utf8'))
        built['patches']=built['patches'][:17];p.write_text(json.dumps(built),encoding='utf8')
        with patch.object(build_release,'LAUNCHER_SHA256',self.launcher_hash),self.assertRaisesRegex(ValueError,'前端构建与当前源码不一致'):
            client_files(ROOT,frontend,runtime,launcher)

    def test_full_client_rejects_missing_or_stale_build_script_receipt(self):
        frontend,runtime,launcher=self.client_fixture()
        path=frontend/'script-studio-build.json'
        built=json.loads(path.read_text(encoding='utf8'))
        for checksum in (None, '0'*64):
            if checksum is None: built['source_sha256'].pop('tools/build_frontend.ps1',None)
            else: built['source_sha256']['tools/build_frontend.ps1']=checksum
            path.write_text(json.dumps(built),encoding='utf8')
            with patch.object(build_release,'LAUNCHER_SHA256',self.launcher_hash),self.assertRaisesRegex(ValueError,'build_frontend.ps1'):
                client_files(ROOT,frontend,runtime,launcher)

    def test_public_default_manifest_does_not_admit_undeclared_recordings(self):
        self.write('sample/agent/main.py',b'# code')
        self.write('sample/assets/resource/base/public.json',b'{}')
        self.write('sample/assets/interface.json',b'{}')
        self.write('sample/agent/recordings/public.json',b'{}')
        self.write('sample/agent/recordings/private.json',b'private recording')
        self.write('sample/recordings/private.json',b'personal library')
        self.write('sample/agent/recordings/manifest.json',json.dumps(dict(version=1,items=[dict(file='public.json',sha256=hashlib.sha256(b'{}').hexdigest())])).encode('utf8'))
        names=[relative for _,relative in collect(str(self.root/'sample'))]
        self.assertIn('agent/recordings/public.json',names)
        self.assertFalse(any('private.json' in name for name in names))

    def test_full_client_collects_canonical_resources_and_crlf_setup_without_preview_data(self):
        frontend, runtime, launcher = self.client_fixture()
        self.write('frontend/libs/AGENTS.md', b'local frontend instructions')
        self.write('python/Lib/Agents.MD', b'local runtime instructions')
        for name in ['recordings/private.json','config/instances/private.json','logs/test.log','appsettings.json',
                     'resource/base/pipeline/old.json','interface.json']:
            self.write('frontend/'+name,b'private or stale preview data')
        self.write('python/Lib/__pycache__/ignored.pyc',b'cache')
        with patch.object(build_release, 'LAUNCHER_SHA256', self.launcher_hash):
            files = dict((relative,Path(source)) for source,relative in client_files(ROOT,frontend,runtime,launcher))
        self.assertIn('MAA造梦西游4.exe', files)
        self.assertIn('runtimes/python/pythonw.exe', files)
        self.assertEqual(files['interface.json'],ROOT/'assets/interface.json')
        self.assertEqual(files['resource/base/pipeline/烛时（钟晷极地）.json'],ROOT/'assets/resource/base/pipeline/烛时（钟晷极地）.json')
        self.assertFalse(any(name.startswith(('recordings/','config/','logs/')) or '__pycache__' in name for name in files))
        self.assertNotIn('appsettings.json',files)
        self.assertFalse(any(Path(name).name.casefold() == 'agents.md' for name in files))
        batch=files['DependencySetup_依赖库安装_win.bat'].read_bytes()
        self.assertIn(b'\r\n',batch)
        self.assertNotIn(b'\n',batch.replace(b'\r\n',b''))
        self.assertIn(b'VC_ARG=/install',batch)
        self.assertIn(b'DOTNET_ARG=/install',batch)

    def test_full_package_keeps_empty_native_plugin_directory(self):
        frontend, runtime, launcher = self.client_fixture()
        output = self.root/'full.zip'
        with patch.object(build_release, 'LAUNCHER_SHA256', self.launcher_hash):
            build_release.build_client(ROOT, frontend, runtime, output, launcher,
                channel='preview', manifest_urls=['https://example.test/client-version.json'])
        with zipfile.ZipFile(output) as archive:
            name = 'runtimes/win-x64/native/plugins/'
            self.assertIn(name, archive.namelist())
            self.assertTrue(archive.getinfo(name).is_dir())
            archive.extractall(self.root/'extracted')
        self.assertTrue((self.root/'extracted'/name).is_dir())

    def test_resource_package_excludes_nested_local_instructions(self):
        for name in ('agent/AGENTS.md', 'assets/resource/base/Agents.MD'):
            self.write('sample/'+name, b'local instructions')
        self.write('sample/agent/main.py', b'# public code')
        self.write('sample/assets/resource/base/public.json', b'{}')
        self.write('sample/assets/interface.json', b'{}')
        names = [relative for _, relative in collect(str(self.root/'sample'))]
        self.assertIn('agent/main.py', names)
        self.assertIn('resource/base/public.json', names)
        self.assertFalse(any(Path(name).name.casefold() == 'agents.md' for name in names))

    def test_client_rejects_unverified_launcher_and_incomplete_gui_runtime(self):
        frontend, runtime, launcher = self.client_fixture()
        with self.assertRaisesRegex(ValueError, 'SHA-256 不匹配'):
            client_files(ROOT, frontend, runtime, launcher)
        with patch.object(build_release, 'LAUNCHER_SHA256', self.launcher_hash):
            launcher.unlink()
            with self.assertRaisesRegex(ValueError, '缺少官方启动器'):
                client_files(ROOT, frontend, runtime, launcher)
            self.write(build_release.LAUNCHER_NAME, b'fixture launcher; never executed')
            for name in ('pythonw.exe', 'tcl/tcl8.6/init.tcl', 'Lib/tkinter/__init__.py'):
                raw = (runtime/name).read_bytes()
                (runtime/name).unlink()
                with self.assertRaisesRegex(ValueError, name.replace('.', r'\.')):
                    client_files(ROOT, frontend, runtime, launcher)
                (runtime/name).write_bytes(raw)

    def test_extracted_client_validation_checks_startup_files_as_well_as_resources(self):
        frontend, runtime, launcher = self.client_fixture()
        stage = self.root/'assembled'
        with patch.object(build_release, 'LAUNCHER_SHA256', self.launcher_hash):
            files = client_files(ROOT, frontend, runtime, launcher)
            for source, relative in files:
                destination = stage/relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            self.assertEqual(len(validate_client(stage)), 10)
            (stage/'runtimes/python/pythonw.exe').unlink()
            with self.assertRaisesRegex(ValueError, 'pythonw.exe'):
                validate_client(stage)
            (stage/'runtimes/python/pythonw.exe').mkdir()
            with self.assertRaisesRegex(ValueError, 'pythonw.exe'):
                validate_client(stage)

    def test_runtime_rebuild_retains_updater_and_recording_dependencies(self):
        source = self.root/'system-python'
        required = ('Lib/tkinter/__init__.py', 'DLLs/_tkinter.pyd', 'DLLs/tcl86t.dll',
                    'DLLs/tk86t.dll', 'tcl/tcl8.6/init.tcl', 'tcl/tk8.6/tk.tcl',
                    'Lib/site-packages/pynput/__init__.py', 'Lib/site-packages/six.py')
        for name in required: self.write('system-python/'+name, b'tk dependency')
        (source/'Lib/site-packages').mkdir(parents=True, exist_ok=True)
        target = self.root/'rebuilt-python'
        with patch.object(setup_runtime, 'PY_DIR', str(target)), patch('builtins.print'):
            setup_runtime.build(str(source))
        for name in required:
            self.assertEqual((target/name).read_bytes(), b'tk dependency', name)



if __name__=='__main__': unittest.main()
