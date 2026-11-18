"""Offline pixel-metric preflight tests; no new art, files or title mutations."""
import copy
import json
import math
from pathlib import Path
import string
import tempfile
import unittest

from setforge.nameplate import DEFAULT_POLICY, measure_text, preflight


class NameplateTests(unittest.TestCase):
    def plan(self, *titles):
        return {'variants': [{'id': 'harvest', 'title': 'Harvest Grill',
                              'cards': [{'id': 'c' + str(i), 'title': title}
                                        for i, title in enumerate(titles)]}]}

    def metrics(self):
        return {'advance': {**dict.fromkeys(string.ascii_letters, 6), ' ': 3, 'W': 14, 'I': 4},
                'baseline_height': 18, 'tracking': 0}

    def cards(self, result):
        return [x for x in result['labels'] if x['kind'] == 'card']

    def test_same_character_count_has_different_measured_fit(self):
        result = preflight(self.plan('WWWW', 'IIII'),
                           {**DEFAULT_POLICY, 'available_width': 40, 'metrics': self.metrics()})
        wide, narrow = self.cards(result)
        self.assertEqual(wide['measured_width'], 56)
        self.assertEqual(narrow['measured_width'], 16)
        self.assertEqual(wide['verdict'], 'fail')
        self.assertEqual(narrow['verdict'], 'pass')

    def test_unrounded_natural_width_boundary_without_fit_hacks(self):
        for width, verdict in [(160, 'pass'), (160.000000001, 'fail')]:
            with self.subTest(width=width):
                result = preflight(self.plan('Boundary'), measurefn=lambda text, height: {'width': width, 'ink_height': height})
                card = self.cards(result)[0]
                self.assertEqual(card['verdict'], verdict)
                self.assertEqual(card['available_width'], 160)
                self.assertEqual(card['target_height'], 18)
                self.assertEqual(card['measured_width'], width)

    def test_fixed_card_and_banner_dimensions_are_explicit(self):
        calls = []
        def measure(text, height):
            calls.append((text, height))
            return {'text': text, 'width': 100, 'ink_height': height, 'fit_to_width': False,
                    'source': 'TEST screenshot atlas', 'font_file_recovered': False}
        result = preflight(self.plan('Proud Platter', 'Warm Blanket'), measurefn=measure)
        self.assertEqual(calls, [('Harvest Grill', 31), ('Proud Platter', 18), ('Warm Blanket', 18)])
        self.assertEqual(result['labels'][0]['available_width'], 850)
        self.assertEqual([c['available_width'] for c in self.cards(result)], [160, 160])
        self.assertEqual(result['verdict'], 'pass')
        self.assertEqual(result['labels'][0]['measurement_source'], 'TEST screenshot atlas')
        self.assertFalse(result['labels'][0]['font_file_recovered'])

    def test_one_argument_measurer_remains_supported(self):
        self.assertEqual(preflight(self.plan('Corn'), measurefn=lambda text: 80)['verdict'], 'pass')

    def test_baseline_metrics_scale_uniformly_and_count_spaces_tracking(self):
        result = measure_text('W I', {'advance': {'W': 28, ' ': 12, 'I': 8},
                                     'baseline_height': 36, 'tracking': 2}, target_height=18)
        self.assertEqual(result['width'], 26)
        self.assertEqual(result['ink_height'], 18)
        self.assertTrue(result['complete'])
        self.assertFalse(result['fit_to_width'])

    def test_unknown_glyph_fallback_is_diagnostic_and_never_a_pass(self):
        metrics = self.metrics(); del metrics['advance']['X']
        measured = measure_text('X', metrics, target_height=18)
        self.assertEqual(measured['width'], 18)
        self.assertFalse(measured['complete'])
        card = self.cards(preflight(self.plan('X'), {'metrics': metrics}))[0]
        self.assertEqual(card['unsupported_characters'], ['X'])
        self.assertEqual(card['verdict'], 'fail')
        self.assertIn('unsupported_glyphs_not_certified', card['reasons'])

    def test_renderer_missing_glyph_and_unavailable_metrics_fail_closed(self):
        def missing(text, height):
            raise ValueError('No source bitmap for digit')
        for measure in (missing, None):
            with self.subTest(measure=measure):
                card = self.cards(preflight(self.plan('Corn'), measurefn=measure))[0]
                self.assertEqual(card['verdict'], 'fail')
                self.assertIn('measurement_unavailable', card['reasons'])

    def test_rendered_shrink_ellipsis_crop_or_wider_lane_cannot_certify(self):
        changes = [{'fit_to_width': True}, {'auto_shrink': True}, {'font_shrunk': True},
                   {'ellipsized': True}, {'cropped': True}, {'widen_to_fit': True},
                   {'ink_height': 17}, {'text': 'C...'}, {'available_width': 200}]
        for change in changes:
            with self.subTest(change=change):
                card = self.cards(preflight(self.plan('Corn'), measurefn=lambda text, height: {'width': 40, **change}))[0]
                self.assertEqual(card['verdict'], 'fail')
        with self.assertRaises(ValueError):
            preflight(self.plan('Corn'), {'auto_shrink': True}, lambda text: 40)

    def test_bad_measurements_cannot_certify(self):
        for width in (0, -1, math.nan, math.inf, True):
            with self.subTest(width=width):
                card = self.cards(preflight(self.plan('Corn'), measurefn=lambda text: width))[0]
                self.assertEqual(card['verdict'], 'fail')
                self.assertIn('measurement_unavailable', card['reasons'])

    def test_no_character_count_limit_replaces_pixel_fit(self):
        title = 'I' * 40
        card = self.cards(preflight(self.plan(title), {'metrics': self.metrics()}))[0]
        self.assertEqual(card['measured_width'], 160)
        self.assertEqual(card['verdict'], 'pass')

    def test_flagged_current_labels_stay_pending_and_plan_file_unchanged(self):
        plan = self.plan('Proud Platter', 'Warm Blanket')
        original = copy.deepcopy(plan)
        policy = {'producer_flagged_labels': ['harvest/c0', 'harvest/c1']}
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'display-titles.json'
            filename.write_text(json.dumps(plan), encoding='utf8')
            before = filename.read_bytes()
            result = preflight(plan, policy, lambda text: 40)
            self.assertEqual(filename.read_bytes(), before)
        self.assertEqual(plan, original)
        self.assertTrue(result['read_only'])
        self.assertEqual([c['text'] for c in self.cards(result)], ['Proud Platter', 'Warm Blanket'])
        for card in self.cards(result):
            self.assertIn('producer_flagged_title_pending', card['reasons'])
            self.assertEqual(card['verdict'], 'fail')
            self.assertEqual(card['next_action'], 'rewrite_title_then_replace_card_concept')

    def test_non_english_script_or_multiline_title_cannot_pass(self):
        for title in ('Корзина', 'Corn\nPlate', ' Corn', ''):
            with self.subTest(title=title):
                self.assertEqual(self.cards(preflight(self.plan(title), measurefn=lambda text: 40))[0]['verdict'], 'fail')


if __name__ == '__main__':
    unittest.main()
