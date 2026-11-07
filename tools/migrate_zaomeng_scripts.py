"""Migrate private ZaoMengScript recordings without changing their source files.

The manifest and migrated recordings are local user data, not PR artifacts.
Every input is converted and semantically checked before publishing any output.
Existing files are never overwritten; the manifest is committed last. Repeated
runs use source IDs/hashes rather than random editor IDs to identify versions.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from script_studio.model import MAX_FILE_BYTES, RecordingDocument

MANIFEST_SCHEMA = "maazmxy4.zaomeng-script-migration"
MANIFEST_VERSION = 1
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1,10)),
                     *(f"LPT{i}" for i in range(1,10))}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(path, maximum=MAX_FILE_BYTES):
    with path.open("rb") as stream:
        data = stream.read(maximum+1)
    if len(data) > maximum:
        raise ValueError(f"文件不能超过 {maximum} 字节：{path.name}")
    return data


def _decode(raw):
    def reject_constant(value):
        raise ValueError("不能包含非有限 JSON 数值：" + value)
    return json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant)


def _encode(data):
    raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+"\n").encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("转换后的文件不能超过 16 MiB")
    return raw


def _atomic_write(path, raw, *, overwrite=False):
    """Publish a complete file; hard-link publication is exclusive on Windows too."""
    temporary, published, cleanup_warning = None, False, None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".migration-",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        published=True
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError as error:
                if not published:
                    raise
                # The destination is already complete and authoritative. Treat
                # a locked temporary hard link as a cleanup warning, rather
                # than rolling back recordings after a manifest was committed.
                cleanup_warning=f"{path.name} 已完整写入，但临时文件 {temporary.name} 暂未清理：{error}"
    return cleanup_warning


def _filename(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(". ")
    if not name or name.startswith("."):
        name = "录制_" + name.lstrip(".")
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "录制_" + name
    if name.lower().endswith(".json"):
        name = name[:-5]
    return name[:140].rstrip(". ") + ".json"


def _target_path(directory, name):
    if (not isinstance(name,str) or not name or name.startswith(".") or Path(name).name != name
            or re.search(r'[<>:"/\\|?*\x00-\x1f]',name) or not name.lower().endswith(".json")):
        raise ValueError("迁移清单包含无效目标文件名")
    path = directory / name
    if path.resolve().parent != directory.resolve():
        raise ValueError("目标脚本不能指向 recordings 目录以外")
    return path


def _manifest(path):
    if not path.exists():
        return dict(schema=MANIFEST_SCHEMA,version=MANIFEST_VERSION,recordings=[]), None
    raw = _read(path)
    data = _decode(raw)
    if (not isinstance(data,dict) or data.get("schema") != MANIFEST_SCHEMA
            or data.get("version") != MANIFEST_VERSION or not isinstance(data.get("recordings"),list)):
        raise ValueError("既有迁移清单损坏或版本不支持，未覆盖")
    required = {"source_id","source_file","source_sha256","file","sha256"}
    if "active_recordings" in data and not isinstance(data["active_recordings"],list):
        raise ValueError("既有迁移清单的当前版本映射无效，未覆盖")
    for record in data["recordings"]+data.get("active_recordings",[]):
        if not isinstance(record,dict) or not required.issubset(record):
            raise ValueError("既有迁移清单记录不完整，未覆盖")
        if not all(isinstance(record[key],str) for key in required):
            raise ValueError("既有迁移清单字段类型无效，未覆盖")
        for key in ("source_sha256","sha256"):
            if re.fullmatch(r"[0-9a-f]{64}",record[key]) is None:
                raise ValueError("既有迁移清单哈希无效，未覆盖")
    return data, _sha(raw)


def _semantic_check(source, document):
    snapshot = document.snapshot()
    inputs = [event for event in source["events"] if event["kind"] != "marker"]
    if len(inputs) != len(snapshot["events"]):
        raise ValueError("转换前后输入动作数量不一致")
    if source["duration_ms"] != snapshot["duration_ms"]:
        raise ValueError("转换前后总时长不一致")
    expected_markers = [(e["t_ms"],e["data"]["label"]) for e in source["events"] if e["kind"] == "marker"]
    if expected_markers != [(m["at_ms"],m["label"]) for m in snapshot["markers"]]:
        raise ValueError("转换前后标记或标记时间不一致")
    width,height = source["window"]["width"],source["window"]["height"]
    scale = 720/min(width,height)
    if snapshot["base"] != [round(width*scale),round(height*scale)]:
        raise ValueError("转换后的720基准尺寸不一致")
    raw = document.to_dict()
    for original,converted in zip(inputs,snapshot["events"]):
        if original["t_ms"] != converted["at_ms"]:
            raise ValueError("转换前后动作时间不一致")
        payload = original["data"]
        if original["kind"].startswith("key_"):
            if converted.get("key") != payload["vk"]:
                raise ValueError("转换前后键码不一致")
        else:
            expected_x = min(snapshot["base"][0]-1,round(payload["x"]*scale))
            expected_y = min(snapshot["base"][1]-1,round(payload["y"]*scale))
            if (converted.get("x"),converted.get("y")) != (expected_x,expected_y):
                raise ValueError("转换前后坐标不符合缩放约定")
            if original["kind"] in {"mouse_down","mouse_up"} and converted.get("button") != payload["button"]:
                raise ValueError("转换前后鼠标按钮不一致")
            if original["kind"] == "wheel":
                amount = payload["delta"]//120
                expected = (amount,0) if payload["axis"] == "horizontal" else (0,amount)
                if (converted.get("dx"),converted.get("dy")) != expected:
                    raise ValueError("转换前后滚轮单位不一致")
        if raw["editor"]["source_events"].get(converted["id"]) != payload:
            raise ValueError("原始输入 metadata 未完整保留")
    original_metadata = {key:value for key,value in source.items() if key != "events"}
    if raw["editor"]["source_macro"] != original_metadata:
        raise ValueError("源录制 metadata 未完整保留")
    reloaded = RecordingDocument.from_dict(raw)
    if reloaded.snapshot() != snapshot:
        raise ValueError("转换后的磁盘格式无法无损重新加载")


def _convert(source, source_file, source_hash):
    if (not isinstance(source,dict) or not isinstance(source.get("id"),str) or not source["id"]
            or not isinstance(source.get("name"),str) or not source["name"].strip()):
        raise ValueError("源录制必须包含非空 id 和 name")
    document = RecordingDocument.from_source_macro(source)
    _semantic_check(source, document)
    raw = document.to_dict()
    # The editor normally creates UUID4 marker IDs. A migration is repeatable:
    # source identity, source version and marker position determine these IDs.
    for index,marker in enumerate(raw["editor"]["markers"]):
        marker["id"] = str(uuid5(NAMESPACE_URL,
            f"maazmxy4-migration:{source['id']}:{source_hash}:marker:{index}"))
    raw["editor"]["migration"] = dict(source_project="ZaoMengScript",source_id=source["id"],
        source_file=source_file,source_sha256=source_hash,
        source_size=[source["window"]["width"],source["window"]["height"]],target_base=raw["base"])
    # Validate the deterministic output, including marker-ID uniqueness.
    verified = RecordingDocument.from_dict(raw)
    verified.to_dict()
    return raw, _encode(raw)


def _identity(record):
    return record["source_id"],record["source_sha256"]


def _choose_file(directory, record, versioned, expected_hash, existing, reserved):
    base = _filename(record["source_name"])
    stem = base[:-5]
    ident = re.sub(r"[^A-Za-z0-9]","",record["source_id"])[:12] or _sha(record["source_id"].encode())[:12]
    source_hash = record["source_sha256"]
    unique = _sha((record["source_id"]+record["source_file"]+source_hash).encode())[:16]
    candidates = ([f"{stem}-{source_hash[:8]}.json"] if versioned else [base]) + [
        f"{stem}-{ident}.json",f"{stem}-{ident}-{source_hash[:8]}.json",f"{stem}-{unique}.json"]
    for name in candidates:
        folded = name.casefold()
        if folded in reserved:
            continue
        actual = existing.get(folded)
        if actual is not None:
            path = _target_path(directory,actual)
            if path.is_file() and _sha(_read(path)) == expected_hash:
                reserved.add(folded)
                return actual, "existing"
            continue
        reserved.add(folded)
        return name, "migrated"
    raise ValueError("所有确定性目标文件名均已占用，未覆盖已有数据")


def _verify_sources(plans, scripts):
    current = sorted(path.name for path in scripts.glob("*.json"))
    if current != sorted(plan["path"].name for plan in plans):
        raise ValueError("迁移期间源脚本列表发生变化，未提交迁移")
    for plan in plans:
        if _sha(_read(plan["path"])) != plan["record"]["source_sha256"]:
            raise ValueError("迁移期间源脚本已被修改：" + plan["path"].name)


def migrate_scripts(source_root: Path, target_root: Path, dry_run=False):
    """Return a JSON-compatible report. ``event_count`` excludes marker events.

    Status is migrated/planned, unchanged, existing, restored, or target_modified.
    A modified target is preserved and reported, rather than being silently
    re-imported over the user's work. New source versions get separate names.
    """
    source_root,target_root = Path(source_root).resolve(),Path(target_root).resolve()
    result = dict(source_root=str(source_root),recordings=[],errors=[],warnings=[])
    scripts, directory = source_root/"data/scripts", target_root/"recordings"
    manifest_path = target_root/".local-migrations/zaomeng/scripts.json"
    plans, created, lock_path, lock_owned = [], [], manifest_path.parent/".scripts.lock", False
    try:
        if not scripts.is_dir():
            raise ValueError("源脚本目录不存在：" + str(scripts))
        if directory.resolve().parent != target_root or not manifest_path.resolve().is_relative_to(target_root):
            raise ValueError("目标数据目录不能指向指定工程以外的位置")
        manifest, manifest_hash = _manifest(manifest_path)
        for entry in manifest["recordings"]:
            _target_path(directory,entry["file"])
        existing = {path.name.casefold():path.name for path in directory.iterdir()} if directory.exists() else {}
        reserved = set()
        for path in sorted(scripts.glob("*.json")):
            try:
                if not path.is_file() or not path.resolve().is_relative_to(source_root):
                    raise ValueError("源脚本不能指向指定源工程以外的位置")
                initial = _read(path)
                source_hash = _sha(initial)
                source_file = path.relative_to(source_root).as_posix()
                source = _decode(initial)
                raw, encoded = _convert(source,source_file,source_hash)
                target_hash = _sha(encoded)
                record = dict(source_id=source["id"],source_file=source_file,source_name=source["name"],
                    source_sha256=source_hash,file="",sha256=target_hash,duration_ms=raw["duration_ms"],
                    event_count=len(raw["actions"]),marker_count=len(raw["editor"]["markers"]),status="migrated")
                alias = next((plan for plan in plans if _identity(plan["record"])==_identity(record)),None)
                matched = next((entry for entry in reversed(manifest["recordings"]) if _identity(entry)==_identity(record)),None)
                if alias is not None:
                    record.update(file=alias["record"]["file"],sha256=alias["record"]["sha256"],status="duplicate")
                    encoded=alias["bytes"]
                    result["warnings"].append(f"{path.name} 与同一源ID/版本重复，共用 {record['file']}")
                elif matched is not None:
                    name = matched["file"]
                    destination = _target_path(directory,name)
                    if name.casefold() in reserved:
                        raise ValueError("迁移清单中的多条记录指向同一目标文件")
                    reserved.add(name.casefold())
                    if destination.exists():
                        actual_hash = _sha(_read(destination))
                        record.update(file=name,sha256=actual_hash,
                                      status="unchanged" if actual_hash==matched["sha256"] else "target_modified")
                        if record["status"]=="target_modified":
                            result["warnings"].append(f"{name} 已由用户修改，保留当前文件，未覆盖")
                    else:
                        record.update(file=name,status="restored")
                else:
                    versioned = any(entry["source_id"]==record["source_id"] for entry in manifest["recordings"])
                    name,status = _choose_file(directory,record,versioned,target_hash,existing,reserved)
                    record.update(file=name,status=status)
                    if versioned:
                        result["warnings"].append(f"{source['name']} 的源文件已更新，新版本另存为 {name}，保留旧版")
                    elif name != _filename(source["name"]):
                        result["warnings"].append(f"原名文件已存在，另存为 {name}，未覆盖")
                if any(action["op"]=="scroll" for action in raw["actions"]):
                    result["warnings"].append(f"{source['name']} 含带坐标滚轮，已完整保留；当前后台回放会明确拒绝，需另行适配")
                if _sha(_read(path)) != source_hash:
                    raise ValueError("预检期间源文件发生变化")
                plans.append(dict(path=path,record=record,bytes=encoded,matched=matched))
            except Exception as error:
                result["errors"].append(f"{path.name}：{error}")
        if result["errors"]:
            return result
        _verify_sources(plans,scripts)
        if not plans:
            result["warnings"].append("源脚本目录为空，没有需要迁移的录制")
            # Preserve historical files, but an existing manifest must not keep
            # advertising a previously active source batch after it was emptied.
            if manifest_hash is None:
                return result
        if dry_run:
            for plan in plans:
                record=deepcopy(plan["record"])
                if record["status"] in {"migrated","restored"}:
                    record["status"]="planned"
                result["recordings"].append(record)
            return result
        directory.mkdir(parents=True,exist_ok=True)
        manifest_path.parent.mkdir(parents=True,exist_ok=True)
        with lock_path.open("x",encoding="utf8") as stream:
            stream.write(str(os.getpid()))
        lock_owned=True
        current_manifest_hash = _sha(_read(manifest_path)) if manifest_path.exists() else None
        if current_manifest_hash != manifest_hash:
            raise ValueError("预检后迁移清单发生变化，请重试；未覆盖")
        _verify_sources(plans,scripts)
        updated = deepcopy(manifest)
        updated.update(source_root=str(source_root),target_root=str(target_root))
        for plan in plans:
            record=plan["record"]
            destination=_target_path(directory,record["file"])
            if record["status"] in {"migrated","restored"}:
                cleanup_warning=_atomic_write(destination,plan["bytes"])
                created.append((destination,_sha(plan["bytes"])))
                if cleanup_warning:
                    result["warnings"].append(cleanup_warning)
            elif not destination.is_file() or _sha(_read(destination)) != record["sha256"]:
                raise ValueError("预检后目标文件发生变化，未提交：" + destination.name)
            if record["status"] not in {"target_modified","duplicate"}:
                entry=deepcopy(record)
                previous_index=next((i for i,value in enumerate(updated["recordings"]) if _identity(value)==_identity(record)),None)
                if previous_index is None:
                    updated["recordings"].append(entry)
                else:
                    updated["recordings"][previous_index]=entry
        # Historical versions stay available for recovery, but their list order
        # does not identify the current source version (a source can roll back).
        # Consumers should use this explicit mapping, or match ID AND source hash.
        updated["active_recordings"]=[deepcopy(plan["record"]) for plan in plans]
        _verify_sources(plans,scripts)
        # Recheck every published file before making the mapping authoritative.
        for plan in plans:
            record=plan["record"]
            if _sha(_read(_target_path(directory,record["file"]))) != record["sha256"]:
                raise ValueError("目标文件在迁移时发生变化，未提交清单：" + record["file"])
        current_manifest_hash = _sha(_read(manifest_path)) if manifest_path.exists() else None
        if current_manifest_hash != manifest_hash:
            raise ValueError("迁移期间清单被其他程序修改，未覆盖")
        cleanup_warning=_atomic_write(manifest_path,_encode(updated),overwrite=manifest_hash is not None)
        if cleanup_warning:
            result["warnings"].append(cleanup_warning)
        result["recordings"]=[deepcopy(plan["record"]) for plan in plans]
    except Exception as error:
        result["errors"].append(str(error))
        # Roll back only complete files this run created and whose bytes still
        # match our own publication. Never remove a concurrently edited file.
        for path,expected in reversed(created):
            try:
                if path.is_file() and _sha(_read(path))==expected:
                    path.unlink()
                elif path.exists():
                    result["warnings"].append("回滚时保留已被其他程序修改的文件："+path.name)
            except OSError as cleanup_error:
                result["warnings"].append(f"未能回滚 {path.name}：{cleanup_error}")
    finally:
        if lock_owned:
            try:
                lock_path.unlink()
            except OSError as error:
                result["warnings"].append("未能移除迁移锁："+str(error))
    return result


def main():
    parser=argparse.ArgumentParser(description="将个人 ZaoMengScript 录制迁移到 MAAZMXY4 脚本库")
    parser.add_argument("--source",type=Path,required=True,help="ZaoMengScript 工程根目录")
    parser.add_argument("--target",type=Path,required=True,help="MAAZMXY4 工程根目录")
    parser.add_argument("--dry-run",action="store_true",help="只预检，不创建文件")
    args=parser.parse_args()
    result=migrate_scripts(args.source,args.target,args.dry_run)
    if hasattr(sys.stdout,"reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 1 if result["errors"] else 0


if __name__=="__main__":
    raise SystemExit(main())
