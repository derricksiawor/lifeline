# What this install is called. Everything it owns comes from here, so installs with different bundle ids share
# nothing. A value comes from the environment, else from a file that bundle.sh or the installer wrote beside this
# one, else the default.

import os
import re
import sys
from pathlib import Path

DEFAULT_ID = "com.example.lifeline"
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*", re.ASCII)
DEFAULT_PORT = 7433
HERE = Path(__file__).resolve().parent


def _setting(name, file, env):
    env = os.environ if env is None else env
    if env.get(name):
        return env[name].strip()
    try:
        return (HERE / file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def bundle_id(env=None):
    found = _setting("LIFELINE_BUNDLE_ID", "bundle-id.txt", env)
    return found if ID_PATTERN.fullmatch(found) else DEFAULT_ID


def port(env=None):
    found = _setting("LIFELINE_PORT", "port.txt", env)
    return int(found) if found.isdigit() and 1 <= int(found) <= 65535 else DEFAULT_PORT


def listen(env=None):
    """The address the other computer listens on, which the installer saves. Empty means the address lifeline pair prints."""
    return _setting("LIFELINE_LISTEN", "listen.txt", env)


def claude(env=None):
    """The claude command the installer found, so a sign-in does not search folders anyone could write to."""
    return _setting("LIFELINE_CLAUDE", "claude.txt", env)


def data_folder(home=None, system=None, env=None):
    home = Path(home) if home is not None else Path.home()
    if (system or sys.platform).lower() == "darwin":
        return home / "Library" / "Application Support" / bundle_id(env)
    return home / ".local" / "share" / bundle_id(env)


def signin_folder(home=None, env=None):
    return (Path(home) if home is not None else Path.home()) / ".config" / bundle_id(env) / "signins"


def log_folder(home=None, env=None):
    return (Path(home) if home is not None else Path.home()) / "Library" / "Logs" / bundle_id(env)
