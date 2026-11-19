"""Project-local OpenType lettering; font software is never copied or exported."""
import base64
import hashlib
import html
from io import BytesIO
import math
import os
from pathlib import Path
import string

from PIL import Image, ImageDraw, ImageFont
from . import neutral_skin
from .io import read, write, locked

NEUTRAL={'version':1,'kind':'neutral','renderer':'pillow-aileron-neutral'}
_REFERENCE=string.ascii_letters+string.digits


def font_profile(path):
    path=Path(path).expanduser().resolve()
    if path.suffix.lower() not in ('.otf','.ttf'):
        raise ValueError('Supply a locally licensed .otf or .ttf font file.')
    data=path.read_bytes()
    if len(data)>20_000_000:raise ValueError('Font exceeds 20 MB.')
    font=ImageFont.truetype(BytesIO(data),64)
    family,style=font.getname()
    return {'version':1,'kind':'local-opentype','renderer':'pillow-opentype-v1',
            'path':str(path),'sha256':hashlib.sha256(data).hexdigest(),
            'family':family,'style':style}


def project_profile(project):
    path=Path(project)/'typography.json'
    return read(path) if path.exists() else dict(NEUTRAL)


def run_profile(run,*,frozen=False):
    run=Path(run)
    override=run/'presentation-typography.json'
    if not frozen and override.exists():return read(override)
    path=run/'inputs/typography.json'
    return read(path) if path.exists() else dict(NEUTRAL)


class Lettering:
    def __init__(self,profile=None):
        self.profile=dict(profile or NEUTRAL)
        if self.profile.get('version')!=1:raise ValueError('Unsupported typography version.')
        self.font=None
        if self.profile.get('kind')=='neutral':
            if self.profile!=NEUTRAL:raise ValueError('Invalid neutral typography profile.')
            return
        if self.profile.get('kind')!='local-opentype' or self.profile.get('renderer')!='pillow-opentype-v1':
            raise ValueError('Unsupported typography renderer.')
        # Other machines may map the identical licensed font to a local path.
        # Environment variables never change the required content hash/family.
        path=Path(os.environ.get('SETFORGE_FONT_PATH') or self.profile['path'])
        try:data=path.read_bytes()
        except OSError as exc:
            raise ValueError('Configured font is unavailable. Supply the licensed font locally or set SETFORGE_FONT_PATH to the identical file; no fallback is permitted.') from exc
        if hashlib.sha256(data).hexdigest()!=self.profile.get('sha256'):
            raise ValueError('Configured font content changed; do not silently substitute typography.')
        self.font=ImageFont.truetype(BytesIO(data),64)
        if self.font.getname()!=(self.profile.get('family'),self.profile.get('style')):
            raise ValueError('Configured font family/style differ from its saved profile.')
        self.box=self.font.getbbox(_REFERENCE,stroke_width=2)
        self.notdef=bytes(self.font.getmask('\uffff'))

    def describe(self):
        if self.font is None:return 'Pillow Aileron (neutral)'
        return self.profile['family']+' '+self.profile['style']

    def public_profile(self):
        return {key:value for key,value in self.profile.items() if key!='path'}

    def compose_label(self,text,ink_height=18):
        if self.font is None:return neutral_skin.compose_label(text,ink_height)
        if type(ink_height) is not int or ink_height<1:
            raise ValueError('Positive integer ink_height required.')
        if not text or any(ord(c)<32 or ord(c)>126 for c in text):
            raise ValueError('English printable ASCII label required.')
        for ch in set(text)-{' '}:
            if bytes(self.font.getmask(ch))==self.notdef:
                raise ValueError('Configured font lacks an English title character: '+repr(ch))
        box=self.font.getbbox(text,stroke_width=2)
        # A single reference height for the whole font; a short/long word never
        # changes the scale. Preserve case, natural advances and kerning.
        top=min(self.box[1],box[1]);bottom=max(self.box[3],box[3])
        if top!=self.box[1] or bottom!=self.box[3]:
            raise ValueError('Title exceeds the configured font vertical reference.')
        canvas=Image.new('RGBA',(max(1,box[2]-box[0]),self.box[3]-self.box[1]))
        ImageDraw.Draw(canvas).text((-box[0],-self.box[1]),text,font=self.font,
            fill='white',stroke_width=2,stroke_fill='#203a35')
        scale=ink_height/canvas.height
        return canvas.resize((max(1,math.ceil(canvas.width*scale)),ink_height),Image.Resampling.LANCZOS)

    def measure_label(self,text,ink_height=18):
        if self.font is None:return neutral_skin.measure_label(text,ink_height)
        image=self.compose_label(text,ink_height)
        return {'text':text,'width':image.width,'height':image.height,'ink_height':ink_height,
                'baseline':ink_height,'case_rescaled':[],'source':self.describe(),
                'renderer':self.profile['renderer'],'font_file_recovered':True,
                'font_sha256':self.profile['sha256'],'fit_to_width':False}

    def label_svg(self,text,ink_height=18):
        if self.font is None:return neutral_skin.label_svg(text,ink_height)
        image=self.compose_label(text,ink_height);buffer=BytesIO();image.save(buffer,format='PNG')
        uri='data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode('ascii')
        label=html.escape(text,quote=True)
        return (f'<svg class="game-type" width="{image.width}" height="{image.height}" '
                f'viewBox="0 0 {image.width} {image.height}" role="img" aria-label="{label}">'
                f'<title>{label}</title><image href="{uri}" width="{image.width}" height="{image.height}"/></svg>')


def for_run(run,*,frozen=False):
    return Lettering(run_profile(run,frozen=frozen))


def configure(target,font=None,*,run=False,neutral=False):
    target=Path(target).resolve()
    if not (target/('state.json' if run else 'project.json')).is_file():
        raise ValueError('Choose an existing '+('run' if run else 'project')+'.')
    if font is not None and neutral:raise ValueError('Choose a font or neutral typography, not both.')
    destination=target/('presentation-typography.json' if run else 'typography.json')
    with locked(target):
        if font is not None or neutral:
            profile=font_profile(font) if font is not None else dict(NEUTRAL)
            lettering=Lettering(profile);lettering.compose_label('Font Check',18)
            write(destination,profile)
        else:lettering=for_run(target) if run else Lettering(project_profile(target))
    return {'typography':lettering.public_profile(),'font_software_copied':False,
            'scope':'presentation of this run only' if run else 'future orders in this project',
            'title_preflight_required':True}
