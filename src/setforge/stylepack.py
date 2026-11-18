"""Import user-supplied local style references. No private-material downloads."""
from pathlib import Path
import re
import shutil
from PIL import Image
from .io import inside,read,sha,write,locked


def configure(project,folder):
    project=Path(project).resolve()
    with locked(project):
        config=read(project/'project.json')
        refs,style,collection_id=import_pack(folder,project)
        config.update(collection_id=collection_id,global_reference_ids=[r['id'] for r in refs[:3]])
        (project/'style.md').write_text(style,encoding='utf8')
        write(project/'references.json',refs);write(project/'project.json',config)
    return {'project':str(project),'reference_count':len(refs),'scope':'future runs only; existing input snapshots are unchanged'}


def import_pack(folder,project):
    folder=Path(folder).resolve();project=Path(project).resolve();spec=read(folder/'style-pack.json')
    if spec.get('version')!=1 or not isinstance(spec.get('references'),list) or not spec['references']:
        raise ValueError('Style pack version1 needs a nonempty references list.')
    collection_id=spec.get('id','collection')
    if not re.fullmatch('[a-z][a-z0-9_-]{0,47}',collection_id):raise ValueError('Invalid style pack id.')
    style=inside(folder,spec.get('style','style.md')).read_text(encoding='utf8')
    if not style.strip():raise ValueError('Style instructions are empty.')
    refs=[];seen=set();prepared=[]
    for item in spec['references']:
        key=item['id']
        if not re.fullmatch('[a-z][a-z0-9_-]{0,47}',key) or key in seen:
            raise ValueError('Reference IDs must be distinct lowercase identifiers.')
        seen.add(key);source=inside(folder,item['path'])
        with Image.open(source) as im:
            if im.format not in ('PNG','JPEG','WEBP'):raise ValueError('Use PNG, JPEG or WebP references.')
            im.verify()
        relative='references/'+key+source.suffix.lower();prepared.append((source,project/relative))
        refs.append({'id':key,'path':relative,'title':item.get('title',key),
                     'description':item.get('description','User-provided style reference'),
                     'set':item.get('set','style-pack'),'tags':item.get('tags',[]),
                     'background':item.get('background'),'sha256':sha(source)})
    for source,destination in prepared:
        destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,destination)
    return refs,style,collection_id
