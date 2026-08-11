import unittest

from ava.services.mailbox import (MailSummary, clean_subject, sender_name,
                                  spoken_summary)


class MailboxTests(unittest.TestCase):
    def test_sender_name_prefers_the_display_name(self):
        self.assertEqual(sender_name("Jean Dupont <jean@exemple.fr>"), "Jean Dupont")
        self.assertEqual(sender_name("no-reply@github.com"), "no-reply")
        self.assertEqual(sender_name(""), "expéditeur inconnu")

    def test_sender_name_decodes_mime_headers(self):
        encoded = "=?utf-8?B?SsOpcsO0bWU=?= <j@x.fr>"
        self.assertEqual(sender_name(encoded), "Jérôme")

    def test_clean_subject_strips_reply_prefixes(self):
        self.assertEqual(clean_subject("Re: RE: Fwd: facture mars"), "facture mars")
        self.assertEqual(clean_subject(""), "sans objet")

    def test_spoken_summary_zero_unread(self):
        text = spoken_summary(MailSummary(True, unread=0))
        self.assertIn("aucun mail", text)

    def test_spoken_summary_lists_the_previews(self):
        summary = MailSummary(True, unread=5, previews=(
            ("Jean", "facture"), ("Alice", "réunion"), ("Bob", "devis"),
        ))
        text = spoken_summary(summary)
        self.assertIn("5 mails non lus", text)
        self.assertIn("Jean", text)
        self.assertIn("Et encore 2 autres", text)

    def test_spoken_summary_unavailable_is_empty(self):
        self.assertEqual(spoken_summary(MailSummary(False, error="boom")), "")


if __name__ == "__main__":
    unittest.main()
