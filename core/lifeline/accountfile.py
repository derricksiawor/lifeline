import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

KEY = "oauthAccount"
BACKUP_PREFIX = "accountfile-"
BACKUP_SUFFIX = ".json"
BACKUP_PATTERN = re.compile(r"^accountfile-(\d{20})\.json$")
TEMP_PREFIX = ".accountfile-"
DEFAULT_KEEP = 10
SURROGATES = re.compile("[\ud800-\udfff]")
NOT_PRUNED = "Lifeline could not remove old copies of Claude Code's settings file from {folder}."
NOT_CHECKED = "Lifeline could not read Claude Code's settings file back to check it."


class AccountFileError(Exception):
    pass


def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def _parse_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"number out of range: {text}")
    return value


def _parse(raw, path):
    try:
        data = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise AccountFileError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise AccountFileError(f"{path} does not hold a JSON object")
    return data


def _load(path):
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise AccountFileError(f"cannot read {path}: {error}") from error
    if not raw.strip():
        raise AccountFileError(f"{path} is empty")
    return raw, _parse(raw, path)


def read_oauth_account(path):
    _, data = _load(path)
    value = data.get(KEY)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AccountFileError(f"{KEY} in {path} is not an object")
    return value


def _escape_surrogate(match):
    return "\\u%04x" % ord(match.group(0))


def _serialise(data, newline):
    text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False)
    text = SURROGATES.sub(_escape_surrogate, text)
    if newline:
        text += "\n"
    return text.encode("utf-8")


def _backup_name(stamp):
    return f"{BACKUP_PREFIX}{stamp:020d}{BACKUP_SUFFIX}"


def _newest_stamp(backups):
    newest = -1
    try:
        for entry in backups.iterdir():
            found = BACKUP_PATTERN.match(entry.name)
            if found:
                newest = max(newest, int(found.group(1)))
    except FileNotFoundError:
        pass
    return newest


def _sync_folder(folder):
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_backup(backups, raw):
    backups = Path(backups)
    backups.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backups, 0o700)
    stamp = max(time.time_ns(), _newest_stamp(backups) + 1)
    while True:
        target = backups / _backup_name(stamp)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            stamp += 1
            continue
        break
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    _sync_folder(backups)
    return target


def _write_atomic(path, content, expected):
    mode = path.stat().st_mode & 0o7777
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=TEMP_PREFIX, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != expected:
            raise AccountFileError(f"{path} changed while it was being updated; nothing was written")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    _sync_folder(path.parent)


def _guard_links(path):
    if path.is_symlink():
        raise AccountFileError(f"{path} is a symbolic link; refusing to replace it")
    if path.stat().st_nlink > 1:
        raise AccountFileError(f"{path} has more than one hard link; refusing to replace it")


def replace_oauth_account(path, new, backups, notes=None):
    path = Path(path)
    notes = notes if notes is not None else []
    try:
        backup = _replace(path, new, backups, notes)
    except AccountFileError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise AccountFileError(f"could not update {path}: {error}") from error
    try:
        prune_backups(backups, protect=backup)
    except (OSError, ValueError):
        notes.append(NOT_PRUNED.format(folder=Path(backups)))
    return backup


def _read_back(path):
    return path.read_bytes()


def _replace(path, new, backups, notes):
    if not isinstance(new, dict):
        raise TypeError("the new account must be a dict")
    raw, data = _load(path)
    _guard_links(path)
    updated = dict(data)
    updated[KEY] = new
    content = _serialise(updated, raw.endswith(b"\n"))
    backup = _write_backup(backups, raw)
    _write_atomic(path, content, raw)
    try:
        written = _read_back(path)
    except OSError:
        notes.append(NOT_CHECKED)
        return backup
    if written != content:
        raise AccountFileError(f"{path} did not match what was written")
    _, after = _load(path)
    expected = {key: value for key, value in data.items() if key != KEY}
    found = {key: value for key, value in after.items() if key != KEY}
    if found != expected or after.get(KEY) != new or list(after) != list(updated):
        raise AccountFileError(f"{path} did not match what was written")
    return backup


def prune_backups(backups, keep=DEFAULT_KEEP, protect=None):
    if keep < 0:
        raise ValueError("keep must not be negative")
    backups = Path(backups)
    try:
        entries = [
            entry
            for entry in backups.iterdir()
            if BACKUP_PATTERN.match(entry.name) and entry.is_file()
        ]
    except FileNotFoundError:
        return
    entries.sort(key=lambda entry: entry.name)
    doomed = entries[: len(entries) - keep] if len(entries) > keep else []
    for entry in doomed:
        if protect is not None and entry == Path(protect):
            continue
        try:
            entry.unlink()
        except FileNotFoundError:
            pass
