import os.path
import os
import sys
import getpass

# If no platform is matched, use the current directory.
_confdir = lambda app: ''
username = getpass.getuser()

def posix(app):
    if os.environ.get("XDG_CONFIG_HOME"):
        return os.path.join(os.environ["XDG_CONFIG_HOME"], app)
    else:
        return os.path.join(os.path.expanduser("~"),'.config',app)

def win32(app):
    if os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], app)
    else:
        return os.path.join(r'C:\Users', username, r'AppData\Roaming', app)

confdirs = (
        ('linux', posix),
        ('win32', win32),
        ('cygwin', posix),
        ('darwin', lambda app: os.path.join('/Users',username,'Library/Application Support',app))
    )

for platform, directory in confdirs:
    if sys.platform.startswith(platform):
        _confdir = directory

custom_config = None
for i, arg in enumerate(sys.argv):
    if arg == "--config" and len(sys.argv) > i+1:
        custom_config = sys.argv[i+1]

def confdir(app):
    if custom_config is not None:
        return custom_config
    else:
        return _confdir(app)

def get(app, conf_file, create=False):
    conf_folder = confdir(app)
    if not os.path.isdir(conf_folder):
        os.makedirs(conf_folder)
    conf_file = os.path.join(conf_folder,conf_file)
    if create and not os.path.isfile(conf_file):
        open(conf_file, 'w').close()
    return conf_file

