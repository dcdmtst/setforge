"""Neutral lettering; reference star artwork is provided by rating_ui."""
from functools import lru_cache
from io import BytesIO
import base64
import html
from PIL import Image, ImageDraw, ImageFont
from .rating_ui import star_svg


@lru_cache(maxsize=1)
def _font():
    return ImageFont.load_default(size=64)


def compose_label(text, ink_height=18):
    if not isinstance(ink_height,int) or ink_height<1:
        raise ValueError('Positive integer ink_height required.')
    if not text or any(ord(c)<32 or ord(c)>126 for c in text):
        raise ValueError('English printable ASCII label required.')
    shown=text.upper();font=_font();box=font.getbbox(shown,stroke_width=2)
    canvas=Image.new('RGBA',(max(1,box[2]-box[0]),max(1,box[3]-box[1])))
    ImageDraw.Draw(canvas).text((-box[0],-box[1]),shown,font=font,fill='white',stroke_width=2,stroke_fill='#203a35')
    return canvas.resize((max(1,round(canvas.width*ink_height/canvas.height)),ink_height),Image.Resampling.LANCZOS)


def measure_label(text,ink_height=18):
    image=compose_label(text,ink_height)
    return {'text':text,'width':image.width,'height':image.height,'ink_height':ink_height,
            'baseline':ink_height,'case_rescaled':[],
            'source':'Neutral uppercase lettering from Pillow bundled Aileron',
            'renderer':'pillow-aileron-neutral','font_file_recovered':False,'fit_to_width':False}


@lru_cache(maxsize=512)
def label_svg(text,ink_height=18):
    image=compose_label(text,ink_height);buffer=BytesIO();image.save(buffer,format='PNG')
    uri='data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode('ascii')
    label=html.escape(text,quote=True)
    return (f'<svg class="game-type" width="{image.width}" height="{image.height}" '
            f'viewBox="0 0 {image.width} {image.height}" role="img" aria-label="{label}">'
            f'<title>{label}</title><image href="{uri}" width="{image.width}" height="{image.height}"/></svg>')
