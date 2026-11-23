"""Local integration checks; network state is manual, Jira faked over HTTP, Claude stubbed, Git real."""
import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
GIT = shutil.which('git')

import fake_jira  # noqa: E402
from agent_runner import journal  # noqa: E402

TAGS = ['agent', 'client']


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.repo = self.root / 'repos' / 'service'
        self.repo.mkdir(parents=True)
        self.origin = self.root / 'origin.git'
        self.git('init', '--bare', str(self.origin))
        self.git('init', '-b', 'main', str(self.repo))
        self.git('-C', str(self.repo), 'config', 'user.name', 'Test')
        self.git('-C', str(self.repo), 'config', 'user.email', 'test@example.invalid')
        (self.repo / 'README.md').write_text('Initial\n')
        self.git('-C', str(self.repo), 'add', '.')
        self.git('-C', str(self.repo), 'commit', '-m', 'Initial')
        self.git('-C', str(self.repo), 'remote', 'add', 'origin', str(self.origin))
        self.git('-C', str(self.repo), 'push', '-u', 'origin', 'main')
        self.git('-C', str(self.repo), 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/main')
        self.journals = self.root / 'logseq' / 'journals'
        self.journals.mkdir(parents=True)
        self.journal = self.journals / f'{datetime.date.today():%Y_%m_%d}.md'
        assets = self.journals.parent / 'assets'
        assets.mkdir()
        (assets / 'calculator requirements.md').write_text('Calculator must add integers.\n')
        pages = self.journals.parent / 'pages'
        pages.mkdir()
        (pages / 'integer-constraints.md').write_text('title:: Integer constraints\nReject fractions.\n')
        self.journal.write_text('''- Private meeting context PROJ-999
  - TODO First [[Projects/Example]] #PROJ-100 #agent #client repo::service
    - Modify the calculator.
    - Look at [Details](../assets/calculator%20requirements.md).
    - Follow [[Integer constraints]].
  - LATER Second PROJ-101 #[[agent]] #[[client]]
    repo:: service
- TODO Agent only PROJ-102 #agent
- TODO Client only PROJ-103 #client
- TODO Property only PROJ-104 #client
  agent:: ready
- TODO Prefix tags PROJ-105 #agent-other #client
''')
        self.state = self.root / 'vpn'
        self.state.write_text('Disconnected')
        self.stub('scutil', '''import os, pathlib
pathlib.Path(os.environ['TEST_VPN'] + '.called').touch()
raise SystemExit('Scripts must not inspect or control the VPN')
''')
        self.stub('osascript', 'pass\n')
        self.stub('git', '''import os, subprocess, sys
if 'push' in sys.argv: raise SystemExit('Pipeline must never push')
raise SystemExit(subprocess.run([os.environ['TEST_GIT'], *sys.argv[1:]]).returncode)
''')
        self.jira = fake_jira.start(self, network_file=self.state)
        for key in ('PROJ-100', 'PROJ-101'):
            self.jira.issues[key] = {'summary': 'Title', 'description': 'Details', 'comments': ['A comment']}
        self.stub('claude', '''import json, os, pathlib, subprocess, sys
assert pathlib.Path(os.environ['TEST_VPN']).read_text() == 'Disconnected'
p = pathlib.Path.cwd().parent
assert any(a.startswith('/') and pathlib.Path(a).resolve() == p / 'attachments' for a in sys.argv), 'Attachment directory not passed to Claude'
if p.name == 'PROJ-101':
    first = p.parent / 'PROJ-100'
    assert (first / 'reported').exists(), 'Previous task not handed off yet'
    assert '- LATER Second' in pathlib.Path(os.environ['TEST_JOURNAL']).read_text() or '- DOING Second' in pathlib.Path(os.environ['TEST_JOURNAL']).read_text()
if os.environ.get('TEST_AGENT_FAIL') == p.name:
    print(json.dumps({'is_error': True, 'result': 'Implementation failed'})); raise SystemExit(1)
repositories = json.loads((p / 'meta.json').read_text())['repositories']
if os.environ.get('TEST_NO_CHANGE') != p.name:
    for repo in repositories[1:]:
        assert repo['worktree'] in sys.argv, 'Additional worktree not passed to Claude'
    for repo in repositories:
        wt = pathlib.Path(repo['worktree'])
        if repo['role'] == 'reference':
            if os.environ.get('TEST_REF_WRITE') == p.name:
                (wt / 'README.md').write_text('Forbidden reference change')
            continue
        (wt / 'change.txt').write_text('Implementation')
        subprocess.run(['git', '-C', str(wt), 'add', 'change.txt'], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['git', '-C', str(wt), 'commit', '-m', p.name + ': implementation'], check=True, stdout=subprocess.DEVNULL)
outcome = {'status': 'complete', 'summary': 'Implemented change. Validation: local checks passed.', 'blocked_repositories': []}
if os.environ.get('TEST_SCOPE_BLOCK') == p.name:
    outcome = {'status': 'blocked', 'summary': 'Partial implementation retained; legacy contract must change.',
               'blocked_repositories': [{'name': os.environ.get('TEST_BLOCK_NAME', 'legacy'), 'reason': 'Add an integer addition endpoint to complete the ticket.'}]}
if os.environ.get('TEST_NOTHING_NEEDED') == p.name:
    outcome = {'status': 'no_change', 'summary': 'Already handled: the code rejects fractions.', 'blocked_repositories': []}
result = json.dumps(outcome)
if os.environ.get('TEST_INVALID_OUTCOME') == p.name:
    result = 'Everything looks done, but a reference repository needs a change.'
print(json.dumps({'is_error': False, 'result': result}))
''')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        WORK_ROOT=str(self.root / 'work'), RUN_DIR=str(self.root / 'work' / 'run-test'),
                        JIRA_AUTH_TOKEN=fake_jira.TOKEN, TEST_VPN=str(self.state), TEST_GIT=GIT,
                        TEST_JOURNAL=str(self.journal))
        self.repos_dir = self.repo.parent
        self.aliases = {}
        self.config = self.root / 'config.toml'

    def write_config(self):
        aliases = ''.join(f'{json.dumps(k)} = {json.dumps(str(v))}\n' for k, v in self.aliases.items())
        self.config.write_text(
            f'[paths]\nlogseq_dir = {json.dumps(str(self.journals.parent))}\n'
            f'repos_dir = {json.dumps(str(self.repos_dir))}\nwork_root = {json.dumps(self.env["WORK_ROOT"])}\n'
            f'[tasks]\nrequired_tags = ["agent", "client"]\n'
            f'[jira]\nurl = "{self.jira.url}"\n[repos]\n{aliases}')

    def runner(self, *args):
        self.write_config()
        return subprocess.run([sys.executable, str(ROOT / 'run.py'), '--config', str(self.config), *args],
                              env=self.env, text=True, capture_output=True)

    def git(self, *args):
        subprocess.run([GIT, *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stub(self, name, code):
        p = self.bin / name
        p.write_text('#!/usr/bin/env python3\n' + code)
        p.chmod(0o755)
        if os.name == 'nt':  # Windows finds commands by extension, so wrap the script.
            p.with_suffix('.cmd').write_text(f'@"{sys.executable}" "%~dp0{name}" %*\n')

    def run_pipeline(self):
        # Simulate the user connecting before gathering.
        self.state.write_text('Connected')
        result = self.runner('gather', '--run-dir', self.env['RUN_DIR'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state.read_text(), 'Connected')
        self.assertFalse(list(Path(self.env['RUN_DIR']).glob('*/result.json')))
        self.assertFalse((Path(self.env['WORK_ROOT']) / '.agent-run.lock').exists())
        # Simulate the user disconnecting and explicitly starting execution.
        self.state.write_text('Disconnected')
        tasks = json.loads((Path(self.env['RUN_DIR']) / 'tasks.json').read_text())
        if tasks:
            result = self.runner('execute', self.env['RUN_DIR'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.state.read_text(), 'Disconnected')
        self.assertFalse(Path(str(self.state) + '.called').exists())
        self.assertFalse((Path(self.env['WORK_ROOT']) / '.agent-run.lock').exists())
        return self.journal.read_text()

    def test_batch_handoff_and_tag_selection(self):
        text = self.run_pipeline()
        self.assertIn('- WAITING First', text)
        self.assertIn('- WAITING Second', text)
        self.assertIn('agent-branch:: agent/PROJ-100-run-test', text)
        self.assertIn('agent-worktree::', text)
        self.assertIn('agent-result:: Implemented change. Validation: local checks passed.', text)
        self.assertIn('- **Agent summary**', text)
        self.assertIn('- Details: ', text)
        self.assertIn('- TODO Agent only', text)
        tasks = json.loads((Path(self.env['RUN_DIR']) / 'tasks.json').read_text())
        self.assertEqual([t['id'] for t in tasks], ['PROJ-100', 'PROJ-101'])
        self.assertEqual(tasks[0]['repo'], 'service')
        self.assertEqual(tasks[0]['keys'], ['PROJ-100'])
        self.assertNotIn('parents', tasks[0])
        prompt = (Path(self.env['RUN_DIR']) / 'PROJ-100' / 'prompt.md').read_text()
        self.assertIn('Modify the calculator.', prompt)
        self.assertIn('Calculator must add integers.', prompt)
        self.assertIn('Reject fractions.', prompt)
        self.assertNotIn('Private meeting context', prompt)
        self.assertNotIn('PROJ-999', prompt)
        bases = [json.loads((Path(self.env['RUN_DIR']) / t['id'] / 'meta.json').read_text())['base'] for t in tasks]
        self.assertEqual(bases[0], bases[1])
        self.assertEqual(self.state.read_text(), 'Disconnected')
        self.env['RUN_DIR'] = str(self.root / 'work' / 'run-again')
        self.run_pipeline()
        self.assertEqual(self.journal.read_text(), text)

    def test_agent_failure_continues_with_manual_network_phases(self):
        self.env['TEST_AGENT_FAIL'] = 'PROJ-100'
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('Implementation failed', text)
        self.assertIn('- WAITING Second', text)
        self.assertEqual(self.state.read_text(), 'Disconnected')

    def test_execute_rejects_unprepared_run(self):
        run_dir = Path(self.env['RUN_DIR'])
        run_dir.mkdir(parents=True)
        result = self.runner('execute', str(run_dir))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('has not completed gathering', result.stderr)
        self.assertFalse((Path(self.env['WORK_ROOT']) / '.agent-run.lock').exists())
        self.assertFalse(Path(str(self.state) + '.called').exists())

    def test_jira_failure_does_not_run_agent(self):
        self.jira.fail.add('PROJ-100')
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('Jira fetch failed', text)
        self.assertIn('- WAITING Second', text)
        self.assertFalse((Path(self.env['RUN_DIR']) / 'PROJ-100' / 'result.json').exists())

    def test_no_change_is_failure(self):
        self.env['TEST_NO_CHANGE'] = 'PROJ-100'
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('Handoff incomplete', text)
        self.assertIn('- WAITING Second', text)

    def test_no_change_outcome_is_ready_for_review(self):
        self.env['TEST_NO_CHANGE'] = 'PROJ-100'
        self.env['TEST_NOTHING_NEEDED'] = 'PROJ-100'
        text = self.run_pipeline()
        self.assertIn('- WAITING First', text)
        self.assertIn('agent-status:: no-change', text)
        self.assertIn('agent-result:: Already handled: the code rejects fractions.', text)
        self.assertIn('No code change needed and no commits made', text)
        self.assertNotIn('Handoff incomplete', text)
        self.assertIn('- WAITING Second', text)
        self.assertEqual((Path(self.env['RUN_DIR']) / 'PROJ-100' / 'status').read_text().strip(), 'no-change')

    def test_no_change_outcome_with_commits_is_failure(self):
        self.env['TEST_NOTHING_NEEDED'] = 'PROJ-100'
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('agent-status:: fail', text)
        self.assertIn('Found 1 implementation commit(s)', text)
        self.assertIn('- WAITING Second', text)

    def test_missing_context_stops_only_affected_task(self):
        (self.journals.parent / 'assets' / 'calculator requirements.md').unlink()
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('Cannot read Markdown context file', text)
        self.assertIn('- WAITING Second', text)
        self.assertFalse((Path(self.env['RUN_DIR']) / 'PROJ-100' / 'result.json').exists())

    def add_repo(self, name):
        repo = self.repo.parent / name
        self.git('clone', '-b', 'main', str(self.origin), str(repo))
        self.git('-C', str(repo), 'config', 'user.name', 'Test')
        self.git('-C', str(repo), 'config', 'user.email', 'test@example.invalid')
        return repo

    def create_epic(self):
        branch = 'epic/PROJ-5000'
        self.git('-C', str(self.repo), 'checkout', '-b', branch)
        (self.repo / 'epic.txt').write_text('Epic baseline\n')
        self.git('-C', str(self.repo), 'add', 'epic.txt')
        self.git('-C', str(self.repo), 'commit', '-m', 'Epic baseline')
        self.git('-C', str(self.repo), 'push', 'origin', branch)
        commit = subprocess.check_output([GIT, '-C', str(self.repo), 'rev-parse', 'HEAD'], text=True).strip()
        self.git('-C', str(self.repo), 'checkout', 'main')
        return commit

    def test_epic_base_and_default_base_are_cached_separately(self):
        epic = self.create_epic()
        self.journal.write_text(self.journal.read_text().replace('repo::service', 'repo::service\n    base:: epic/PROJ-5000'))
        text = self.run_pipeline()
        self.assertIn('- WAITING First', text)
        self.assertIn('- WAITING Second', text)
        run = Path(self.env['RUN_DIR'])
        first = json.loads((run / 'PROJ-100' / 'meta.json').read_text())
        second = json.loads((run / 'PROJ-101' / 'meta.json').read_text())
        tasks = json.loads((run / 'tasks.json').read_text())
        self.assertEqual(tasks[0]['keys'], ['PROJ-100'])
        self.assertEqual(first['base'], epic)
        self.assertEqual(first['base_ref'], 'origin/epic/PROJ-5000')
        self.assertNotEqual(first['base'], second['base'])
        self.assertTrue((run / 'PROJ-100' / 'wt' / 'epic.txt').exists())
        self.assertFalse((run / 'PROJ-101' / 'wt' / 'epic.txt').exists())
        source_epic = subprocess.check_output([GIT, '-C', str(self.repo), 'rev-parse', 'epic/PROJ-5000'], text=True).strip()
        self.assertEqual(source_epic, epic)

    def test_repo_base_override_and_reference_default(self):
        epic = self.create_epic()
        self.add_repo('ui')
        self.add_repo('legacy')
        self.journal.write_text(self.journal.read_text().replace('repo::service',
            'repos:: service, ui\n    reference-repos:: legacy\n    base:: origin/epic/PROJ-5000\n    base-branches:: ui=default'))
        text = self.run_pipeline()
        self.assertIn('- WAITING First', text)
        meta = json.loads((Path(self.env['RUN_DIR']) / 'PROJ-100' / 'meta.json').read_text())
        service, ui, legacy = meta['repositories']
        self.assertEqual(service['base'], epic)
        self.assertEqual(ui['base_ref'], 'origin/main')
        self.assertEqual(legacy['base_ref'], 'origin/main')
        self.assertFalse((Path(ui['worktree']) / 'epic.txt').exists())
        self.assertFalse((Path(legacy['worktree']) / 'epic.txt').exists())

    def test_missing_base_fails_even_with_stale_tracking_ref(self):
        self.git('-C', str(self.repo), 'update-ref', 'refs/remotes/origin/epic/missing', 'HEAD')
        self.journal.write_text(self.journal.read_text().replace('repo::service', 'repo::service\n    base:: epic/missing'))
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('Could not fetch base branch origin/epic/missing', text)
        self.assertIn('- WAITING Second', text)
        self.assertFalse((Path(self.env['RUN_DIR']) / 'PROJ-100' / 'wt').exists())

    def test_unknown_base_override_repository_fails_preparation(self):
        self.journal.write_text(self.journal.read_text().replace('repo::service', 'repo::service\n    base-branches:: typo=epic/PROJ-5000'))
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('base-branches:: names unknown repositories: typo', text)
        self.assertIn('- WAITING Second', text)

    def add_attachment(self, declared=None):
        content = b'Jira attachment: integer addition requirements.'
        self.jira.issues['PROJ-100']['attachments'] = {'requirements.md': (content, declared or len(content))}

    def test_jira_attachments_downloaded_and_listed_offline(self):
        self.add_attachment()
        text = self.run_pipeline()
        self.assertIn('- WAITING First', text)
        task_dir = Path(self.env['RUN_DIR']) / 'PROJ-100'
        attachment = task_dir / 'attachments' / 'PROJ-100' / 'requirements.md'
        self.assertIn('integer addition requirements', attachment.read_text())
        self.assertIn(str(attachment.resolve()), (task_dir / 'prompt.md').read_text())
        self.assertIn('Downloaded Jira attachments', (task_dir / 'prompt.md').read_text())

    def test_incomplete_attachment_stops_only_affected_task(self):
        self.add_attachment(declared=999)
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('Attachment preparation failed: Attachment size mismatch', text)
        self.assertIn('- WAITING Second', text)
        self.assertFalse((Path(self.env['RUN_DIR']) / 'PROJ-100' / 'result.json').exists())

    def test_multiple_implementation_and_reference_repositories(self):
        for name in ['ui', 'worker', 'legacy']:
            self.add_repo(name)
        self.journal.write_text(self.journal.read_text().replace('repo::service', 'repos:: service, ui, worker\n    reference-repos:: legacy'))
        text = self.run_pipeline()
        self.assertIn('- WAITING First', text)
        self.assertIn('agent-repositories::', text)
        self.assertIn('legacy [reference]', text)
        self.assertIn('ui=agent/PROJ-100-run-test', text)
        meta = json.loads((Path(self.env['RUN_DIR']) / 'PROJ-100' / 'meta.json').read_text())
        self.assertEqual([r['role'] for r in meta['repositories']], ['implementation'] * 3 + ['reference'])
        for repo in meta['repositories']:
            wt = Path(repo['worktree'])
            self.assertEqual((wt / 'change.txt').exists(), repo['role'] == 'implementation')
            self.assertEqual((Path(repo['repo']) / 'README.md').read_text(), 'Initial\n')
        self.assertFalse((Path(meta['repositories'][-1]['worktree']) / 'change.txt').exists())

    def test_named_aliases_resolve_outside_repos_dir(self):
        ui = self.add_repo('ui')
        reference = self.add_repo('legacy')
        self.aliases = {'api-service': self.repo, 'web-app': ui, 'context-library': reference,
                        'service': self.repo, 'PROJ': '/missing/project-default'}
        self.repos_dir = self.root / 'unrelated'
        self.journal.write_text(self.journal.read_text().replace(
            'repo::service', 'repos:: api-service, web-app\n    reference-repos:: context-library'))
        text = self.run_pipeline()
        self.assertIn('- WAITING First', text)
        self.assertIn('- WAITING Second', text)
        meta = json.loads((Path(self.env['RUN_DIR']) / 'PROJ-100' / 'meta.json').read_text())
        self.assertEqual([r['repo'] for r in meta['repositories']],
                         [str(p.resolve()) for p in [self.repo, ui, reference]])

    def test_reference_changes_fail_handoff(self):
        self.add_repo('legacy')
        self.journal.write_text(self.journal.read_text().replace('repo::service', 'repo::service\n    reference-repos:: legacy'))
        self.env['TEST_REF_WRITE'] = 'PROJ-100'
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('Reference repository changed: legacy', text)
        self.assertIn('- WAITING Second', text)

    def scope_block(self, no_changes=False):
        self.add_repo('legacy')
        self.journal.write_text(self.journal.read_text().replace('repo::service', 'repo::service\n    reference-repos:: legacy'))
        self.env['TEST_SCOPE_BLOCK'] = 'PROJ-100'
        if no_changes:
            self.env['TEST_NO_CHANGE'] = 'PROJ-100'
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('agent-status:: blocked', text)
        self.assertIn('agent-blocked:: legacy: Add an integer addition endpoint', text)
        self.assertIn('- WAITING Second', text)
        task_dir = Path(self.env['RUN_DIR']) / 'PROJ-100'
        self.assertEqual((task_dir / 'status').read_text().strip(), 'blocked')
        self.assertEqual((task_dir / 'wt' / 'change.txt').exists(), not no_changes)
        meta = json.loads((task_dir / 'meta.json').read_text())
        reference = next(r for r in meta['repositories'] if r['role'] == 'reference')
        self.assertEqual((Path(reference['worktree']) / 'README.md').read_text(), 'Initial\n')
        return text

    def test_scope_block_overrides_committed_implementation(self):
        text = self.scope_block()
        self.env['RUN_DIR'] = str(self.root / 'work' / 'run-blocked-again')
        self.run_pipeline()
        self.assertEqual(self.journal.read_text(), text)

    def test_scope_block_without_implementation_changes(self):
        self.scope_block(no_changes=True)

    def test_invalid_outcome_cannot_be_ready(self):
        self.env['TEST_INVALID_OUTCOME'] = 'PROJ-100'
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('Agent outcome missing or invalid', text)
        self.assertIn('agent-status:: fail', text)
        self.assertIn('- WAITING Second', text)

    def test_blocker_must_name_a_reference_repository(self):
        self.env['TEST_SCOPE_BLOCK'] = 'PROJ-100'
        self.env['TEST_BLOCK_NAME'] = 'service'
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('Agent outcome missing or invalid', text)
        self.assertNotIn('agent-status:: blocked', text)
        self.assertIn('- WAITING Second', text)

    def test_conflicting_repository_roles_fail_preparation(self):
        self.journal.write_text(self.journal.read_text().replace('repo::service', 'repo::service\n    reference-repos:: service'))
        text = self.run_pipeline()
        self.assertIn('- TODO First', text)
        self.assertIn('conflicting roles', text)
        self.assertIn('- WAITING Second', text)
        self.assertFalse((Path(self.env['RUN_DIR']) / 'PROJ-100' / 'wt').exists())

    def test_report_ids_match_exactly(self):
        self.journal.write_text('- DOING Other\n  agent-run:: PROJ-100-2 @ now\n- DOING Target\n  agent-run:: PROJ-100 @ now\n')
        journal.report(self.journal, 'PROJ-100', 'ok', 'Done')
        text = self.journal.read_text()
        self.assertIn('- DOING Other', text)
        self.assertIn('- WAITING Target', text)

    def test_report_writes_summary_as_child_blocks(self):
        self.journal.write_text('- DOING Target #agent #client\n  agent-run:: PROJ-100 @ now\n  collapsed:: true\n\t- Existing note\n- Next\n')
        summary = ('Backend done; UI not started.\n\n## Changes\n- service\n  - Added the enum,\n    mapped to codes\n'
                   '## Open questions\n- TODO pick a default #agent #client\n- repo:: other\n')
        journal.report(self.journal, 'PROJ-100', 'ok', summary, details='/run/summary.md')
        self.assertEqual(self.journal.read_text(), '\n'.join([
            '- WAITING Target #agent #client', '  agent-run:: PROJ-100 @ now',
            '  agent-result:: Backend done; UI not started.', '  agent-status:: ok', '  collapsed:: true',
            '\t- **Agent summary**', '\t\t- **Changes**', '\t\t\t- service', '\t\t\t\t- Added the enum, mapped to codes',
            '\t\t- **Open questions**', '\t\t\t- Todo pick a default #agent #client', '\t\t\t- repo: other',
            '\t\t- Details: /run/summary.md', '\t- Existing note', '- Next', '']))
        self.assertEqual(journal.pick(self.journals.parent, 1, TAGS, dry_run=True), [])

    def test_report_without_children_uses_space_indent(self):
        self.journal.write_text('  - DOING Target\n    agent-run:: PROJ-100 @ now\n  - Sibling\n')
        journal.report(self.journal, 'PROJ-100', 'fail', 'Agent failed.\n- See claude.err')
        self.assertEqual(self.journal.read_text(), '  - TODO Target\n    agent-run:: PROJ-100 @ now\n'
                         '    agent-result:: Agent failed.\n    agent-status:: fail\n'
                         '    - **Agent summary**\n      - See claude.err\n  - Sibling\n')


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journals = self.root / 'journals'
        self.journals.mkdir()
        self.journal = self.journals / f'{datetime.date.today():%Y_%m_%d}.md'

    def pick(self, block):
        self.journal.write_text(block)
        return journal.pick(self.root, 1, TAGS, dry_run=True)

    def context(self, task):
        return journal.build_context(task, self.root)

    def test_configured_tags_select_tasks(self):
        self.journal.write_text('- TODO Other #PROJ-1 #agent #other\n- TODO Client #PROJ-2 #agent #[[client]]\n')
        self.assertEqual([t['id'] for t in journal.pick(self.root, 1, ['agent', 'client'], dry_run=True)], ['PROJ-2'])
        self.assertEqual(journal.pick(self.root, 1, ['agent', 'client'], dry_run=True)[0]['title'], 'TODO Client #PROJ-2')

    def test_crlf_journal_keeps_its_line_endings(self):
        self.journal.write_bytes(b'- TODO Windows #PROJ-1 #agent #client\r\n  - Child\r\n')
        journal.pick(self.root, 1, TAGS)
        journal.start(self.journal, 'PROJ-1')
        journal.report(self.journal, 'PROJ-1', 'ok', 'Done.\n- Detail')
        data = self.journal.read_bytes()
        self.assertNotIn(b'\n', data.replace(b'\r\n', b''))
        self.assertIn(b'- WAITING Windows', data)
        self.assertIn(b'agent-result:: Done.\r\n', data)

    def test_windows_style_link_paths(self):
        self.assertEqual(journal.link_path('C:\\docs\\spec.md#part'), Path('C:\\docs\\spec.md'))
        self.assertIsNone(journal.link_path('https://example.com/spec.md'))
        self.assertIsNone(journal.link_path('file://server/share/spec.md'))

    def test_parent_ticket_is_not_inherited(self):
        task = self.pick('- Meeting PROJ-999\n  - TODO Implement addition #agent #client repo::my-service\n    - Integers only.\n')[0]
        self.assertEqual(task['keys'], [])
        self.assertEqual(task['repo'], 'my-service')
        self.assertNotIn('Meeting', task['block'])

    def test_file_url_and_angle_path_with_spaces(self):
        doc = self.root / 'details with spaces.md'
        doc.write_text('Additional requirements')
        task = self.pick(f'- TODO Addition #PROJ-5000 #agent #client repo::my-service\n  - [Details]({doc.as_uri()}#section)\n  - [Details](<../details with spaces.md>)\n')[0]
        self.assertEqual(task['context_files'], [str(doc.resolve())])
        self.assertEqual(self.context(task).count('Additional requirements'), 1)

    def test_namespace_page_and_missing_page(self):
        pages = self.root / 'pages'
        pages.mkdir()
        (pages / 'Specs___Addition.md').write_text('Integer addition specification')
        task = self.pick('- TODO Addition [[Projects/Example]] #PROJ-5000 #agent #client\n  - Look at [[Specs/Addition]].\n')[0]
        self.assertEqual(task['context_pages'], ['Specs/Addition'])
        self.assertIn('Integer addition specification', self.context(task))
        task['context_pages'] = ['Missing page']
        with self.assertRaisesRegex(journal.JournalError, 'found 0'):
            self.context(task)

    def test_repository_lists_and_legacy_property(self):
        task = self.pick('- TODO Full stack #PROJ-5000 #agent #client repo::api repos:: ui, worker\n  reference-repos:: shared, legacy\n')[0]
        self.assertEqual(task['repo'], 'api')
        self.assertEqual(task['repos'], ['api', 'ui', 'worker'])
        self.assertEqual(task['reference_repos'], ['shared', 'legacy'])

    def test_base_properties_and_ticket_keys(self):
        task = self.pick('- TODO Addition #PROJ-100 #agent #client repo::api base::epic/PROJ-5000\n  base-branches:: api=origin/epic/PROJ-5001, ui=default\n')[0]
        self.assertEqual(task['base_branch'], 'epic/PROJ-5000')
        self.assertEqual(task['base_branches'], {'api': 'origin/epic/PROJ-5001', 'ui': 'default'})
        self.assertEqual(task['keys'], ['PROJ-100'])
        self.assertEqual(task['base_error'], '')

    def test_malformed_base_override_is_reported(self):
        task = self.pick('- TODO Addition #PROJ-100 #agent #client\n  base-branches:: api\n')[0]
        self.assertIn('repo=branch', task['base_error'])


if __name__ == '__main__':
    unittest.main()
