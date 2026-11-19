"""Planning regressions from repeated circus poses; fixtures contain no art."""

import copy
import unittest

from setforge.character_staging import check_staging, policy, staging_schema
from setforge.schemas import validate


def staging(**changes):
    return {'character_id': 'pip', 'stance': 'standing', 'gesture': 'arms_spread',
            'action': 'Welcomes the audience with both arms open.',
            'camera_azimuth': 'front', 'camera_elevation': 'eye_level', **changes}


def plan():
    return {'variants': [{'id': 'big-top', 'entities': [
        {'id': 'pip', 'scope': 'set'}], 'cards': [
            {'id': 'welcome', 'key_character_count': 1, 'character_staging': [staging()]},
            {'id': 'bow', 'key_character_count': 1, 'character_staging': [staging(
                stance='bending', gesture='hands_together',
                action='Bows deeply toward the audience.',
                camera_azimuth='right_three_quarter', camera_elevation='high')]},
            {'id': 'drum', 'key_character_count': 0, 'character_staging': []},
        ]}]}


class CharacterStagingTests(unittest.TestCase):
    def test_distinct_pose_and_camera_pass_with_prop_cards(self):
        self.assertTrue(check_staging(plan()))
        self.assertEqual(policy()['version'], 1)
        validate([staging()], staging_schema())

    def test_new_action_or_title_does_not_hide_same_pose(self):
        data = plan()
        second = data['variants'][0]['cards'][1]
        second['character_staging'][0] = staging(
            action='Carries a totally different title and prop.',
            camera_azimuth='right_profile')
        with self.assertRaisesRegex(ValueError, 'different pose'):
            check_staging(data)

    def test_changed_pose_still_requires_changed_camera(self):
        data = plan()
        data['variants'][0]['cards'][1]['character_staging'][0] = staging(stance='kneeling')
        with self.assertRaisesRegex(ValueError, 'camera angle'):
            check_staging(data)

    def test_different_character_allowed_in_same_camera(self):
        data = plan()
        data['variants'][0]['cards'][1]['character_staging'][0] = staging(character_id='lulu')
        self.assertTrue(check_staging(data))

    def test_all_appearances_not_only_adjacent_cards_are_checked(self):
        data = plan()
        cards = data['variants'][0]['cards']
        cards[1], cards[2] = cards[2], cards[1]
        cards[2]['character_staging'][0] = staging()
        with self.assertRaisesRegex(ValueError, 'big-top/welcome, big-top/bow'):
            check_staging(data)

    def test_count_missing_tags_and_duplicate_ids_fail_before_art(self):
        for mode in ('missing', 'count', 'duplicate'):
            with self.subTest(mode=mode):
                data = plan()
                card = data['variants'][0]['cards'][0]
                if mode == 'missing':
                    card.pop('character_staging')
                elif mode == 'count':
                    card['key_character_count'] = 2
                else:
                    card['key_character_count'] = 2
                    card['character_staging'].append(staging())
                with self.assertRaises(ValueError):
                    check_staging(data)

    def test_collection_identity_compares_across_sets_local_names_do_not(self):
        data = plan()
        other = copy.deepcopy(data['variants'][0])
        other['id'] = 'night-show'
        data['variants'].append(other)
        self.assertTrue(check_staging(data))
        for variant in data['variants']:
            variant['entities'][0]['scope'] = 'collection'
        with self.assertRaisesRegex(ValueError, 'Repeated character pip'):
            check_staging(data)

    def test_free_prose_cannot_replace_controlled_pose_tags(self):
        data = plan()
        data['variants'][0]['cards'][0]['character_staging'][0]['stance'] = 'standing-but-new-name'
        with self.assertRaises(ValueError):
            check_staging(data)

    def test_collection_definition_once_and_pinned_canon_keep_global_identity(self):
        for pinned in (False, True):
            with self.subTest(pinned=pinned):
                data = plan()
                other = copy.deepcopy(data['variants'][0])
                other['id'] = 'night-show'
                other['entities'] = []
                data['variants'].append(other)
                data['variants'][0]['entities'][0]['scope'] = 'collection'
                canon = []
                if pinned:
                    canon = data['variants'][0]['entities']
                    data['variants'][0]['entities'] = []
                with self.assertRaisesRegex(ValueError, 'Repeated character pip'):
                    check_staging(data, canon)


if __name__ == '__main__':
    unittest.main()
