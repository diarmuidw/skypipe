"""Cloud satellite manager

Here we use dotcloud to lookup or deploy the satellite server. This also
means we need dotcloud credentials, so we get those if we need them.
Most of this functionality is pulled from the dotcloud client, but is
modified and organized to meet our needs. This is why we pass around and
work with a cli object. This is the CLI object from the dotcloud client.
"""
import time
import os
import os.path
import socket
import sys
import subprocess
import threading
from StringIO import StringIO

import dotcloud.ui.cli
from dotcloud.ui.config import GlobalConfig, CLIENT_KEY, CLIENT_SECRET
from dotcloud.client import RESTClient
from dotcloud.client.auth import NullAuth
from dotcloud.client.errors import RESTAPIError

from skypipe import client

APPNAME = "skypipe0"
satellite_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'satellite')

# This is a monkey patch to silence rsync output
class FakeSubprocess(object):
    @staticmethod
    def call(*args, **kwargs):
        kwargs['stdout'] = subprocess.PIPE
        return subprocess.call(*args, **kwargs)
dotcloud.ui.cli.subprocess = FakeSubprocess

def wait_for(text, finish=None, io=None):
    """Displays dots until returned event is set"""
    if finish:
        finish.set()
        time.sleep(0.1) # threads, sigh
    if not io:
        io = sys.stdout
    finish = threading.Event()
    io.write(text)
    def _wait():
        while not finish.is_set():
            io.write('.')
            io.flush()
            finish.wait(timeout=1)
        io.write('\n')
    threading.Thread(target=_wait).start()
    return finish


def lookup_endpoint(cli):
    """Looks up the application endpoint from dotcloud"""
    url = '/applications/{0}/environment'.format(APPNAME)
    environ = cli.user.get(url).item
    port = environ['DOTCLOUD_SATELLITE_ZMQ_PORT']
    host = socket.gethostbyname(environ['DOTCLOUD_SATELLITE_ZMQ_HOST'])
    return "tcp://{0}:{1}".format(host, port)


def setup_dotcloud_account(cli):
    """Gets user/pass for dotcloud, performs auth, and stores keys"""
    client = RESTClient(endpoint=cli.client.endpoint)
    client.authenticator = NullAuth()
    urlmap = client.get('/auth/discovery').item
    username = cli.prompt('dotCloud email')
    password = cli.prompt('Password', noecho=True)
    credential = {'token_url': urlmap.get('token'),
        'key': CLIENT_KEY, 'secret': CLIENT_SECRET}
    try:
        token = cli.authorize_client(urlmap.get('token'), credential, username, password)
    except Exception as e:
        cli.die('Username and password do not match. Try again.')
    token['url'] = credential['token_url']
    config = GlobalConfig()
    config.data = {'token': token}
    config.save()
    cli.global_config = GlobalConfig()  # reload
    cli.setup_auth()
    cli.get_keys()

def setup(cli):
    """Everything to make skypipe ready to use"""
    if not cli.global_config.loaded:
        setup_dotcloud_account(cli)
    discover_satellite(cli)
    cli.success("Skypipe is ready for action")


def discover_satellite(cli, deploy=True, timeout=5):
    """Looks to make sure a satellite exists, returns endpoint

    First makes sure we have dotcloud account credentials. Then it looks
    up the environment for the satellite app. This will contain host and
    port to construct an endpoint. However, if app doesn't exist, or
    endpoint does not check out, we call `launch_satellite` to deploy,
    which calls `discover_satellite` again when finished. Ultimately we
    return a working endpoint. If deploy is False it will not try to
    deploy.
    """
    if not cli.global_config.loaded:
        cli.die("Please setup skypipe by running `skypipe --setup`")

    try:
        endpoint = lookup_endpoint(cli)
        ok = client.check_skypipe_endpoint(endpoint, timeout)
        if ok:
            return endpoint
        else:
            return launch_satellite(cli) if deploy else None
    except (RESTAPIError, KeyError):
        return launch_satellite(cli) if deploy else None

def destroy_satellite(cli):
    url = '/applications/{0}'.format(APPNAME)
    try:
        res = cli.user.delete(url)
    except RESTAPIError:
        pass

def launch_satellite(cli):
    """Deploys a new satellite app over any existing app"""

    cli.info("Launching skypipe satellite:")

    finish = wait_for("    Pushing to dotCloud")

    # destroy any existing satellite
    destroy_satellite(cli)

    # create new satellite app
    url = '/applications'
    try:
        cli.user.post(url, {
            'name': APPNAME,
            'flavor': 'sandbox'
            })
    except RESTAPIError as e:
        if e.code == 409:
            cli.die('Application "{0}" already exists.'.format(APPNAME))
        else:
            cli.die('Creating application "{0}" failed: {1}'.format(APPNAME, e))
    class args: application = APPNAME
    #cli._connect(args)

    # push satellite code
    protocol = 'rsync'
    url = '/applications/{0}/push-endpoints{1}'.format(APPNAME, '')
    endpoint = cli._select_endpoint(cli.user.get(url).items, protocol)
    class args: path = satellite_path
    cli.push_with_rsync(args, endpoint)

    # tell dotcloud to deploy, then wait for it to finish
    revision = None
    clean = False
    url = '/applications/{0}/deployments'.format(APPNAME)
    response = cli.user.post(url, {'revision': revision, 'clean': clean})
    deploy_trace_id = response.trace_id
    deploy_id = response.item['deploy_id']


    original_stdout = sys.stdout

    finish = wait_for("    Waiting for deployment", finish, original_stdout)

    try:
        sys.stdout = StringIO()
        res = cli._stream_deploy_logs(APPNAME, deploy_id,
                deploy_trace_id=deploy_trace_id, follow=True)
        if res != 0:
            return res
    except KeyboardInterrupt:
        cli.error('You\'ve closed your log stream with Ctrl-C, ' \
            'but the deployment is still running in the background.')
        cli.error('If you aborted because of an error ' \
            '(e.g. the deployment got stuck), please e-mail\n' \
            'support@dotcloud.com and mention this trace ID: {0}'
            .format(deploy_trace_id))
        cli.error('If you want to continue following your deployment, ' \
                'try:\n{0}'.format(
                    cli._fmt_deploy_logs_command(deploy_id)))
        cli.die()
    except RuntimeError:
        # workaround for a bug in the current dotcloud client code
        pass
    finally:
        sys.stdout = original_stdout

    finish = wait_for("    Satellite coming online", finish)

    endpoint = lookup_endpoint(cli)
    ok = client.check_skypipe_endpoint(endpoint, 120)
   
    finish.set()
    time.sleep(0.1) # sigh, threads

    if ok:
        return endpoint
    else:
        cli.die("Satellite failed to come online")

