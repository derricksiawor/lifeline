import json
import os
import re
from pathlib import Path

from lifeline import ident
from lifeline.slot import (
    STDIN_LIMIT,
    FileSlot,
    MacKeychainSlot,
    SecurityError,
    SlotError,
    _compact,
    _quote,
    login_keychain,
    run_security,
)

STORE_SERVICE = ident.bundle_id()
PREVIOUS_SUFFIX = ".previous"
WRAPPER_KEY = "claudeAiOauth"
_UUID_PATTERN = re.compile(r"[A-Za-z0-9-]+", re.ASCII)


class StoreError(SlotError):
    pass


class StoreUnreadable(StoreError):
    pass


class StoreLeftover(StoreError):
    pass


def _check_uuid(uuid):
    if not isinstance(uuid, str) or _UUID_PATTERN.fullmatch(uuid) is None:
        raise StoreError("Lifeline names each account in its files with letters, digits and hyphens only.")
    return uuid


def _wrap(oauth):
    if not isinstance(oauth, dict):
        raise StoreError("A parked sign-in must be a JSON object.")
    wrapped = {WRAPPER_KEY: oauth}
    failed = False
    try:
        json.dumps(wrapped, allow_nan=False)
    except (TypeError, ValueError):
        failed = True
    if failed:
        raise StoreError("A parked sign-in must hold only JSON values.")
    return wrapped


def _unwrap(value):
    inner = value.get(WRAPPER_KEY) if isinstance(value, dict) else None
    if not isinstance(inner, dict):
        raise StoreError("The parked sign-in is damaged.")
    return inner


class Store:
    def put(self, uuid, oauth):
        _check_uuid(uuid)
        wrapped = _wrap(oauth)
        previous = uuid + PREVIOUS_SUFFIX
        self._precheck(uuid, wrapped)
        self._precheck(previous, wrapped)
        self._prepare()
        try:
            current = self._read(uuid)
        except StoreUnreadable:
            raise
        except StoreError:
            current = None
        if current is not None:
            self._guard(
                lambda: self._write(previous, current),
                "A previous copy of the parked sign-in could not be kept.",
            )
        self._guard(lambda: self._write(uuid, wrapped), "The new parked sign-in could not be saved.")
        if not self._try_delete(previous) and not self._try_delete(previous):
            raise StoreLeftover("The new sign-in is saved, but an old copy remains.")

    def get(self, uuid):
        _check_uuid(uuid)
        self._prepare()
        damaged = False
        value = None
        try:
            value = self._read(uuid)
        except StoreUnreadable:
            raise
        except StoreError:
            damaged = True
        if damaged:
            return _unwrap(self._read_kept(uuid + PREVIOUS_SUFFIX))
        if value is None:
            return None
        return _unwrap(value)

    def names(self):
        """Every account this store holds a sign-in for, so Reset can clear them all when the account list is damaged."""
        found = {name.removesuffix(PREVIOUS_SUFFIX) for name in self._listed()}
        return {name for name in found if _UUID_PATTERN.fullmatch(name)}

    def remove(self, uuid):
        _check_uuid(uuid)
        self._prepare()
        self._guard(lambda: self._delete(uuid + PREVIOUS_SUFFIX), "The old copy could not be removed.")
        self._guard(lambda: self._delete(uuid), "The parked sign-in could not be removed.")

    def _read_kept(self, previous):
        kept = self._read(previous)
        if kept is None:
            raise StoreError("The parked sign-in is damaged and no earlier copy could be read.")
        return kept

    def _try_delete(self, name):
        ok = True
        try:
            self._delete(name)
        except (SlotError, OSError):
            ok = False
        return ok

    def _precheck(self, name, wrapped):
        pass

    def _prepare(self):
        pass

    def _guard(self, action, message):
        failed = False
        try:
            action()
        except StoreError:
            raise
        except (SlotError, OSError, ValueError):
            failed = True
        if failed:
            raise StoreError(message)

    def _read(self, name):
        slot = self._slot(name)
        failure = None
        value = None
        try:
            value = slot.read()
        except (SecurityError, OSError):
            failure = "unreadable"
        except (SlotError, ValueError):
            failure = "damaged"
        if failure == "unreadable":
            raise StoreUnreadable("The parked sign-in could not be read.")
        if failure == "damaged":
            raise StoreError("The parked sign-in is damaged.")
        if value is not None:
            _unwrap(value)
        return value

    def _write(self, name, value):
        self._slot(name).write(value)

    def _slot(self, name):
        raise NotImplementedError

    def _delete(self, name):
        raise NotImplementedError


class MacKeychainStore(Store):
    def __init__(self, service=STORE_SERVICE, runner=run_security, keychain=None):
        self.service = service
        self.runner = runner
        self.keychain = keychain if keychain is not None else login_keychain()

    def _slot(self, name):
        return MacKeychainSlot(self.service, name, self.runner, self.keychain)

    def _precheck(self, name, wrapped):
        digits = _compact(wrapped).encode("utf-8").hex()
        command = (
            f"add-generic-password -U -a {_quote(name)} "
            f"-s {_quote(self.service)} -X {digits} {_quote(self.keychain)}\n"
        )
        if len(command.encode("utf-8")) > STDIN_LIMIT:
            raise StoreError("The sign-in is too large to park without putting it on a command line.")

    def _write(self, name, value):
        self._precheck(name, value)
        super()._write(name, value)

    def _listed(self):
        for block in self.runner(["dump-keychain", self.keychain]).split("keychain: ")[1:]:
            service = re.search(r'"svce"<blob>="([^"]*)"', block)
            account = re.search(r'"acct"<blob>="([^"]*)"', block)
            if service and account and service.group(1) == self.service:
                yield account.group(1)

    def _delete(self, name):
        self._slot(name).delete()


def _inside(path, parent):
    return path == parent or parent in path.parents


def _is_slot_folder(folder):
    slot_folder = Path.home() / ".claude"
    real_slot = Path(os.path.realpath(slot_folder))
    real = Path(os.path.realpath(folder))
    if _inside(real, real_slot):
        return True
    try:
        reference = os.stat(slot_folder)
    except OSError:
        return False
    for probe in (real, *real.parents):
        try:
            found = os.stat(probe)
        except OSError:
            continue
        if (found.st_dev, found.st_ino) == (reference.st_dev, reference.st_ino):
            return True
    return False


def _refuse_slot_folder(folder):
    if _is_slot_folder(folder):
        raise StoreError("The sign-in folder may not be inside the Claude Code folder.")


class FileStore(Store):
    def __init__(self, folder=None):
        chosen = Path(folder) if folder is not None else ident.signin_folder()
        _refuse_slot_folder(chosen)
        self.folder = chosen
        self._create()

    def _create(self):
        missing = []
        probe = self.folder
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        failed = False
        try:
            for path in reversed(missing):
                path.mkdir(mode=0o700, exist_ok=True)
                os.chmod(path, 0o700)
            os.chmod(self.folder, 0o700)
        except OSError:
            failed = True
        if failed:
            raise StoreError("The sign-in folder could not be created.")

    def _prepare(self):
        _refuse_slot_folder(self.folder)

    def _slot(self, name):
        path = self.folder / (name + ".json")
        resolved_folder = Path(os.path.realpath(self.folder))
        if path.is_symlink() or Path(os.path.realpath(path)).parent != resolved_folder:
            raise StoreUnreadable("The sign-in file is a link or lies outside the sign-in folder.")
        return FileSlot(path)

    def _listed(self):
        return [path.name.removesuffix(".json") for path in self.folder.glob("*.json")]

    def _delete(self, name):
        try:
            os.unlink(self.folder / (name + ".json"))
        except FileNotFoundError:
            pass
