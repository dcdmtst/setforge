"""Original reference star groups, including their native five-star arc."""
import base64
from functools import lru_cache
from importlib.resources import files
import json


@lru_cache(maxsize=1)
def provenance():
    return json.loads(files('setforge').joinpath('defaults/ui/rating-stars.json').read_text(encoding='utf8'))


@lru_cache(maxsize=5)
def _uri(count):
    data=files('setforge').joinpath(f'defaults/ui/rating-stars-{count}.png').read_bytes()
    return 'data:image/png;base64,'+base64.b64encode(data).decode('ascii')


def rating_group_svg(count):
    if type(count) is not int or not 1<=count<=5:
        raise ValueError('Rating must be an integer from 1 to 5.')
    spec=provenance();w=spec['canvas_width'];h=spec['canvas_height']
    return (f'<svg class="rating-stars" viewBox="0 0 {w} {h}" aria-hidden="true">'
            f'<image href="{_uri(count)}" width="{w}" height="{h}"/></svg>')


def star_svg():
    spec=provenance();x,y,right,bottom=spec['assets'][0]['alpha_bounds']
    return (f'<svg class="star" viewBox="{x} {y} {right-x} {bottom-y}" aria-hidden="true">'
            f'<image href="{_uri(1)}" width="{spec["canvas_width"]}" height="{spec["canvas_height"]}"/></svg>')


def rating_image(count,card_width):
    """Raster preview uses the same native group and card-relative anchor."""
    from PIL import Image
    spec=provenance();scale=card_width/spec['reference_card_width']
    with files('setforge').joinpath(f'defaults/ui/rating-stars-{count}.png').open('rb') as source:
        image=Image.open(source).convert('RGBA').resize(
            (round(spec['canvas_width']*scale),round(spec['canvas_height']*scale)),Image.Resampling.LANCZOS)
    return image,-round(spec['side_overhang']*scale),-round(spec['above_card']*scale)
