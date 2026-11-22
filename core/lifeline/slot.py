import ctypes
import json
import os
import subprocess
import tempfile
from pathlib import Path

SECURITY_BINARY = "/usr/bin/security"
SERVICE = "Claude Code-credentials"
ITEM_NOT_FOUND = 44
STDIN_LIMIT = 4032
TIMEOUT_SECONDS = 5
NOT_FOUND = -25300
ASKS = -25308
KEYCHAIN_UNSAVED = "Lifeline could not save the sign-in in the Keychain, so it changed nothing there."
KEYCHAIN_ASKS = "The Keychain wanted a click before it would save this sign-in, so Lifeline changed nothing there."
TEST_GUARD = "LIFELINE_TEST_GUARD"
_RUN, _CDLL, _SECURITY = subprocess.run, ctypes.CDLL, SECURITY_BINARY


class SlotError(Exception):
    pass


class KeychainGuard(BaseException):
    """The tests turn the guard on, and then any Keychain call a test has not faked raises at once: real items such as
    Claude Code-credentials and Lifeline's, and every other name too. It is not an Exception, so no handler in Lifeline
    can swallow it."""


def _guard(faked):
    if os.environ.get(TEST_GUARD) and not faked:
        raise KeychainGuard("A test reached the real Keychain without a fake.")


class SecurityError(SlotError):
    def __init__(self, command, returncode, detail=""):
        self.command = command
        self.returncode = returncode
        self.detail = detail
        message = f"security {command} exited with status {returncode}"
        if detail:
            message += f": {detail}"
        super().__init__(message)


def run_security(args, stdin=None):
    _guard(subprocess.run is not _RUN or SECURITY_BINARY != _SECURITY)
    command = args[0] if args else ""
    failure = None
    feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        done = subprocess.run(
            [SECURITY_BINARY, *args],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            process_group=0,
            **feed,
        )
    except subprocess.TimeoutExpired:
        failure = "timed out"
    except OSError as error:
        failure = type(error).__name__
    if failure is not None:
        raise SecurityError(command, -1, failure)
    if done.returncode != 0:
        detail = done.stderr.strip() if command == "find-generic-password" else ""
        raise SecurityError(command, done.returncode, detail)
    return done.stdout


def login_keychain():
    """The only keychain Lifeline touches. Naming it in every call keeps a lookup for a missing item out of every other
    keychain, such as a locked one that would open a password dialog."""
    return str(Path.home() / "Library" / "Keychains" / "login.keychain-db")


def update_item(service, account, data, keychain):
    """Sets a Keychain item's data through Security.framework, so a sign-in never sits on a command line that ps can show.

    It turns Keychain prompts off for this whole process. That is right for Lifeline, which never shows a Keychain
    prompt: a write the Keychain would ask about fails at once with ASKS instead of waiting for a click. The search list
    holds only the keychain it is given, which is the login keychain outside the tests.
    """
    _guard(ctypes.CDLL is not _CDLL)
    pointer = ctypes.c_void_p
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    for call in (cf.CFStringCreateWithCString, cf.CFDataCreate, cf.CFDictionaryCreate, cf.CFArrayCreate):
        call.restype = pointer
    cf.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFDataCreate.argtypes = [pointer, ctypes.c_char_p, ctypes.c_long]
    cf.CFDictionaryCreate.argtypes = [pointer, ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.c_long, pointer, pointer]
    cf.CFArrayCreate.argtypes = [pointer, ctypes.POINTER(pointer), ctypes.c_long, pointer]
    cf.CFRelease.argtypes = [pointer]
    security.SecItemUpdate.argtypes = [pointer, pointer]
    security.SecItemUpdate.restype = ctypes.c_int32
    security.SecKeychainOpen.argtypes = [ctypes.c_char_p, ctypes.POINTER(pointer)]
    security.SecKeychainOpen.restype = ctypes.c_int32
    security.SecKeychainSetUserInteractionAllowed.argtypes = [ctypes.c_bool]
    owned = []

    def keep(made):
        if not made:
            raise MemoryError()
        owned.append(made)
        return made

    def name(symbol):
        return pointer.in_dll(security, symbol).value

    def callbacks(*kinds):
        return [ctypes.addressof(pointer.in_dll(cf, f"kCFType{kind}CallBacks")) for kind in kinds]

    def table(pairs):
        keys, values = (pointer * len(pairs))(*[k for k, _ in pairs]), (pointer * len(pairs))(*[v for _, v in pairs])
        return keep(cf.CFDictionaryCreate(None, keys, values, len(pairs), *callbacks("DictionaryKey", "DictionaryValue")))

    def text(value):
        return keep(cf.CFStringCreateWithCString(None, value.encode("utf-8"), 0x08000100))

    try:
        pairs = [(name("kSecClass"), name("kSecClassGenericPassword")), (name("kSecAttrService"), text(service)), (name("kSecAttrAccount"), text(account))]
        opened = pointer()
        status = security.SecKeychainOpen(os.fsencode(keychain), ctypes.byref(opened))
        if status != 0:
            return status
        keep(opened.value)
        pairs.append((name("kSecMatchSearchList"), keep(cf.CFArrayCreate(None, (pointer * 1)(opened.value), 1, *callbacks("Array")))))
        change = table([(name("kSecValueData"), keep(cf.CFDataCreate(None, data, len(data))))])
        security.SecKeychainSetUserInteractionAllowed(False)
        return security.SecItemUpdate(table(pairs), change)
    finally:
        for made in owned:
            cf.CFRelease(made)


def _quote(value):
    if any(ch in value for ch in "\n\r\0"):
        raise SlotError("A Keychain name may not contain a line break.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _compact(signin):
    if not isinstance(signin, dict):
        raise SlotError("A sign-in must be a JSON object.")
    return json.dumps(signin, separators=(",", ":"))


def _parse(text):
    text = text.strip()
    if not text:
        return None
    value = None
    bad = False
    try:
        if not text.startswith("{"):
            text = bytes.fromhex(text).decode("utf-8")
        value = json.loads(text)
    except ValueError:
        bad = True
    if bad:
        raise SlotError("The stored sign-in is not valid JSON.")
    if not isinstance(value, dict):
        raise SlotError("The stored sign-in is not a JSON object.")
    return value


class Slot:
    def read(self):
        raise NotImplementedError

    def write(self, signin):
        raise NotImplementedError


class MacKeychainSlot(Slot):
    def __init__(self, service=SERVICE, account=None, runner=run_security, keychain=None):
        self.service = service
        self.account = account if account is not None else os.environ["USER"]
        self.runner = runner
        self.keychain = keychain if keychain is not None else login_keychain()
        self._where = [self.keychain]
        self._place = " " + _quote(self.keychain)

    def read(self):
        try:
            output = self.runner(
                ["find-generic-password", "-s", self.service, "-a", self.account, "-w", *self._where]
            )
        except SecurityError as error:
            if error.returncode == ITEM_NOT_FOUND:
                return None
            raise
        return _parse(output)

    def write(self, signin):
        compact = _compact(signin)
        digits = compact.encode("utf-8").hex()
        command = (
            f"add-generic-password -U -a {_quote(self.account)} "
            f"-s {_quote(self.service)} -X {digits}{self._place}\n"
        )
        if len(command.encode("utf-8")) <= STDIN_LIMIT:
            self.runner(["-i"], stdin=command)
        else:
            self._write_large(compact.encode("utf-8"))
        if self.read() != json.loads(compact):
            raise SlotError(
                "The Keychain did not hold what was written. Another writer may have changed it."
            )

    def _write_large(self, data):
        status, made = update_item(self.service, self.account, data, self.keychain), False
        if status == NOT_FOUND:
            self.runner(["-i"], stdin=f"add-generic-password -a {_quote(self.account)} -s {_quote(self.service)} -w {_quote('{}')}{self._place}\n")
            status, made = update_item(self.service, self.account, data, self.keychain), True
        if status != 0:
            if made:
                try:
                    self.delete()
                except SecurityError:
                    pass
            raise SlotError(KEYCHAIN_ASKS if status == ASKS else KEYCHAIN_UNSAVED)

    def delete(self):
        try:
            self.runner(["delete-generic-password", "-s", self.service, "-a", self.account, *self._where])
        except SecurityError as error:
            if error.returncode != ITEM_NOT_FOUND:
                raise


class FileSlot(Slot):
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        return _parse(text)

    def write(self, signin):
        compact = _compact(signin)
        target = Path(os.path.realpath(self.path))
        folder = target.parent
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=folder, prefix=target.name + ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(compact)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, target)
        except BaseException:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise
        try:
            dir_fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass


def _oauth(signin):
    if not isinstance(signin, dict):
        return {}
    value = signin.get("claudeAiOauth")
    return value if isinstance(value, dict) else {}


def access_token(signin):
    return _oauth(signin).get("accessToken")


def refresh_token(signin):
    return _oauth(signin).get("refreshToken")


def expires_at(signin):
    return _oauth(signin).get("expiresAt")


def scopes(signin):
    value = _oauth(signin).get("scopes")
    return list(value) if isinstance(value, list) else []
