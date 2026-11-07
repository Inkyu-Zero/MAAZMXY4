"""Private-data migration checks using temporary source/target projects only."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("tested_script_migration",ROOT/"tools/migrate_zaomeng_scripts.py")
migration=importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def source_macro(ident="source-one",name="原录制",key=74):
    return dict(schema="zaomeng.macro",version=1,id=ident,name=name,
        created_at="2026-10-24T09:19:57+08:00",notes="个人备注，需保留",
        window=dict(title="造梦盒子",class_name="ApolloRuntimeContentWindow",width=1175,height=737),
        duration_ms=500,events=[
            dict(t_ms=10,kind="key_down",data=dict(vk=key,scan=36,extended=False)),
            dict(t_ms=100,kind="key_up",data=dict(vk=key,scan=36,extended=False)),
            dict(t_ms=200,kind="mouse_down",data=dict(button="right",x=100,y=200)),
            dict(t_ms=250,kind="mouse_move",data=dict(x=110,y=210)),
            dict(t_ms=300,kind="mouse_up",data=dict(button="right",x=120,y=220)),
            dict(t_ms=400,kind="marker",data=dict(label="阶段"))])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        # Windows TEMP can contain an 8.3 alias (e.g. RUNNER~1). Match the
        # production code's canonical paths so fault injection actually fires.
        self.base=Path(self.temp.name).resolve()
        self.source=self.base/"source"
        self.target=self.base/"target"
        self.scripts=self.source/"data/scripts"
        self.scripts.mkdir(parents=True)
        self.outputs=self.target/"recordings"
        self.manifest=self.target/".local-migrations/zaomeng/scripts.json"
        self.file=self.write_source("one.json",source_macro())
    def tearDown(self):
        self.temp.cleanup()
    def write_source(self,name,data):
        path=self.scripts/name
        path.write_text(json.dumps(data,ensure_ascii=False),encoding="utf8")
        return path
    def run_migration(self,dry_run=False):
        return migration.migrate_scripts(self.source,self.target,dry_run=dry_run)
    def read_manifest(self):
        return json.loads(self.manifest.read_text(encoding="utf8"))

    def test_migration_preserves_source_and_semantics_and_records_hashes(self):
        before=self.file.read_bytes()
        result=self.run_migration()
        self.assertEqual(result["errors"],[])
        self.assertEqual(len(result["recordings"]),1)
        record=result["recordings"][0]
        self.assertEqual(record["status"],"migrated")
        self.assertEqual(record["file"],"原录制.json")
        self.assertEqual(record["source_file"],"data/scripts/one.json")
        self.assertEqual((record["duration_ms"],record["event_count"],record["marker_count"]),(500,5,1))
        self.assertEqual(record["source_sha256"],hashlib.sha256(before).hexdigest())
        target=self.outputs/record["file"]
        self.assertEqual(record["sha256"],hashlib.sha256(target.read_bytes()).hexdigest())
        raw=json.loads(target.read_text(encoding="utf8"))
        self.assertEqual(raw["base"],[1148,720])
        self.assertEqual([a["t"] for a in raw["actions"]],[10,90,100,50,50])
        self.assertEqual(raw["editor"]["markers"][0]["at_ms"],400)
        self.assertEqual(raw["notes"],"个人备注，需保留")
        self.assertEqual(self.file.read_bytes(),before)
        self.assertEqual(self.read_manifest()["recordings"][0]["sha256"],record["sha256"])
        self.assertFalse((self.outputs/".selected.json").exists())

    def test_dry_run_writes_nothing_and_has_the_same_planned_hash(self):
        first=self.run_migration(dry_run=True)
        second=self.run_migration(dry_run=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(first,second)
        self.assertEqual(first["recordings"][0]["status"],"planned")
        actual=self.run_migration()
        self.assertEqual(first["recordings"][0]["sha256"],actual["recordings"][0]["sha256"])

    def test_repeat_is_idempotent_and_does_not_rewrite_recording(self):
        initial=self.run_migration()["recordings"][0]
        target=self.outputs/initial["file"]
        before,target_time=target.read_bytes(),target.stat().st_mtime_ns
        repeat=self.run_migration()
        self.assertEqual(repeat["errors"],[])
        self.assertEqual(repeat["recordings"][0]["status"],"unchanged")
        self.assertEqual(list(self.outputs.glob("*.json")),[target])
        self.assertEqual((target.read_bytes(),target.stat().st_mtime_ns),(before,target_time))
        self.assertEqual(len(self.read_manifest()["recordings"]),1)

    def test_user_modified_target_is_preserved_and_baseline_hash_kept(self):
        initial=self.run_migration()["recordings"][0]
        target=self.outputs/initial["file"]
        target.write_text('{"user":"changed"}',encoding="utf8")
        modified=target.read_bytes()
        result=self.run_migration()
        self.assertEqual(result["errors"],[])
        self.assertEqual(result["recordings"][0]["status"],"target_modified")
        self.assertEqual(result["recordings"][0]["sha256"],hashlib.sha256(modified).hexdigest())
        self.assertTrue(result["warnings"])
        self.assertEqual(target.read_bytes(),modified)
        self.assertEqual(self.read_manifest()["recordings"][0]["sha256"],initial["sha256"])
        active=self.read_manifest()["active_recordings"][0]
        self.assertEqual(active["status"],"target_modified")
        self.assertEqual(active["sha256"],hashlib.sha256(modified).hexdigest())
        self.assertEqual(len(list(self.outputs.glob("*.json"))),1)

    def test_changed_source_creates_a_hash_version_without_overwriting_old(self):
        initial=self.run_migration()["recordings"][0]
        old=self.outputs/initial["file"]
        original=old.read_bytes()
        self.write_source("one.json",source_macro(key=75))
        changed=self.run_migration()
        record=changed["recordings"][0]
        self.assertEqual(changed["errors"],[])
        self.assertEqual(record["file"],f"原录制-{record['source_sha256'][:8]}.json")
        self.assertEqual(record["status"],"migrated")
        self.assertEqual(old.read_bytes(),original)
        self.assertEqual(len(self.read_manifest()["recordings"]),2)
        self.assertEqual(self.run_migration()["recordings"][0]["status"],"unchanged")
        self.assertEqual(len(list(self.outputs.glob("*.json"))),2)

    def test_source_rollback_has_an_explicit_current_mapping_not_history_order(self):
        first=self.run_migration()["recordings"][0]
        self.write_source("one.json",source_macro(key=75))
        second=self.run_migration()["recordings"][0]
        self.write_source("one.json",source_macro())
        reverted=self.run_migration()["recordings"][0]
        manifest=self.read_manifest()
        self.assertEqual(reverted["file"],first["file"])
        self.assertEqual(manifest["recordings"][-1]["source_sha256"],second["source_sha256"])
        self.assertEqual(manifest["active_recordings"][0]["source_sha256"],first["source_sha256"])
        self.assertEqual(manifest["active_recordings"][0]["file"],first["file"])
        self.assertEqual(len(list(self.outputs.glob("*.json"))),2)

    def test_empty_source_clears_current_mapping_but_preserves_history_and_output(self):
        initial=self.run_migration()["recordings"][0]
        target=self.outputs/initial["file"]
        original=target.read_bytes()
        self.file.unlink()
        result=self.run_migration()
        self.assertEqual(result["errors"],[])
        self.assertEqual(result["recordings"],[])
        self.assertTrue(result["warnings"])
        manifest=self.read_manifest()
        self.assertEqual(manifest["active_recordings"],[])
        self.assertEqual(len(manifest["recordings"]),1)
        self.assertEqual(target.read_bytes(),original)

    def test_name_collision_gets_deterministic_suffix_and_preserves_existing(self):
        self.outputs.mkdir(parents=True)
        conflicting=self.outputs/"原录制.json"
        conflicting.write_bytes(b'private-user-file')
        first=self.run_migration(dry_run=True)
        second=self.run_migration(dry_run=True)
        self.assertEqual(first["recordings"][0]["file"],second["recordings"][0]["file"])
        self.assertNotEqual(first["recordings"][0]["file"],conflicting.name)
        actual=self.run_migration()
        self.assertEqual(actual["errors"],[])
        self.assertEqual(conflicting.read_bytes(),b'private-user-file')

    def test_two_different_sources_with_same_name_get_separate_files(self):
        self.write_source("two.json",source_macro(ident="source-two",key=75))
        result=self.run_migration()
        self.assertEqual(result["errors"],[])
        names=[record["file"].casefold() for record in result["recordings"]]
        self.assertEqual(len(set(names)),2)
        self.assertEqual(len(self.read_manifest()["recordings"]),2)

    def test_duplicate_source_id_and_version_share_one_output(self):
        self.write_source("alias.json",source_macro())
        result=self.run_migration()
        self.assertEqual(result["errors"],[])
        self.assertEqual({r["status"] for r in result["recordings"]},{"migrated","duplicate"})
        self.assertEqual(len(list(self.outputs.glob("*.json"))),1)
        self.assertEqual(len(self.read_manifest()["recordings"]),1)

    def test_renamed_source_file_does_not_duplicate_the_same_version(self):
        initial=self.run_migration()["recordings"][0]
        self.file.rename(self.scripts/"renamed.json")
        result=self.run_migration()
        self.assertEqual(result["errors"],[])
        self.assertEqual(result["recordings"][0]["status"],"unchanged")
        self.assertEqual(result["recordings"][0]["file"],initial["file"])
        self.assertEqual(len(list(self.outputs.glob("*.json"))),1)

    def test_deleted_output_is_restored_exclusively(self):
        initial=self.run_migration()["recordings"][0]
        path=self.outputs/initial["file"]
        path.unlink()
        result=self.run_migration()
        self.assertEqual(result["errors"],[])
        self.assertEqual(result["recordings"][0]["status"],"restored")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),initial["sha256"])

    def test_invalid_second_source_causes_zero_writes_after_full_preflight(self):
        self.write_source("two.json",dict(schema="wrong"))
        result=self.run_migration()
        self.assertTrue(result["errors"])
        self.assertEqual(result["recordings"],[])
        self.assertFalse(self.target.exists())

    def test_incomplete_pairs_are_not_repaired_or_written_silently(self):
        invalid=source_macro(); invalid["events"].pop(1)
        self.write_source("one.json",invalid)
        result=self.run_migration()
        self.assertTrue(result["errors"])
        self.assertIn("未释放",result["errors"][0])
        self.assertFalse(self.target.exists())

    def test_source_change_during_preflight_aborts_before_creating_target(self):
        original_verify=migration._verify_sources
        def changed(plans,scripts):
            self.file.write_text(json.dumps(source_macro(key=75)),encoding="utf8")
            return original_verify(plans,scripts)
        with patch.object(migration,"_verify_sources",side_effect=changed):
            result=self.run_migration()
        self.assertTrue(result["errors"])
        self.assertFalse(self.target.exists())

    def test_source_change_during_publication_rolls_back_created_outputs(self):
        original_write=migration._atomic_write
        injected=[]
        def external_change(path,raw,**kwargs):
            original_write(path,raw,**kwargs)
            if path.parent==self.outputs:
                injected.append(path)
                self.file.write_text(json.dumps(source_macro(key=75)),encoding="utf8")
        with patch.object(migration,"_atomic_write",side_effect=external_change):
            result=self.run_migration()
        self.assertEqual(len(injected),1,"source-change injection did not reach the output file")
        self.assertTrue(result["errors"])
        self.assertEqual(list(self.outputs.iterdir()),[])
        self.assertFalse(self.manifest.exists())

    def test_second_write_failure_rolls_back_first_and_never_commits_manifest(self):
        self.write_source("two.json",source_macro(ident="source-two",name="第二份"))
        original_write=migration._atomic_write
        count=[0]
        def fail_second(path,raw,**kwargs):
            if path.parent==self.outputs:
                count[0]+=1
                if count[0]==2: raise OSError("fake publication failure")
            return original_write(path,raw,**kwargs)
        with patch.object(migration,"_atomic_write",side_effect=fail_second):
            result=self.run_migration()
        self.assertEqual(count[0],2,"second-write failure injection did not reach both outputs")
        self.assertTrue(result["errors"])
        self.assertEqual(list(self.outputs.iterdir()),[])
        self.assertFalse(self.manifest.exists())
        self.assertFalse((self.manifest.parent/".scripts.lock").exists())

    def test_manifest_commit_failure_preserves_old_manifest_and_old_recording(self):
        initial=self.run_migration()["recordings"][0]
        before_manifest=self.manifest.read_bytes()
        before_output=(self.outputs/initial["file"]).read_bytes()
        self.write_source("two.json",source_macro(ident="source-two",name="第二份"))
        original_write=migration._atomic_write
        injected=[]
        def fail_manifest(path,raw,**kwargs):
            if path==self.manifest:
                injected.append(path)
                raise OSError("fake manifest failure")
            return original_write(path,raw,**kwargs)
        with patch.object(migration,"_atomic_write",side_effect=fail_manifest):
            result=self.run_migration()
        self.assertEqual(injected,[self.manifest],"manifest failure injection was not exercised")
        self.assertTrue(result["errors"])
        self.assertEqual(self.manifest.read_bytes(),before_manifest)
        self.assertEqual((self.outputs/initial["file"]).read_bytes(),before_output)
        self.assertEqual(len(list(self.outputs.glob("*.json"))),1)

    def test_temp_cleanup_failure_after_publication_never_rolls_back_committed_outputs(self):
        original_unlink=Path.unlink
        def locked_temporary(path,*args,**kwargs):
            if path.name.startswith(".migration-"):
                raise PermissionError("fake antivirus lock")
            return original_unlink(path,*args,**kwargs)
        with patch.object(Path,"unlink",locked_temporary):
            result=self.run_migration()
        self.assertEqual(result["errors"],[])
        self.assertTrue(any("临时文件" in warning for warning in result["warnings"]))
        self.assertTrue(self.manifest.exists())
        record=self.read_manifest()["recordings"][0]
        output=self.outputs/record["file"]
        self.assertTrue(output.exists())
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(),record["sha256"])

    def test_concurrent_destination_creation_is_not_overwritten_or_removed(self):
        original_write=migration._atomic_write
        injected=[]
        def competing_writer(path,raw,**kwargs):
            if path.parent==self.outputs:
                injected.append(path)
                path.write_bytes(b"concurrent-user-file")
            return original_write(path,raw,**kwargs)
        with patch.object(migration,"_atomic_write",side_effect=competing_writer):
            result=self.run_migration()
        self.assertEqual(len(injected),1,"concurrent-file injection did not reach the output file")
        self.assertTrue(result["errors"])
        self.assertEqual((self.outputs/"原录制.json").read_bytes(),b"concurrent-user-file")
        self.assertFalse(self.manifest.exists())

    def test_corrupt_manifest_is_never_replaced(self):
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_bytes(b"{broken")
        result=self.run_migration()
        self.assertTrue(result["errors"])
        self.assertEqual(self.manifest.read_bytes(),b"{broken")
        self.assertFalse(self.outputs.exists())

    def test_wheel_conversion_is_preserved_with_explicit_playback_warning(self):
        source=source_macro()
        source["events"].append(dict(t_ms=450,kind="wheel",data=dict(x=100,y=200,delta=120,axis="vertical")))
        self.write_source("one.json",source)
        result=self.run_migration()
        self.assertEqual(result["errors"],[])
        self.assertTrue(any("滚轮" in warning for warning in result["warnings"]))
        raw=json.loads((self.outputs/result["recordings"][0]["file"]).read_text(encoding="utf8"))
        self.assertEqual(raw["actions"][-1]["dy"],1)
        self.assertIn("x",raw["actions"][-1])

    def test_high_precision_wheel_preflight_refuses_silent_rounding(self):
        source=source_macro()
        source["events"].append(dict(t_ms=450,kind="wheel",data=dict(x=100,y=200,delta=60,axis="vertical")))
        self.write_source("one.json",source)
        result=self.run_migration()
        self.assertTrue(result["errors"])
        self.assertFalse(self.target.exists())

    def test_unsafe_source_name_is_sanitized_inside_recordings(self):
        self.write_source("one.json",source_macro(name="../CON:private"))
        result=self.run_migration()
        self.assertEqual(result["errors"],[])
        path=self.outputs/result["recordings"][0]["file"]
        self.assertEqual(path.resolve().parent,self.outputs.resolve())
        self.assertEqual(result["recordings"][0]["source_name"],"../CON:private")

    def test_cli_dry_run_returns_json_without_writing_private_data(self):
        result=subprocess.run([sys.executable,"-B",str(ROOT/"tools/migrate_zaomeng_scripts.py"),
            "--source",str(self.source),"--target",str(self.target),"--dry-run"],capture_output=True,check=False)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout.decode("utf8"))
        self.assertEqual(report["errors"],[])
        self.assertEqual(report["recordings"][0]["status"],"planned")
        self.assertFalse(self.target.exists())


if __name__=="__main__": unittest.main()
