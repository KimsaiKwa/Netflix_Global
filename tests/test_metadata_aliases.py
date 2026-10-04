"""Offline anti-mismatch regressions for reviewed Netflix/IMDb identities."""
import copy
import unittest
from unittest.mock import patch

import enrich_metadata as m


class VerifiedIdentityTests(unittest.TestCase):
    def setUp(self):
        self.title = 'Muroi Shinji: Stay Alive'
        self.identity = m.verified_identities()[m.cache_key('movie', self.title)]
        self.item = {
            'id': self.identity['imdb_id'],
            'l': self.identity['imdb_titles'][0],
            'y': self.identity['year'], 'q': 'feature',
            'i': {'imageUrl': 'https://m.media-amazon.com/images/M/verified.jpg'},
        }

    def resolve(self, item):
        with patch.object(m, 'request_json', return_value={'d': [item]}):
            return m.resolve_fresh_meta(self.title, 'movie', country_hints={'JP'})

    def test_reviewed_alias_requires_exact_id_title_year_and_type(self):
        result = self.resolve(self.item)
        self.assertEqual(result['imdb_id'], 'tt33452952')
        self.assertEqual(result['year'], '2024')
        self.assertTrue(result['poster_candidates'])
        self.assertEqual(result['cn_title'], '')
        self.assertIn('exact_imdb_id_year', result['evidence'])

    def test_rejects_different_id_even_when_title_and_year_match(self):
        item = copy.deepcopy(self.item)
        item['id'] = 'tt33184066'
        self.assertFalse(self.resolve(item)['poster_candidates'])

    def test_rejects_different_sequel_even_when_id_matches(self):
        item = copy.deepcopy(self.item)
        item['l'] = 'Muroi Shinji: Yaburezaru Mono'
        self.assertFalse(self.resolve(item)['poster_candidates'])

    def test_rejects_adjacent_year_for_verified_identity(self):
        item = copy.deepcopy(self.item)
        item['y'] += 1
        self.assertFalse(self.resolve(item)['poster_candidates'])

    def test_rejects_television_series_for_film(self):
        item = copy.deepcopy(self.item)
        item['q'] = 'TV series'
        self.assertFalse(self.resolve(item)['poster_candidates'])

    def test_verified_identity_still_enriches_chinese_title_only_from_douban(self):
        candidate = {'id': '12345678', 'title': '室井慎次 继续生活之人',
                     'sub_title': self.identity['imdb_titles'][0],
                     'year': '2024', 'type': 'movie',
                     'img': 'https://img3.doubanio.com/view/photo/s_ratio_poster/public/p1.jpg'}
        with patch.object(m, 'request_json', return_value={'d': [self.item]}), \
                patch.object(m, 'search_douban', return_value=[candidate]):
            result = m.resolve_fresh_meta(self.title, 'movie')
        self.assertEqual(result['cn_title'], candidate['title'])
        self.assertEqual(result['douban_id'], candidate['id'])
        self.assertEqual(result['imdb_id'], self.identity['imdb_id'])
        self.assertEqual(result['poster_candidates'][0], candidate['img'])

    def test_verified_identity_rejects_douban_wrong_year_type_or_incomplete_title(self):
        candidate = {'id': '12345678', 'title': '中文片名',
                     'sub_title': self.identity['imdb_titles'][0],
                     'year': '2024', 'type': 'movie'}
        for changes in [{'year': '2006'}, {'year': ''}, {'type': 'tv'},
                        {'sub_title': 'Muroi Shinji'}]:
            with self.subTest(changes=changes), \
                    patch.object(m, 'request_json', return_value={'d': [self.item]}), \
                    patch.object(m, 'search_douban', return_value=[{**candidate, **changes}]):
                result = m.resolve_fresh_meta(self.title, 'movie')
            self.assertEqual(result['cn_title'], '')
            self.assertEqual(result['douban_id'], '')
            self.assertEqual(result['imdb_id'], self.identity['imdb_id'])

    def test_provider_failure_does_not_fall_back_to_namesake(self):
        with patch.object(m, 'search_imdb', side_effect=RuntimeError('offline')), \
                patch.object(m, 'search_justwatch_multi') as jw:
            result = m.resolve_fresh_meta(self.title, 'movie')
        self.assertFalse(result['poster_candidates'])
        jw.assert_not_called()

    def test_identity_change_invalidates_fresh_cache_without_merging_old_poster(self):
        key = m.cache_key('movie', self.title)
        old = m.empty_meta()
        old.update(imdb_id='tt0441796', year='2006',
                   poster_candidates=['https://example.com/wrong-film.jpg'])
        cache = {'items': {key: {'saved_at': m.now_ts(), 'match_version': m.MATCH_VERSION,
                                 'identity_revision': 'previous-identity', 'meta': old}}}
        with patch.object(m, 'request_json', return_value={'d': [self.item]}):
            meta, cached = m.get_stable_meta(cache, 'movie', self.title, '', {'JP'})
        self.assertFalse(cached)
        self.assertEqual(meta['imdb_id'], self.identity['imdb_id'])
        self.assertNotIn('https://example.com/wrong-film.jpg', meta['poster_candidates'])
        self.assertEqual(cache['items'][key]['identity_revision'], m.identity_revision(self.title, 'movie'))

    def test_current_verified_cache_survives_temporary_network_failure(self):
        key = m.cache_key('movie', self.title)
        old = self.resolve(self.item)
        cache = {'items': {key: {'saved_at': 1, 'match_version': m.MATCH_VERSION,
                                 'identity_revision': m.identity_revision(self.title, 'movie'), 'meta': old}}}
        with patch.object(m, 'search_imdb', side_effect=RuntimeError('offline')):
            meta, cached = m.get_stable_meta(cache, 'movie', self.title, '', {'JP'})
        self.assertFalse(cached)
        self.assertEqual(meta['poster_candidates'], old['poster_candidates'])

    def test_definitive_identity_rejection_discards_expired_cached_poster(self):
        key = m.cache_key('movie', self.title)
        old = self.resolve(self.item)
        cache = {'items': {key: {'saved_at': 1, 'match_version': m.MATCH_VERSION,
                                 'identity_revision': m.identity_revision(self.title, 'movie'), 'meta': old}}}
        contradicting = {**self.item, 'l': 'Muroi Shinji: Yaburezaru Mono'}
        with patch.object(m, 'request_json', return_value={'d': [contradicting]}):
            meta, cached = m.get_stable_meta(cache, 'movie', self.title, '', {'JP'})
        self.assertFalse(cached)
        self.assertFalse(meta['poster_candidates'])
        self.assertFalse(cache['items'][key]['meta']['imdb_id'])

    def test_missing_or_malformed_provider_response_does_not_erase_cached_identity(self):
        key = m.cache_key('movie', self.title)
        old = self.resolve(self.item)
        for response in [{}, {'d': []}, {'d': 'truncated'},
                         {'d': [{**self.item, 'id': 'tt9999999'}]},
                         {'d': [{**self.item, 'y': None}]},
                         {'d': [{**self.item, 'q': ''}]},
                         {'d': [{**self.item, 'l': ''}]}]:
            cache = {'items': {key: {'saved_at': 1, 'match_version': m.MATCH_VERSION,
                                     'identity_revision': m.identity_revision(self.title, 'movie'), 'meta': old}}}
            with self.subTest(response=response), \
                    patch.object(m, 'request_json', return_value=response):
                meta, cached = m.get_stable_meta(cache, 'movie', self.title, '', {'JP'})
            self.assertFalse(cached)
            self.assertEqual(meta['poster_candidates'], old['poster_candidates'])

    def test_documentation_changes_do_not_expire_identity_but_alias_changes_do(self):
        key = m.cache_key('movie', self.title)
        original = m.identity_revision(self.title, 'movie')
        edited = {**self.identity, 'note': 'New explanation', 'sources': ['https://example.com/']}
        with patch.object(m, 'verified_identities', return_value={key: edited}):
            self.assertEqual(m.identity_revision(self.title, 'movie'), original)
        edited['imdb_titles'] = self.identity['imdb_titles'] + ['Reviewed additional title']
        with patch.object(m, 'verified_identities', return_value={key: edited}):
            self.assertNotEqual(m.identity_revision(self.title, 'movie'), original)

    def test_generic_matching_does_not_gain_alias_or_subtitle_fuzziness(self):
        for candidate, target in [
            ('Muroi Shinji', 'Muroi Shinji: Stay Alive'),
            ('Muroi Shinji: Not Defeated', 'Muroi Shinji: Stay Alive'),
            ('Bayside Shakedown 3', 'Bayside Shakedown 4: The Final'),
            ('Golden Kamuy', 'Golden Kamuy -The Abashiri Prison Raid-'),
            ('Stella', 'Stella Next to Me'),
            ('The Killer', 'Ichi the Killer 4K'),
        ]:
            with self.subTest(candidate=candidate):
                self.assertEqual(m.strict_title_score(candidate, target), 0)
        self.assertGreater(m.strict_title_score('Ichi the Killer', 'Ichi the Killer 4K'), 0)

    def test_generic_id_hint_cannot_select_another_id(self):
        with patch.object(m, 'request_json', return_value={'d': [self.item]}):
            self.assertIsNone(m.search_imdb(self.item['l'], 'movie', '2024', 'tt9999999'))

    def test_manifest_has_no_duplicate_keys_or_fixed_posters(self):
        identities = m.verified_identities()
        self.assertGreaterEqual(len(identities), 5)
        for entry in identities.values():
            self.assertNotIn('poster', entry)
            self.assertTrue(any('netflix.com/' in url for url in entry['sources']))
            self.assertTrue(any('imdb.com/' in url for url in entry['sources']))


if __name__ == '__main__':
    unittest.main()
