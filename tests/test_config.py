"""Config file loading."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_runner import config  # noqa: E402


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def load(self, text):
        path = self.dir / 'config.toml'
        path.write_text(text)
        return config.load(str(path))

    def test_example_config_loads(self):
        settings = config.load(str(ROOT / 'config.example.toml'))
        self.assertEqual(settings.required_tags, ['agent', 'client'])
        self.assertIn('Bash(npm test:*)', settings.allowed_tools)
        self.assertIn('Bash(git -C * commit:*)', settings.allowed_tools)

    def test_paths_resolve_relative_to_config_and_home(self):
        settings = self.load('[paths]\nlogseq_dir = "graph"\nrepos_dir = "~/repos"\n'
                             '[jira]\nurl = "https://jira.example.com/rest/api/2/"\ntoken_file = "secrets/jira.token"\n')
        self.assertEqual(settings.logseq_dir, (self.dir / 'graph').resolve())
        self.assertEqual(settings.repos_dir, Path.home() / 'repos')
        self.assertEqual(settings.jira.token_file, str((self.dir / 'secrets' / 'jira.token').resolve()))
        self.assertEqual(settings.work_root, config.PROJECT_DIR)

    def test_settings_round_trip_through_run_snapshot(self):
        settings = self.load('[paths]\nlogseq_dir = "g"\nrepos_dir = "r"\n[jira]\nurl = "u"\n[repos]\nPROJ = "proj"\n')
        self.assertEqual(config.Settings.from_json(settings.to_json()), settings)

    def test_typos_and_missing_values_are_rejected(self):
        base = '[paths]\nlogseq_dir = "g"\nrepos_dir = "r"\n[jira]\nurl = "u"\n'
        for text, message in [(base + 'tokn_env = "X"\n', 'unknown key'),
                              (base + '[agnet]\n', 'unknown section'),
                              ('[paths]\nlogseq_dir = "g"\nrepos_dir = "r"\n', 'url is required'),
                              (base + '[agent]\nmax_turns = "10"\n', 'must be int'),
                              (base + '[tasks]\nrequired_tags = []\n', 'at least one tag')]:
            with self.subTest(message), self.assertRaisesRegex(config.ConfigError, message):
                self.load(text)

    def test_missing_file_points_to_example(self):
        with self.assertRaisesRegex(config.ConfigError, 'config.example.toml'):
            config.load(str(self.dir / 'absent.toml'))


if __name__ == '__main__':
    unittest.main()
