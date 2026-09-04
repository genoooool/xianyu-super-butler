#!/usr/bin/env python3
"""Assemble fresh verified inputs and sign macOS update assets locally. No upload/install."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tarfile

ROOT=Path(__file__).resolve().parents[2]
TAURI=ROOT/'desktop/src-tauri'


def run(args,**kwargs): subprocess.run([str(x) for x in args],check=True,**kwargs)
def digest(path):
    with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base-app',type=Path,required=True)
    parser.add_argument('--backend',type=Path,required=True)
    parser.add_argument('--native',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--signing-key',type=Path,required=True)
    parser.add_argument('--notes',required=True)
    args=parser.parse_args()
    args.output_dir=args.output_dir.resolve()
    conf=json.loads((TAURI/'tauri.conf.json').read_text()); version=conf['version']
    native_version=subprocess.check_output([str(args.native.resolve()),'--version'],text=True).strip()
    if native_version != version: raise ValueError('Native executable version differs from release config')
    key=args.signing_key.resolve(strict=True)
    if key.is_relative_to(ROOT) or key.stat().st_mode & 0o077:
        raise ValueError('Signing key must be outside the repository and readable only by its owner')
    public=Path(str(key)+'.pub').read_text().strip()
    if public!=conf['plugins']['updater']['pubkey']: raise ValueError('Signing key does not match the app public key')
    if not args.backend.joinpath('xianyu-backend').is_file(): raise ValueError('Missing built backend')
    args.output_dir.mkdir(parents=True,exist_ok=False)
    app=args.output_dir/'闲鱼工作台.app'
    run(['ditto',args.base_app,app])
    resources=app/'Contents/Resources'
    # Preserve superseded generated resources, never delete a user's installed app.
    (resources/'backend').rename(args.output_dir/'inherited-backend-unused')
    run(['ditto',args.backend,resources/'backend'])
    shutil.copy2(args.native,app/'Contents/MacOS/xianyu-workbench')
    plist_path=app/'Contents/Info.plist'
    with plist_path.open('rb') as f: plist=plistlib.load(f)
    if plist['CFBundleIdentifier']!=conf['identifier']: raise ValueError('App identifier changed')
    plist.update(CFBundleShortVersionString=version,CFBundleVersion=version)
    with plist_path.open('wb') as f: plistlib.dump(plist,f)
    arch=subprocess.check_output(['uname','-m'],text=True).strip()
    target='darwin-'+{'arm64':'aarch64','x86_64':'x86_64'}[arch]
    contract=dict(identifier=conf['identifier'],version=version,data_compatibility=1,target=target)
    (resources/'update-contract.json').write_text(json.dumps(contract,indent=2)+'\n')
    run(['codesign','--force','--deep','--sign','-','--entitlements',TAURI/'Entitlements.plist',app])
    run(['codesign','--verify','--deep','--strict',app])
    archive=args.output_dir/f'xianyu-workbench-{version}-{target}.app.tar.gz'
    with tarfile.open(archive,'w:gz',dereference=False) as tar:
        tar.add(app,arcname=app.name)
    # CLI reads its key from a file; never pass private key content on command line.
    env=dict(os.environ)
    env.pop('TAURI_SIGNING_PRIVATE_KEY',None)
    env.setdefault('TAURI_SIGNING_PRIVATE_KEY_PASSWORD','')
    run([ROOT/'desktop/node_modules/.bin/tauri','signer','sign','--private-key-path',key,archive],env=env)
    sig=Path(str(archive)+'.sig').read_text().strip()
    manifest=dict(version=version,notes=args.notes,data_compatibility=1,platforms={target:dict(signature=sig,
        url=f'https://github.com/genoooool/xianyu-super-butler/releases/download/v{version}/{archive.name}')})
    (args.output_dir/'latest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    # ZIP is the manual-download alternative; user drags the contained App to Applications.
    zip_path=args.output_dir/f'xianyu-workbench-{version}-{target}.zip'
    run(['ditto','-c','-k','--sequesterRsrc','--keepParent',app,zip_path])
    result=dict(version=version,app=str(app),archive=str(archive),zip=str(zip_path),
                archive_sha256=digest(archive),native_sha256=digest(app/'Contents/MacOS/xianyu-workbench'),
                backend_sha256=digest(resources/'backend/xianyu-backend'),published=False,installed=False)
    (args.output_dir/'candidate.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__': main()
