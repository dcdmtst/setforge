"""Font choice happens before work; OS installation is explicit and test-isolated."""
import contextlib
import http.client
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from setforge import font_selection as fonts
from setforge.cli import main
from setforge.io import read
from setforge.stylepack import configure as configure_style
from setforge.typography import NEUTRAL, Lettering, configure, for_run
from setforge.ui import create_order, make_server
import test_typography as typography_tests
import test_public_distribution as distribution_tests


class FontSelectionTests(unittest.TestCase):
    setUp=distribution_tests.DistributionTests.setUp
    pack=distribution_tests.DistributionTests.pack
    font_file=typography_tests.TypographyTests.font_file

    def test_missing_choice_precedes_run_and_pins_default_for_only_that_order(self):
        configure_style(self.project,self.pack());runs=self.root/'orders'
        with patch.object(fonts,'installed_fonts',return_value=[]):
            with self.assertRaises(fonts.FontChoiceRequired):create_order(self.project,runs,{'theme':'TEST'})
            self.assertFalse(runs.exists())
            order=create_order(self.project,runs,{'theme':'TEST','font_decision':'default'})
        profile=read(order.run/'inputs/typography.json')
        self.assertEqual(profile['kind'],'neutral');self.assertEqual(profile['requested'],fonts.DEFAULT_FONT)
        self.assertEqual(read(self.project/'project.json')['font'],fonts.DEFAULT_FONT)
        self.assertNotIn('font_decision',read(order.run/'inputs/brief.json'))
        configure(self.project,self.font_file())
        self.assertEqual(for_run(order.run).profile,profile)

    def test_named_system_font_uses_real_metadata_and_explicit_default_is_sticky(self):
        path=self.font_file();family,style=fonts.ImageFont.truetype(str(path),64).getname()
        item={'name':family+' '+style,'family':family,'style':style,'path':str(path)}
        with patch.object(fonts,'installed_fonts',return_value=[item]):
            profile=fonts.selection(self.project,item['name'])
            self.assertEqual(Lettering(profile).font.getname(),(family,style))
            self.assertEqual(fonts.selection(self.project,item['name'],'default')['kind'],'neutral')
        self.assertNotIn('path',Lettering(profile).public_profile())

    def test_cli_ci_defaults_without_prompt_and_install_choice_creates_no_run(self):
        configure_style(self.project,self.pack());run=self.root/'ci-run'
        with patch.object(fonts,'installed_fonts',return_value=[]),patch('sys.stdin.isatty',return_value=False),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            result=main(['run','--project',str(self.project),'--run',str(run),'--theme','TEST','--font','Missing Test Font','--missing-font','install'])
            self.assertEqual(result,4);self.assertFalse(run.exists())
            result=main(['run','--project',str(self.project),'--run',str(run),'--theme','TEST','--font','Missing Test Font'])
            self.assertIn(result,(0,3))
        self.assertEqual(read(run/'inputs/typography.json')['kind'],'neutral')

    def test_os_instructions_and_official_attempt_never_claim_download_is_install(self):
        for system,phrase in [('Windows','правой кнопкой'),('Darwin','Font Book'),('Linux','~/.local/share/fonts')]:
            help=fonts.installation_help(fonts.DEFAULT_FONT,system)
            self.assertIn(phrase,' '.join(help['steps']))
        with patch.object(fonts,'installed_fonts',return_value=[]),patch.object(fonts.platform,'system',return_value='Linux'),patch.object(fonts.webbrowser,'open') as browser:
            result=fonts.try_install(self.project,fonts.DEFAULT_FONT)
        browser.assert_called_once_with(fonts.ADOBE_URL)
        self.assertEqual(result['status'],'manual_step_required')

    def test_closed_interactive_input_falls_back_instead_of_crashing(self):
        configure_style(self.project,self.pack());run=self.root/'closed-input'
        with patch.object(fonts,'installed_fonts',return_value=[]),patch('sys.stdin.isatty',return_value=True),patch('builtins.input',side_effect=EOFError),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            code=main(['run','--project',str(self.project),'--run',str(run),'--theme','TEST'])
        self.assertIn(code,(0,3));self.assertEqual(read(run/'inputs/typography.json')['kind'],'neutral')

    def test_failed_automatic_install_keeps_manual_and_default_paths_available(self):
        with patch.object(fonts,'install_local_file',side_effect=OSError('Access denied')):
            result=fonts.try_install(self.project,fonts.DEFAULT_FONT,source='fixture.otf')
        self.assertEqual(result['status'],'manual_step_required')
        self.assertIn('Aileron',result['message']);self.assertIn('steps',result['instructions'])

    def test_automatic_local_install_is_scoped_to_user_fonts_and_validates_identity(self):
        source=self.font_file();family,style=fonts.ImageFont.truetype(str(source),64).getname()
        with patch.object(fonts.Path,'home',return_value=self.root),patch.object(fonts.platform,'system',return_value='Linux'),patch.object(fonts.shutil,'which',return_value=None),patch.object(fonts,'installed_fonts',return_value=[]):
            with self.assertRaisesRegex(ValueError,'семейству'):fonts.install_local_file(source,'Wrong Family')
            result=fonts.install_local_file(source,family+' '+style)
        files=list((self.root/'.local/share/fonts').glob('*.ttf'))
        self.assertEqual(result['status'],'installed');self.assertEqual(len(files),1)
        self.assertEqual(files[0].read_bytes(),source.read_bytes())
        self.assertFalse(list(self.project.rglob('*.ttf')))

    def test_http_dialog_contract_and_install_endpoint_origin_guard(self):
        configure_style(self.project,self.pack());runs=self.root/'orders'
        server=make_server(self.project,runs,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def request(path,value,origin=None):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            headers={'Content-Type':'application/json'}
            if origin:headers['Origin']=origin
            connection.request('POST',path,json.dumps(value),headers)
            response=connection.getresponse();data=json.loads(response.read());status=response.status;connection.close();return status,data
        with patch.object(fonts,'installed_fonts',return_value=[]),patch('setforge.ui.advance_order'):
            status,data=request('/api/runs',{'theme':'TEST','font':'Missing Test Font'})
            self.assertEqual(status,409);self.assertEqual(data['code'],'font_choice_required')
            self.assertFalse(list(runs.glob('run-*')))
            status,data=request('/api/fonts/check',{'font':'Missing Test Font'})
            self.assertEqual(status,200);self.assertFalse(data['available']);self.assertIn('os',data['instructions'])
            self.assertFalse(data['pending'])
            with patch.object(fonts,'try_install',return_value={'status':'manual_step_required'}) as installer:
                status,_=request('/api/fonts/install',{'font':fonts.DEFAULT_FONT},'https://unrelated.example')
                self.assertEqual(status,403);installer.assert_not_called()
                status,_=request('/api/fonts/install',{'font':fonts.DEFAULT_FONT})
                self.assertEqual(status,200);installer.assert_called_once()
            status,_=request('/api/runs',{'theme':'TEST','font_decision':'default'})
            self.assertEqual(status,201)
