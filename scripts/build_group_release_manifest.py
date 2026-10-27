#!/usr/bin/env python3
"""Hash tracked source and a separately built frontend; never package runtime data."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(frontend):
    tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
    sources={}
    for name in tracked:
        path=ROOT/name
        if not name or path.is_symlink() or not path.is_file():
            continue
        if name.startswith(('data/','backups/','static/','scratch/','.env')):
            continue
        if path.suffix in {'.py','.ts','.tsx','.css','.json','.html','.svg','.sh','.md','.txt'} or name=='.gitignore':
            sources[name]=digest(path)
    assets={str(path.relative_to(frontend)):digest(path) for path in sorted(frontend.rglob('*'))
            if path.is_file() and not path.is_symlink()}
    if 'index.html' not in assets:
        raise ValueError('Build the frontend before generating a release manifest')
    return {'git_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),
            'source_sha256':sources,'frontend_sha256':assets,'selector_version':'v2',
            'schema_runtime_minimum':19,'membership_migration':20,
            'deploy_authorized':False,'facebook_submit_authorized':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend-dir',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    args.output.write_text(json.dumps(build(args.frontend_dir.resolve()),indent=2)+'\n')
