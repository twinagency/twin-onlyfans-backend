"""Regression fixtures reproduce attribution failures without private source text."""
import unittest

from onlyfans_runtime.profile_learning.evidence import digest, summary, validate, subject_supported


def candidate(field, value, body, subject, *, creator):
    source = {'text': body, 'direction': 'out' if creator else 'in',
              'at': '2026-10-29', 'digest': digest(body)}
    result = {'facts': [{'field': field, 'value': value, 'quote': body,
                         'source': '1:2', 'subject': subject}]}
    return result, {'1:2': source}


class SubjectAttributionTests(unittest.TestCase):
    def test_fan_mentions_do_not_become_creator_biography(self):
        for field, value, body in [
            ('name', 'Alex', 'Hey Alex. How are you?'),
            ('location', 'London', 'What kept you in London?'),
            ('hobbies', 'basketball', 'Why did you switch to basketball?'),
            ('occupation', 'pilot', 'You are a pilot?'),
        ]:
            with self.subTest(field=field):
                result, sources = candidate(field, value, body, 'fan', creator=True)
                self.assertEqual(validate(result, sources, creator=True), [])

    def test_creator_identity_does_not_become_fan_identity(self):
        result, sources = candidate('name', 'Mia', 'Your name is Mia', 'creator', creator=False)
        self.assertEqual(validate(result, sources), [])

    def test_creator_claim_is_retained_as_unverified(self):
        result, sources = candidate('name', 'Mia', 'I am Mia', 'creator', creator=True)
        facts = validate(result, sources, creator=True)
        self.assertEqual(facts[0]['attribution'], 'account claim')
        self.assertIn('Mia', summary(facts, creator=True, now='2026-10-30'))

    def test_old_mislabeled_rows_are_filtered_when_read(self):
        result, sources = candidate('name', 'Mia', 'I am Mia', 'creator', creator=True)
        fact = validate(result, sources, creator=True)[0]
        fact['subject'] = 'fan'
        self.assertNotIn('Mia', summary([fact], creator=True, now='2026-10-30'))
        self.assertFalse(subject_supported(fact, creator=True))
        fact['subject'] = 'creator'
        self.assertNotIn('Mia', summary([fact], now='2026-10-30'))

    def test_relatives_remain_context_and_not_identity(self):
        for subject in ['relative', 'brother', 'mother']:
            self.assertTrue(subject_supported({'field': 'family', 'subject': subject}))
            self.assertTrue(subject_supported({'field': 'event', 'subject': subject}))
            self.assertFalse(subject_supported({'field': 'occupation', 'subject': subject}))
            self.assertFalse(subject_supported({'field': 'name', 'subject': subject}))

    def test_manual_authority_and_legacy_default_are_preserved(self):
        self.assertTrue(subject_supported({'field': 'name', 'subject': 'creator', 'manual': True}))
        self.assertTrue(subject_supported({'field': 'name', 'subject': 'fan', 'protected': True}, creator=True))
        self.assertTrue(subject_supported({'field': 'name'}))
        self.assertTrue(subject_supported({'field': 'name'}, creator=True))
        self.assertFalse(subject_supported({'field': 'name', 'subject': None}))


if __name__ == '__main__':
    unittest.main()
