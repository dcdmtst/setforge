"""Licensed fonts stay local; labels and preflight share exact rendered pixels."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import ImageFont
from setforge.cli import main
from setforge.delivery import export_bundle
from setforge.io import read, write
from setforge.project import init_project, snapshot
from setforge.stylepack import configure as configure_style
from setforge.typography import Lettering, NEUTRAL, configure, for_run, project_profile
import test_public_distribution as distribution


class TypographyTests(unittest.TestCase):
    setUp=distribution.DistributionTests.setUp
    completed_fixture=distribution.DistributionTests.completed_fixture
    pack=distribution.DistributionTests.pack

    def font_file(self):
        # Test-time copy of Pillow's own bundled fixture, never a vendored font.
        path=self.root/'fixture-font.ttf'
        path.write_bytes(ImageFont.load_default(size=64).path.getvalue())
        return path

    def test_cli_configures_real_local_font_and_reports_actual_identity(self):
        path=self.font_file();output=io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['font-configure','--project',str(self.project),'--font',str(path)]),0)
        result=json.loads(output.getvalue());profile=project_profile(self.project)
        self.assertEqual(profile['family'],ImageFont.truetype(str(path),64).getname()[0])
        self.assertEqual(result['typography']['sha256'],profile['sha256'])
        self.assertNotIn('path',result['typography'])
        self.assertFalse(result['font_software_copied'])
        self.assertFalse(list(self.project.rglob('*.ttf')))

    def test_snapshot_pins_font_while_other_project_and_old_runs_keep_theirs(self):
        configure_style(self.project,self.pack());configure(self.project,self.font_file())
        run=self.root/'run';(run/'inputs').mkdir(parents=True)
        snapshot(self.project,run);selected=for_run(run,frozen=True)
        configure(self.project,neutral=True)
        self.assertEqual(project_profile(self.project),NEUTRAL)
        self.assertEqual(selected.public_profile(),for_run(run,frozen=True).public_profile())
        self.assertEqual(for_run(self.root/'historical').public_profile(),NEUTRAL)
        self.assertFalse(list((run/'inputs').rglob('*.ttf')))

    def test_same_font_drives_plan_pixel_fit_and_svg_without_word_shrinking(self):
        configure(self.project,self.font_file());lettering=Lettering(project_profile(self.project))
        plan={'variants':[{'id':'v','title':'Font Check','cards':[{'id':'c','title':'W'*40}]}]}
        path=self.root/'plan.json';write(path,plan);output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(['nameplate-check','--plan',str(path),'--project',str(self.project)])
        result=json.loads(output.getvalue());card=result['labels'][1]
        self.assertEqual(code,2);self.assertGreater(card['measured_width'],160)
        self.assertEqual(card['measured_width'],lettering.compose_label('W'*40,18).width)
        self.assertEqual(result['typography'],lettering.public_profile())
        self.assertNotEqual(lettering.compose_label('Dice').tobytes(),lettering.compose_label('DICE').tobytes())
        self.assertEqual(lettering.compose_label('H').getbbox()[3]-lettering.compose_label('H').getbbox()[1],
                         lettering.compose_label('HH').getbbox()[3]-lettering.compose_label('HH').getbbox()[1])

    def test_missing_or_changed_font_never_silently_falls_back(self):
        path=self.font_file();configure(self.project,path);profile=project_profile(self.project)
        relocated=self.root/'other.ttf';relocated.write_bytes(path.read_bytes());path.unlink()
        with self.assertRaisesRegex(ValueError,'unavailable'):Lettering(profile)
        with patch.dict(os.environ,{'SETFORGE_FONT_PATH':str(relocated)}):
            self.assertEqual(Lettering(profile).public_profile()['sha256'],profile['sha256'])
            relocated.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'changed'):Lettering(profile)

    def test_existing_run_override_is_presentation_only_and_zip_embeds_no_font(self):
        run=self.completed_fixture();state=(run/'state.json').read_bytes()
        configure(run,self.font_file(),run=True);lettering=for_run(run)
        self.assertEqual(for_run(run,frozen=True).public_profile(),NEUTRAL)
        receipt=export_bundle(run)
        self.assertEqual((run/'state.json').read_bytes(),state)
        with zipfile.ZipFile(receipt['path']) as bundle:
            self.assertFalse(any(n.lower().endswith(('.ttf','.otf','.woff','.woff2')) for n in bundle.namelist()))
            self.assertNotIn('presentation-typography.json',bundle.namelist())
            manifest=json.loads(bundle.read('result-manifest.json'))
            self.assertEqual(manifest['typography'],lettering.public_profile())
            page=bundle.read('report.html').decode()
            title=read(run/'state.json')['plan']['variants'][0]['cards'][0]['title']
            self.assertIn(lettering.label_svg(title,18),page)
            self.assertNotIn(str(self.root),page)
            self.assertNotIn('fixture-font.ttf',page)
            self.assertEqual(json.loads(bundle.read('nameplate-report.json'))['typography'],lettering.public_profile())


if __name__=='__main__':unittest.main()
