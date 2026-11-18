"""Preserve supplied star artwork and native layout across offline delivery."""
import base64
import hashlib
from importlib.resources import files
from io import BytesIO
import re
import unittest
from PIL import Image
from setforge.rating_ui import provenance, rating_group_svg
from setforge.showcase import CSS, stars_svg


class RatingUITests(unittest.TestCase):
    def test_every_group_embeds_the_exact_packaged_pixels(self):
        for asset in provenance()['assets']:
            with self.subTest(count=asset['count']):
                svg=stars_svg(asset['count'])
                embedded=base64.b64decode(re.search(r'base64,([^\"]+)',svg)[1])
                self.assertEqual(hashlib.sha256(embedded).hexdigest(),asset['sha256'])
                self.assertEqual(embedded,files('setforge').joinpath('defaults/ui/'+asset['file']).read_bytes())
                image=Image.open(BytesIO(embedded))
                self.assertEqual(image.getchannel('A').getbbox(),tuple(asset['alpha_bounds']))
                self.assertEqual(svg.count('<image '),1)
                self.assertIn(f'aria-label="{asset["count"]} stars"',svg)

    def test_five_star_arc_is_in_the_pixels_not_css_approximation(self):
        image=Image.open(files('setforge').joinpath('defaults/ui/rating-stars-5.png')).getchannel('A')
        # Native five-star silhouette: raised center and progressively lower ends.
        windows=((27,125),(125,222),(222,340),(340,435),(435,532))
        tops=[image.crop((left,0,right,image.height)).getbbox()[1] for left,right in windows]
        self.assertLess(tops[2],tops[1]);self.assertLess(tops[1],tops[0])
        self.assertLess(tops[2],tops[3]);self.assertLess(tops[3],tops[4])
        self.assertNotIn('nth-child',CSS)
        self.assertNotIn('<polygon',rating_group_svg(5))

    def test_invalid_rating_is_not_silently_drawn(self):
        for value in (0,6,True,'5'):
            with self.assertRaises(ValueError):rating_group_svg(value)


if __name__=='__main__':unittest.main()
