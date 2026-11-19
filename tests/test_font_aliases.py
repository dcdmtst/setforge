"""Published SFNT names must resolve even when FreeType returns numeric styles."""
import struct
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from setforge.font_selection import _font_aliases, find_profile

class FontAliasTests(unittest.TestCase):
    def test_published_full_and_postscript_names_are_decoded(self):
        full='Lithos Pro Black'.encode('utf-16-be');post='LithosPro-Black'.encode('utf-16-be')
        names=struct.pack('>HHH',0,2,30)+struct.pack('>HHHHHH',3,1,1033,4,len(full),0)+struct.pack('>HHHHHH',3,1,1033,6,len(post),len(full))+full+post
        data=b'OTTO'+struct.pack('>HHHH',1,16,0,0)+struct.pack('>4sIII',b'name',0,28,len(names))+names
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'15680';path.write_bytes(data)
            self.assertEqual(_font_aliases(path),['Lithos Pro Black','LithosPro-Black'])
    def test_numeric_style_is_not_mistaken_for_a_missing_installed_face(self):
        item={'name':'Lithos Pro 850','family':'Lithos Pro','style':'850','path':'licensed-local-file','aliases':['Lithos Pro Black','LithosPro-Black']}
        with patch('setforge.font_selection.project_profile',return_value={'kind':'neutral'}),patch('setforge.font_selection.installed_fonts',return_value=[item]),patch('setforge.font_selection._profile',return_value={'requested':'Lithos Pro Black'}) as profile:
            self.assertEqual(find_profile('.', 'Lithos Pro Black')['requested'],'Lithos Pro Black')
            profile.assert_called_once_with('licensed-local-file','Lithos Pro Black')
