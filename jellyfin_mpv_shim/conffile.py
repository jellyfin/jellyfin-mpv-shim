import os.path
import os
import sys
import getpass

from .args import get_args

# If no platform is matched, use the current directory.
_confdir = None
username = getpass.getuser()


def posix(app: str):
    if os.environ.get("XDG_CONFIG_HOME"):
        return os.path.join(os.environ["XDG_CONFIG_HOME"], app)
    else:
        return os.path.join(os.path.expanduser("~"), ".config", app)


def win32(app: str):
    if os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], app)
    else:
        return os.path.join(r"C:\Users", username, r"AppData\Roaming", app)


confdirs = (
    ("linux", posix),
    ("openbsd", posix),
    ("win32", win32),
    ("cygwin", posix),
    (
        "darwin",
        lambda app: os.path.join(
            "/Users", username, "Library/Application Support", app
        ),
    ),
)

for platform, directory in confdirs:
    if sys.platform.startswith(platform):
        _confdir = directory

def confdir(app: str):
    custom_config = get_args().config
    if custom_config is not None:
        return custom_config
    elif _confdir is not None:
        return _confdir(app)
    else:
        return ""


def get(app: str, conf_file: str, create: bool = False):
    conf_folder = confdir(app)
    # exist_ok: two processes starting simultaneously would otherwise race
    # between the isdir check and makedirs and one hits FileExistsError.
    os.makedirs(conf_folder, exist_ok=True)
    conf_file = os.path.join(conf_folder, conf_file)
    if create and not os.path.isfile(conf_file):
        open(conf_file, "w").close()
    return conf_file


def get_dir(app: str, subfolder: str):
    conf_folder = os.path.join(confdir(app), subfolder)
    os.makedirs(conf_folder, exist_ok=True)
    return conf_folder
