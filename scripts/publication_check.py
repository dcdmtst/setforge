"""Check the exact source allowlist, private-data patterns and unchanged engine."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
TOP_FILES={'README.md','SETUP_AGENT.md','AGENTS.md','LICENSE','NOTICE.md','PUBLISH.md',
           'BASELINE.json','pyproject.toml','Dockerfile','.gitignore','.gitattributes','.dockerignore'}
TOP_DIRS={'src','tests','docs','scripts','examples','.agents','.github'}
EXTENSIONS={'.py','.md','.json','.toml','.html','.yaml','.yml','.gitignore','.gitattributes','.dockerignore'}
PATTERNS=[r'(?i)[a-z]:[/\\]Users[/\\]',r'(?i)\b[A-Z0-9._%+-]+@(?:gmail|hotmail|outlook|yahoo)\.[A-Z]{2,}\b',
          r'(?i)miro\.com/app/board/',r'\bsk-[A-Za-z0-9_-]{20,}',
          r'\b(?:ghp_|github_pat_|hf_)[A-Za-z0-9_]{20,}',r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----']


def source_files(root=ROOT):
    for path in sorted(root.rglob('*')):
        if not path.is_file():continue
        parts=path.relative_to(root).parts
        if parts[0] in TOP_FILES and len(parts)==1:yield path
        elif parts[0] in TOP_DIRS and not any(x in {'__pycache__','.pytest_cache'} or x.endswith('.egg-info') for x in parts):yield path


def check(root=ROOT,tracked=False):
    root=Path(root);errors=[];selected=list(source_files(root));allowed={p.relative_to(root).as_posix() for p in selected}
    for path in selected:
        rel=path.relative_to(root).as_posix()
        if path.name not in TOP_FILES and path.suffix not in EXTENSIONS and rel!='.github/CODEOWNERS':errors.append('Unapproved source type: '+rel)
        if any(part.lower() in {'vendor','outputs','raw','producer-runs','runs','references','work','secrets'} for part in path.relative_to(root).parts):
            errors.append('Private/runtime directory: '+rel)
        try:content=path.read_text(encoding='utf-8')
        except (UnicodeError,OSError):errors.append('Non-text file: '+rel);continue
        for pattern in PATTERNS:
            if re.search(pattern,content):errors.append('Private-data pattern in '+rel);break
    for required in ('README.md','SETUP_AGENT.md','LICENSE','NOTICE.md','AGENTS.md','PUBLISH.md'):
        if not (root/required).is_file():errors.append('Missing '+required)
    baseline=json.loads((root/'BASELINE.json').read_text(encoding='utf8'))
    for name,expected in baseline['unchanged_core_modules'].items():
        path=root/'src/setforge'/name
        if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:errors.append('Engine baseline changed: '+name)
    if tracked:
        result=subprocess.run(['git','ls-files','-z'],cwd=root,capture_output=True,check=True)
        for name in result.stdout.decode().split('\0'):
            if name and name not in allowed:errors.append('Tracked outside source allowlist: '+name)
    return {'pass':not errors,'source_files':len(selected),'engine_baseline':baseline['engine_baseline'],'errors':errors}


def main():
    p=argparse.ArgumentParser();p.add_argument('--tracked',action='store_true');p.add_argument('--archive',type=Path)
    args=p.parse_args();result=check(tracked=args.tracked)
    if args.archive and result['pass']:
        out=args.archive.resolve()
        if out.exists():raise SystemExit('Archive already exists; choose a new filename.')
        out.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
            for path in source_files():archive.write(path,'setforge/'+path.relative_to(ROOT).as_posix())
        result['archive']=str(out);result['sha256']=hashlib.sha256(out.read_bytes()).hexdigest()
    print(json.dumps(result,indent=2));return 0 if result['pass'] else 1


if __name__=='__main__':raise SystemExit(main())
