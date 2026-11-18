"""Optional explicit OpenAI API adapter, distinct from an agent's imagegen tool."""
import base64
import os
from pathlib import Path
from contextlib import ExitStack
from .io import inside


def invoke_image_api(config,request,run,job,timeout):
    key=os.environ.get(config.get('key_env','OPENAI_API_KEY'))
    if not key:raise ValueError('Explicit API credential is missing; the builtin imagegen tool does not use this adapter.')
    try:from openai import OpenAI
    except ImportError as exc:raise ValueError('Install the optional openai-image dependency for this explicitly selected API adapter.') from exc
    client=OpenAI(api_key=key,timeout=timeout,max_retries=0)
    if not config.get('model'):raise ValueError('Choose an explicit image API model; there is no billing fallback from builtin imagegen.')
    arguments={'model':config['model'],'prompt':request['prompt'],
               'size':config.get('size','auto'),'quality':config.get('quality','medium'),'output_format':'png','n':1}
    with ExitStack() as stack:
        images=[stack.enter_context(inside(run,i['path']).open('rb')) for i in request.get('images',[])]
        data=client.images.edit(image=images,**arguments) if images else client.images.generate(**arguments)
    if not data.data or not data.data[0].b64_json:raise ValueError('Image API did not return PNG data.')
    target=Path(job)/'generated.png';target.write_bytes(base64.b64decode(data.data[0].b64_json,validate=True))
    return {'image_path':str(target),'note':'Explicit optional image API call; no implicit retries or billing substitution.'}
