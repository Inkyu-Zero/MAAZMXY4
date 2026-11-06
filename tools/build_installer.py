"""Build the offline Inno installer from the same verified full-client ZIP."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request

from build_release import validate_client
from build_sfx import extract_client, sha256_file

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT/'tools/installer'


def verify_binary(path, item, *, signature=True):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or sha256_file(path) != item['sha256']:
        raise ValueError('固定 SHA-256 校验失败：' + str(path))
    if signature:
        env = {k:v for k,v in os.environ.items() if k.casefold() != 'psmodulepath'}
        env['MAA_VERIFY_BINARY']=str(path.resolve())
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            "$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath $env:MAA_VERIFY_BINARY; "
            "@{status=[string]$s.Status;subject=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding='utf8', errors='replace', env=env, check=True)
        data = json.loads(result.stdout)
        if data['status'] != 'Valid' or 'O='+item['publisher']+',' not in data['subject']:
            raise ValueError('签名或发布者校验失败：' + str(path))
    return path


def dependencies(cache, lock, download=False):
    cache = Path(cache).resolve(); cache.mkdir(parents=True, exist_ok=True)
    for item in lock['dependencies']:
        path = cache/item['file']
        if not path.exists() and download:
            temporary = path.with_suffix('.download')
            if temporary.exists(): raise FileExistsError(temporary)
            with urllib.request.urlopen(item['url'], timeout=90) as stream, temporary.open('xb') as out:
                shutil.copyfileobj(stream, out)
            verify_binary(temporary, item)
            temporary.rename(path)
        verify_binary(path, item)
    return cache


def quote(value):
    value = str(value)
    if any(c in value for c in '\r\n"'): raise ValueError('非法构建路径/值')
    return '"' + value + '"'


def build(client_zip, compiler, cache, out, work, *, download=False):
    out, work = Path(out).resolve(), Path(work).resolve()
    if out.suffix.lower() != '.exe' or out.exists() or work.exists():
        raise ValueError('输出必须是新的 .exe，工作目录必须尚不存在。')
    lock = json.loads((SPEC/'dependencies.json').read_text('utf8'))
    verify_binary(compiler, dict(sha256=lock['compiler']['iscc_sha256'], publisher=lock['compiler']['publisher']))
    cache = dependencies(cache, lock, download)
    work.mkdir(parents=True); stage = work/'client'; stage.mkdir()
    extract_client(client_zip, stage); validate_client(stage)
    runtime = json.loads((stage/'MFAAvalonia.runtimeconfig.json').read_text('utf8'))['runtimeOptions']['framework']
    if runtime != {'name':'Microsoft.NETCore.App', 'version':'10.0.0'}:
        raise ValueError('前端运行时要求已改变，须先更新安装器依赖清单。')
    version = json.loads((stage/'interface.json').read_text('utf8'))['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?', version): raise ValueError('资源版本格式无效')
    script = work/'script'; script.mkdir()
    for name in ('client.iss', 'policy.iss', 'install_support.py'):
        shutil.copy2(SPEC/name, script/name)
    shutil.copy2(SPEC/'dependencies.json', stage/'installer-dependencies.json')
    (stage/'NOTICE-installer.txt').write_text(
        'Installer: Inno Setup 7.1.1, Jordan Russell / Martijn Laan. https://jrsoftware.org/\n'
        'Bundled Microsoft installers are unmodified; their license terms apply.\n'+
        '\n'.join(d['id']+' '+d['version']+'\n'+d['url']+'\n'+d['license'] for d in lock['dependencies'])+'\n', 'utf8')
    files = sorted(p.relative_to(stage).as_posix() for p in stage.rglob('*') if p.is_file())
    (stage/'installer-files.json').write_text(json.dumps(dict(schema=1, files=files), ensure_ascii=False, indent=2), 'utf8')
    files.append('installer-files.json')
    lines = []
    for name in files:
        destination = str(Path(name).parent).replace('/', '\\')
        flags = 'ignoreversion'
        if name == 'interface.json': flags += ' uninsneveruninstall'
        if name.startswith('agent/recordings/'): flags += ' onlyifdoesntexist uninsneveruninstall'
        lines.append('Source: '+quote(stage/name)+'; DestDir: '+quote('{app}'+('\\'+destination if destination != '.' else ''))+'; Flags: '+flags)
    (script/'payload-files.iss').write_text('\n'.join(lines)+'\n', 'utf-8-sig')
    values = dict(ClientVersion=version, FileVersion='.'.join(version.split('-')[0].split('.')[:3])+'.0',
                  OutputDir=str(out.parent), OutputName=out.stem, IconPath=str(ROOT/'新图标.ico'),
                  LicensePath=str(ROOT/'LICENSE'), DependencyDir=str(cache))
    for item in lock['dependencies']:
        prefix = 'DotNet' if item['id']=='dotnet' else 'Vc'
        values[prefix+'Minimum']=item['minimum']; values[prefix+'Hash']=item['sha256']
    (script/'build-values.iss').write_text('\n'.join('#define '+key+' '+quote(value) for key,value in values.items()), 'utf-8-sig')
    out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([str(Path(compiler).resolve()), str(script/'client.iss')],
                            capture_output=True, text=True, encoding='utf8', errors='replace')
    (work/'compile.log').write_text(result.stdout+'\n'+result.stderr, 'utf8')
    if result.returncode or not out.is_file(): raise RuntimeError('Inno 编译失败：'+(result.stdout+result.stderr)[-2500:])
    receipt = dict(schema=1, client_version=version, client_zip_sha256=sha256_file(client_zip),
                   installer_sha256=sha256_file(out), size=out.stat().st_size, dependencies=lock,
                   sources={p.name:sha256_file(p) for p in SPEC.iterdir() if p.is_file()},
                   installed_or_launched=False)
    out.with_suffix('.build.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), 'utf8')
    return out


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('client-zip','compiler','cache','out','work'): parser.add_argument('--'+name, required=True)
    parser.add_argument('--download',action='store_true',help='下载清单中的依赖；必须通过固定哈希和签名校验')
    args=parser.parse_args()
    print(build(args.client_zip,args.compiler,args.cache,args.out,args.work,download=args.download))


if __name__=='__main__': main()
