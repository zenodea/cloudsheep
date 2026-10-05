import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKES = ROOT / 'tests' / 'fakes'


class Sandbox(unittest.TestCase):
    """Temp HOME-like dirs, fake ssh on PATH, isolated cloudsheep state/config."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='cloudsheep-test-'))
        self.remote_home = self.tmp / 'remote-home'
        self.remote_home.mkdir()
        self.ssh_log = self.tmp / 'ssh.log'
        self.saved = dict(os.environ)
        os.environ.update({
            'PATH': f'{FAKES}:{os.environ["PATH"]}',
            'FAKE_REMOTE_HOME': str(self.remote_home),
            'FAKE_SSH_LOG': str(self.ssh_log),
            'CLOUDSHEEP_STATE_DIR': str(self.tmp / 'state'),
            'CLOUDSHEEP_CONFIG': str(self.tmp / 'machines.toml'),
            'GIT_CONFIG_GLOBAL': str(self.tmp / 'gitconfig'),
        })
        os.environ.pop('HERDR_BIN_PATH', None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        subprocess.run(['rm', '-rf', str(self.tmp)])

    def git(self, repo, *args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout

    def make_repo(self, name='repo') -> Path:
        repo = self.tmp / name
        repo.mkdir()
        self.git(repo, 'init', '-q', '-b', 'main')
        self.git(repo, 'config', 'user.name', 'Test')
        self.git(repo, 'config', 'user.email', 'test@example.com')
        (repo / 'app.py').write_text('print("v1")\n')
        (repo / 'gone.txt').write_text('remove me remotely\n')
        (repo / '.gitignore').write_text('build/\n')
        (repo / '.env.example').write_text('TOKEN=\n')
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-q', '-m', 'init')
        return repo

    def config(self, text):
        Path(os.environ['CLOUDSHEEP_CONFIG']).write_text(text)
