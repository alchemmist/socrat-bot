"""Authenticate the existing push robot over verified HTTPS; never log credentials.

BuildKit receives the ordinary Docker credential file, not an alternate token or
public registry. Its isolated Go TLS setting is configured by the workflow.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.parse
import urllib.request

HOST = 'harbor.socrat.alchemmist.xyz'


def configure(repository):
    if not re.fullmatch(r'homelab/[a-z0-9-]+', repository):
        raise ValueError('INVALID_REPOSITORY')
    username, password = os.environ['HARBOR_USERNAME'], os.environ['HARBOR_PASSWORD']
    if not username or not password or '\n' in username or '\n' in password:
        raise ValueError('INVALID_CREDENTIAL_CONFIGURATION')
    auth = base64.b64encode((username+':'+password).encode()).decode()
    query = urllib.parse.urlencode({'service':'harbor-registry', 'scope':'repository:'+repository+':pull,push'})
    request = urllib.request.Request('https://'+HOST+'/service/token?'+query,
                                     headers={'Authorization':'Basic '+auth})
    # Normal DNS, native robot auth and certificate verification. No redirects
    # should be involved in the native registry machine endpoint.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        body=response.read(65537)
        if len(body)>65536:raise ValueError('TOKEN_RESPONSE_TOO_LARGE')
        result=json.loads(body)
    token=result.get('token') or result.get('access_token')
    if not isinstance(token,str):raise ValueError('TOKEN_RESPONSE_INVALID')
    # This is a grant sanity check of an HTTPS-authenticated issuer response;
    # registry signature validation remains the registry's responsibility.
    part=token.split('.')[1]
    claims=json.loads(base64.urlsafe_b64decode(part+'='*((-len(part))%4)))
    grants=[x for x in claims.get('access',[]) if x.get('type')=='repository' and x.get('name')==repository]
    if not any({'pull','push'}<=set(x.get('actions',[])) for x in grants):
        raise ValueError('ROBOT_PUSH_GRANT_MISSING')
    directory=Path(tempfile.mkdtemp(prefix='harbor-ci-auth-',dir=os.environ['RUNNER_TEMP']))
    directory.chmod(0o700)
    path=directory/'config.json'
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump({'auths':{HOST:{'auth':auth}}},stream)
    with open(os.environ['GITHUB_ENV'],'a') as stream:stream.write('DOCKER_CONFIG='+str(directory)+'\n')
    print('Verified existing scoped robot pull/push grant; configured private ephemeral Docker credentials.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--repository',required=True)
    args=parser.parse_args()
    try:configure(args.repository)
    except Exception as error:
        # No URLs, HTTP bodies, credential material or token text in tracebacks.
        print('Harbor authentication preflight failed: '+type(error).__name__)
        raise SystemExit(1) from None
