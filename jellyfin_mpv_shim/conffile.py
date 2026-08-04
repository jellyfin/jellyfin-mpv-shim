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


def posix_cache(app: str):
    if os.environ.get("XDG_CACHE_HOME"):
        return os.path.join(os.environ["XDG_CACHE_HOME"], app)
    return os.path.join(os.path.expanduser("~"), ".cache", app)


def win32_cache(app: str):
    # LOCALAPPDATA, not APPDATA: a cache must not follow a roaming profile
    # around a domain, which is what Microsoft's own guidance says and what
    # every other client does.
    if os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.environ["LOCALAPPDATA"], app, "cache")
    return os.path.join(r"C:\Users", username, r"AppData\Local", app, "cache")


cachedirs = (
    ("linux", posix_cache),
    ("openbsd", posix_cache),
    ("win32", win32_cache),
    ("cygwin", posix_cache),
    (
        "darwin",
        lambda app: os.path.join("/Users", username, "Library/Caches", app),
    ),
)

_cachedir = None
for platform, directory in cachedirs:
    if sys.platform.startswith(platform):
        _cachedir = directory


def cachedir(app: str):
    """Where regenerable data goes: artwork, not configuration.

    Separate from :func:`confdir` because the two have opposite lifetimes.
    Config is the user's; a cache is the app's, is safe to delete at any
    moment, and on every platform belongs somewhere the system already
    knows it may reclaim -- XDG_CACHE_HOME, ~/Library/Caches, LOCALAPPDATA.
    Putting hundreds of megabytes of posters in ~/.config would be rude to
    anyone who backs it up or syncs it.

    ``--config DIR`` takes it with it, so an isolated config directory stays
    isolated: two copies pointed at different configs do not then share one
    cache.

    A platform with no known cache convention falls back to the config
    directory's own ``cache/`` rather than to nothing. There is no case
    where this app can keep credentials but not artwork -- if the config
    directory is unusable, the answer is not "cache elsewhere", it is that
    nothing works -- so the only remaining fallback is a directory that
    cannot be created at all.
    """
    custom_config = get_args().config
    if custom_config is not None:
        return os.path.join(custom_config, "cache")
    if _cachedir is not None:
        return _cachedir(app)
    return os.path.join(confdir(app), "cache")


def get_cache_dir(app: str, subfolder: str):
    """A created subdirectory of :func:`cachedir`, or None if it could not
    be created -- a read-only home, a sandbox. Rare to the point of meaning
    the app is in trouble anyway (the config directory is a sibling
    problem), but None is a usable answer: the caller falls back to scratch
    space rather than failing to start over a cache."""
    base = cachedir(app)
    if not base:
        return None
    path = os.path.join(base, subfolder)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    return path


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
