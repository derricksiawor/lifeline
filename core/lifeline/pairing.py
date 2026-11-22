"""Pairing with another computer, and the signature on every request between them.

The other computer shows a one-time code. The Mac proves it knows the code, the two sides make a key with a
Diffie-Hellman exchange that the code signs, and the code stops working. Someone who records the exchange and learns
the code later still cannot work out the key. From then on every request and every reply carries an HMAC of its
content made with that key, so nothing unpaired is accepted.
"""

import hashlib
import hmac
import secrets
import socket
import threading
import time
from pathlib import Path

from lifeline import ident

PORT = ident.port()
CODE_SECONDS = 3600
SKEW_SECONDS = 300
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 20
TIME_HEADER = "X-Lifeline-Time"
NONCE_HEADER = "X-Lifeline-Nonce"
PROOF_HEADER = "X-Lifeline-Proof"
PAIR_ROUTE = "/pair"
FILE_NAME = "pairing.json"
NEWEST_NAME = "pairing-newest.json"
EVERY_NETWORK = "0.0.0.0"
# RFC 3526 group 14, the 2048-bit MODP group, with generator 2.
GROUP = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF6955817183995497CEA956AE515D2261898FA0510"
    "15728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)


def new_code():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LENGTH, 4))


def code_key(code):
    clean = "".join(ch for ch in str(code).upper() if ch in ALPHABET)
    if len(clean) != CODE_LENGTH:
        raise ValueError("not a pairing code")
    return hashlib.sha256(b"lifeline pairing\0" + clean.encode("ascii")).digest()


def new_half():
    """A secret and the half of the key made from it, which crosses the network."""
    secret = secrets.randbelow(GROUP - 3) + 2
    return secret, pow(2, secret, GROUP)


def half_from(text):
    if not isinstance(text, str) or not 0 < len(text) <= 512 or any(ch not in "0123456789abcdef" for ch in text):
        raise ValueError("not a key half")
    half = int(text, 16)
    if not 1 < half < GROUP - 1:
        raise ValueError("not a key half")
    return half


def shared_key(code_key, nonce, secret, half):
    mixed = pow(half, secret, GROUP).to_bytes(256, "big")
    return hmac.new(code_key, b"key\0" + nonce + mixed, hashlib.sha256).digest()


def proof(key, *parts):
    mac = hmac.new(key, digestmod=hashlib.sha256)
    for part in parts:
        mac.update(part if isinstance(part, bytes) else str(part).encode("utf-8"))
        mac.update(b"\0")
    return mac.hexdigest()


def request_headers(key, method, target, body):
    stamp, nonce = str(int(time.time())), secrets.token_hex(16)
    return {TIME_HEADER: stamp, NONCE_HEADER: nonce, PROOF_HEADER: proof(key, method, target, stamp, nonce, body)}


def reply_proof(key, request, status, body):
    return proof(key, "reply", request, status, body)


def same(expected, given):
    return hmac.compare_digest(expected.encode("ascii"), str(given or "").encode("utf-8", "replace"))


def own_name():
    return socket.gethostname().split(".")[0] or "Other computer"


def own_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
        except OSError:
            return socket.gethostname()


def listen_host(env=None):
    return ident.listen(env) or own_address()


def parse_address(text):
    host, sep, port = str(text or "").strip().rpartition(":")
    if not sep:
        host, port = port, str(PORT)
    if not host or any(ch.isspace() or ch in "/@[]" for ch in host) or not (port.isascii() and port.isdigit()):
        raise ValueError("not an address")
    if not 1 <= int(port) <= 65535:
        raise ValueError("not a port")
    return host, int(port)


class Keeper:
    """The other computer's side: the waiting code and the shared key, in the keychain on a Mac or else in a file only
    its owner can read. The newest time stamp it accepted sits beside that, so a restart forgets no replay."""

    def __init__(self, path, write, read, clock=time.time, slot=None):
        self.path = Path(path)
        self._write, self._read, self.clock, self._slot = write, read, clock, slot
        self._seen = {}
        self._lock = threading.Lock()
        try:
            newest = self._read(self.path.with_name(NEWEST_NAME)).get("newest")
        except Exception:
            newest = None
        self._floor = self._newest = newest if isinstance(newest, int) else 0

    def _data(self):
        try:
            data = self._slot.read() if self._slot else self._read(self.path)
        except Exception:
            data = None
        return data if isinstance(data, dict) else {}

    def _save(self, data):
        if self._slot:
            self._slot.write(data)
        else:
            self._write(self.path, data)

    def new_code(self, reset=False):
        code = new_code()
        data = {} if reset else self._data()
        data.update(code_key=code_key(code).hex(), code_expires=self.clock() + CODE_SECONDS)
        self._save(data)
        return code

    def key(self, pairing=False):
        data = self._data()
        if not pairing:
            return bytes.fromhex(data["key"]) if isinstance(data.get("key"), str) else None
        waiting = data.get("code_key")
        expires = data.get("code_expires")
        if not isinstance(waiting, str) or not isinstance(expires, (int, float)) or expires < self.clock():
            return None
        return bytes.fromhex(waiting)

    def fresh(self, headers):
        """True when the signature headers are there and the time is close. Checked before any body is read."""
        stamp = headers.get(TIME_HEADER) or ""
        return stamp.isdigit() and abs(self.clock() - int(stamp)) <= SKEW_SECONDS and all(
            headers.get(name) for name in (NONCE_HEADER, PROOF_HEADER)
        )

    def check(self, method, target, headers, body):
        """Returns the request's proof when it is signed with the right key, else None."""
        key = self.key(pairing=target == PAIR_ROUTE)
        if key is None or not self.fresh(headers):
            return None
        stamp, nonce, given = (headers.get(name) for name in (TIME_HEADER, NONCE_HEADER, PROOF_HEADER))
        if not same(proof(key, method, target, stamp, nonce, body), given):
            return None
        with self._lock:
            now, at = self.clock(), int(stamp)
            self._seen = {seen: when for seen, when in self._seen.items() if now - when <= 2 * SKEW_SECONDS}
            if given in self._seen or (target != PAIR_ROUTE and at <= self._floor):
                return None
            self._seen[given] = now
            if target != PAIR_ROUTE and at > self._newest:
                self._newest = at
                try:
                    self._write(self.path.with_name(NEWEST_NAME), {"newest": at})
                except Exception:
                    pass
        return given

    def pair(self, nonce, half):
        """Makes the shared key from the Mac's half and a new one of ours. Returns the key and our half."""
        secret, own = new_half()
        key = shared_key(self.key(pairing=True), nonce, secret, half)
        self._save({"key": key.hex()})
        self._floor = self._newest = 0
        return key, own

    def unpair(self):
        self._save({})


def default_path(home=None):
    return ident.signin_folder(home).parent / FILE_NAME
