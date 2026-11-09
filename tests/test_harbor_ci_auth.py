import base64
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch

spec=importlib.util.spec_from_file_location('harbor_ci_auth',Path(__file__).resolve().parents[1]/'scripts/harbor_ci_auth.py')
auth=importlib.util.module_from_spec(spec);spec.loader.exec_module(auth)


class HarborAuthTests(unittest.TestCase):
    def run_auth(self,actions):
        claims=base64.urlsafe_b64encode(json.dumps({'access':[{'type':'repository','name':'homelab/socrat-bot','actions':actions}]}).encode()).decode().rstrip('=')
        response=MagicMock();response.__enter__.return_value=response
        response.read.return_value=json.dumps({'token':'header.'+claims+'.signature'}).encode()
        opener=MagicMock();opener.open.return_value=response
        return opener,response

    def test_native_auth_grants_private_standard_config(self):
        opener,response=self.run_auth(['push','pull'])
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'RUNNER_TEMP':temp,'GITHUB_ENV':temp+'/env','HARBOR_USERNAME':'synthetic-robot','HARBOR_PASSWORD':'synthetic-password-only'}),patch.object(auth.urllib.request,'build_opener',return_value=opener):
            auth.configure('homelab/socrat-bot')
            directory=Path(Path(temp+'/env').read_text().strip().split('=',1)[1])
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode),0o700)
            self.assertEqual(stat.S_IMODE((directory/'config.json').stat().st_mode),0o600)
            config=json.loads((directory/'config.json').read_text());self.assertEqual(set(config['auths']),{auth.HOST})
            request=opener.open.call_args.args[0];self.assertTrue(request.full_url.startswith('https://'+auth.HOST+'/service/token?'))
            self.assertEqual(opener.open.call_args.kwargs['timeout'],15)
            response.read.assert_called_once_with(65537)

    def test_missing_push_never_writes_credentials(self):
        opener,_=self.run_auth(['pull'])
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'RUNNER_TEMP':temp,'GITHUB_ENV':temp+'/env','HARBOR_USERNAME':'synthetic-robot','HARBOR_PASSWORD':'synthetic-password-only'}),patch.object(auth.urllib.request,'build_opener',return_value=opener):
            with self.assertRaisesRegex(ValueError,'ROBOT_PUSH_GRANT_MISSING'):auth.configure('homelab/socrat-bot')
            self.assertEqual(list(Path(temp).iterdir()),[])

    def test_no_arbitrary_registry_or_repository(self):
        for repo in ('https://other.invalid/a','other/private','homelab/name:tag','homelab/a/b'):
            with self.assertRaisesRegex(ValueError,'INVALID_REPOSITORY'):auth.configure(repo)
