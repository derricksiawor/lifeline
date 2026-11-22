import os
import ssl
import subprocess
import sys
import threading

MAC_BUNDLE = "/etc/ssl/cert.pem"
SECURITY = "/usr/bin/security"
SYSTEM_ROOTS = "/System/Library/Keychains/SystemRootCertificates.keychain"
EXPORT_SECONDS = 20
PEM_MARK = "-----BEGIN CERTIFICATE-----"

_lock = threading.Lock()
_built = None


def _defaults_work():
    found = ssl.get_default_verify_paths()
    return any(isinstance(place, str) and place and os.path.exists(place) for place in (found.cafile, found.capath))


def _system_roots(run):
    try:
        done = run(
            [SECURITY, "find-certificate", "-a", "-p", SYSTEM_ROOTS],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=EXPORT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    text = getattr(done, "stdout", None)
    if getattr(done, "returncode", 1) != 0 or not isinstance(text, str) or PEM_MARK not in text:
        return None
    return text


def build(system=None, run=subprocess.run):
    made = ssl.create_default_context()
    system = sys.platform if system is None else system
    if system != "darwin" or _defaults_work():
        return made
    if os.path.exists(MAC_BUNDLE):
        try:
            made.load_verify_locations(cafile=MAC_BUNDLE)
            return made
        except (OSError, ssl.SSLError, ValueError):
            pass
    roots = _system_roots(run)
    if roots is not None:
        try:
            made.load_verify_locations(cadata=roots)
        except (OSError, ssl.SSLError, ValueError):
            pass
    return made


def context():
    global _built
    with _lock:
        if _built is None:
            _built = build()
        return _built
