"""Unit checks for the Jira client, plus fetches against a fake Jira server."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_jira  # noqa: E402
from agent_runner.config import ConfigError, JiraSettings  # noqa: E402
from agent_runner.jira import (  # noqa: E402
    JiraError,
    browse_url,
    extract_acceptance_from_description,
    extract_messages,
    fetch_tickets,
    find_acceptance_field_id,
    normalize_keys,
    plain_text,
    safe_filename,
)


class HelperTests(unittest.TestCase):
    def test_browse_url_from_rest_url(self):
        self.assertEqual(browse_url('https://jira.example.com/rest/api/2/', 'PROJ-123'),
                         'https://jira.example.com/browse/PROJ-123')

    def test_plain_text_handles_jira_doc_content(self):
        value = {'type': 'doc', 'content': [{'content': [{'text': 'First paragraph'}]},
                                            {'content': [{'text': 'Second paragraph'}]}]}
        self.assertEqual(plain_text(value), 'First paragraph\nSecond paragraph')

    def test_extract_acceptance_from_heading(self):
        description = 'Problem statement\n\nAcceptance Criteria:\n- One\n- Two\n\nNotes:\nOther text\n'
        self.assertEqual(extract_acceptance_from_description(description), '- One\n- Two')

    def test_find_acceptance_field_id_prefers_exact_name(self):
        fields = [{'id': 'customfield_1', 'name': 'Team Acceptance'},
                  {'id': 'customfield_2', 'name': 'Acceptance Criteria'}]
        self.assertEqual(find_acceptance_field_id(fields), 'customfield_2')

    def test_normalize_keys_splits_dedupes_and_strips_hash(self):
        self.assertEqual(normalize_keys(['proj-1, PROJ-2\n#PROJ-3', 'PROJ-1']), ['PROJ-1', 'PROJ-2', 'PROJ-3'])

    def test_extract_messages_uses_issue_comment_fallback(self):
        messages = extract_messages({'comment': {'comments': [
            {'author': {'displayName': 'Jane User'}, 'created': '2026-08-05T10:00:00.000-0500',
             'updated': '2026-08-05T10:01:00.000-0500', 'body': 'See the attached screenshot.'}]}}, [])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].author, 'Jane User')
        self.assertEqual(messages[0].body, 'See the attached screenshot.')

    def test_safe_filename_is_portable_and_stays_inside_ticket_directory(self):
        self.assertEqual(safe_filename('folder/name: image.png'), 'folder_name_ image.png')
        self.assertEqual(safe_filename('a<b>c|d?e*f"g\\h.txt'), 'a_b_c_d_e_f_g_h.txt')
        self.assertEqual(safe_filename('notes. '), 'notes')
        self.assertEqual(safe_filename('CON.txt'), '_CON.txt')
        self.assertEqual(safe_filename('..'), 'attachment')
        self.assertEqual(safe_filename(''), 'attachment')


class TokenTests(unittest.TestCase):
    def test_env_var_wins_then_token_file(self):
        with tempfile.TemporaryDirectory() as temp:
            token_file = Path(temp) / 'jira.token'
            token_file.write_text('from-file\n')
            settings = JiraSettings(url='x', token_env='TEST_RUNNER_TOKEN', token_file=str(token_file))
            os.environ.pop('TEST_RUNNER_TOKEN', None)
            self.assertEqual(settings.token(), 'from-file')
            os.environ['TEST_RUNNER_TOKEN'] = 'from-env'
            self.addCleanup(os.environ.pop, 'TEST_RUNNER_TOKEN', None)
            self.assertEqual(settings.token(), 'from-env')

    def test_missing_token_names_sources(self):
        os.environ.pop('TEST_RUNNER_TOKEN', None)
        with self.assertRaisesRegex(ConfigError, 'TEST_RUNNER_TOKEN'):
            JiraSettings(url='x', token_env='TEST_RUNNER_TOKEN').token()


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.server = fake_jira.start(self)
        self.server.issues['PROJ-100'] = {
            'summary': 'Add numbers', 'description': 'Integers only.', 'acceptance': 'Sums are correct.',
            'comments': ['Comment 1', 'Comment 2'], 'attachments': {'spec.md': (b'spec', 4)}}
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        os.environ['TEST_RUNNER_TOKEN'] = fake_jira.TOKEN
        self.addCleanup(os.environ.pop, 'TEST_RUNNER_TOKEN', None)
        self.settings = JiraSettings(url=self.server.url, token_env='TEST_RUNNER_TOKEN')

    def test_fetches_ticket_comments_and_attachments(self):
        [ticket] = fetch_tickets(self.settings, ['proj-100'], str(self.dir / 'attachments'))
        self.assertEqual(ticket['key'], 'PROJ-100')
        self.assertEqual(ticket['title'], 'Add numbers')
        self.assertEqual(ticket['acceptance_criteria'], 'Sums are correct.')
        self.assertIn('customfield_9', self.server.requested_fields)
        self.assertEqual([m['body'] for m in ticket['messages']], ['Comment 1', 'Comment 2'])
        [attachment] = ticket['attachments']
        self.assertEqual(attachment['local_path'], str(self.dir / 'attachments' / 'PROJ-100' / 'spec.md'))
        self.assertEqual(Path(attachment['local_path']).read_text(), 'spec')
        self.assertEqual(attachment['mime_type'], 'text/markdown')
        self.assertTrue(ticket['url'].endswith('/browse/PROJ-100'))

    def test_configured_acceptance_field_skips_discovery(self):
        self.settings.acceptance_field = 'customfield_9'
        fetch_tickets(self.settings, ['PROJ-100'], str(self.dir))
        self.assertNotIn('/rest/api/2/field', self.server.requests)

    def test_missing_ticket_fails(self):
        with self.assertRaisesRegex(JiraError, '404'):
            fetch_tickets(self.settings, ['PROJ-404'], str(self.dir))

    def test_cli_fetch_and_fields_use_config(self):
        config = self.dir / 'jira.toml'
        config.write_text(f'[paths]\nlogseq_dir = "."\nrepos_dir = "."\n[jira]\nurl = "{self.server.url}"\n'
                          'token_env = "TEST_RUNNER_TOKEN"\n')
        run = [sys.executable, str(ROOT / 'run.py'), '--config', str(config), 'jira']
        fetched = subprocess.run([*run, 'fetch', 'PROJ-100', '--attachments-dir', str(self.dir / 'a')],
                                 text=True, capture_output=True)
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(json.loads(fetched.stdout)[0]['key'], 'PROJ-100')
        fields = subprocess.run([*run, 'fields', 'accept'], text=True, capture_output=True)
        self.assertEqual(fields.stdout, 'customfield_9\tAcceptance Criteria\n')


if __name__ == '__main__':
    unittest.main()
