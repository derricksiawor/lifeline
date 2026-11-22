import os
import ssl
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from lifeline import tls

PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"


class Recorder:
    def __init__(self):
        self.loaded = []

    def load_verify_locations(self, cafile=None, capath=None, cadata=None):
        self.loaded.append({"cafile": cafile, "capath": capath, "cadata": cadata})


def paths(cafile=None, capath=None):
    return SimpleNamespace(cafile=cafile, capath=capath)


class ContextTests(unittest.TestCase):
    def build(self, defaults, present=(), run=None, system="darwin"):
        made = Recorder()
        calls = []

        def fake_run(args, **options):
            calls.append((args, options))
            return SimpleNamespace(returncode=0, stdout=PEM * 2)

        with mock.patch.object(tls.ssl, "create_default_context", return_value=made), mock.patch.object(
            tls.ssl, "get_default_verify_paths", return_value=defaults
        ), mock.patch.object(tls.os.path, "exists", side_effect=lambda path: path in present):
            found = tls.build(system=system, run=run or fake_run)
        self.assertIs(found, made)
        return made, calls

    def test_working_default_paths_are_kept_as_they_are(self):
        for defaults, present in (
            (paths(cafile="/x/cert.pem"), ("/x/cert.pem",)),
            (paths(capath="/x/certs"), ("/x/certs",)),
        ):
            with self.subTest(defaults=defaults):
                made, calls = self.build(defaults, present + (tls.MAC_BUNDLE,))
                self.assertEqual((made.loaded, calls), ([], []))

    def test_missing_default_paths_load_the_macs_own_bundle(self):
        defaults = paths(
            cafile="/Library/Frameworks/Python.framework/Versions/3.13/etc/openssl/cert.pem",
            capath="/Library/Frameworks/Python.framework/Versions/3.13/etc/openssl/certs",
        )
        made, calls = self.build(defaults, (tls.MAC_BUNDLE,))
        self.assertEqual(made.loaded, [{"cafile": "/etc/ssl/cert.pem", "capath": None, "cadata": None}])
        self.assertEqual(calls, [])

    def test_without_the_bundle_the_system_roots_are_exported(self):
        made, calls = self.build(paths(cafile="/missing/cert.pem"), ())
        self.assertEqual(made.loaded, [{"cafile": None, "capath": None, "cadata": PEM * 2}])
        args, options = calls[0]
        self.assertEqual(
            args,
            ["/usr/bin/security", "find-certificate", "-a", "-p", "/System/Library/Keychains/SystemRootCertificates.keychain"],
        )
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertTrue(options["capture_output"])
        self.assertEqual(options["timeout"], tls.EXPORT_SECONDS)

    def test_a_failed_export_leaves_the_defaults(self):
        for result in (
            SimpleNamespace(returncode=1, stdout=PEM),
            SimpleNamespace(returncode=0, stdout=""),
            OSError("no security"),
            subprocess.TimeoutExpired("security", 20),
        ):
            with self.subTest(result=type(result).__name__):

                def run(args, **options):
                    if isinstance(result, BaseException):
                        raise result
                    return result

                made, _ = self.build(paths(), (), run=run)
                self.assertEqual(made.loaded, [])

    def test_linux_keeps_its_defaults(self):
        made, calls = self.build(paths(cafile="/missing/cert.pem"), (), system="linux")
        self.assertEqual((made.loaded, calls), ([], []))

    def test_the_context_is_built_once_and_reused(self):
        built = []

        def build():
            built.append(1)
            return ssl.create_default_context()

        saved = tls._built
        self.addCleanup(setattr, tls, "_built", saved)
        tls._built = None
        with mock.patch.object(tls, "build", side_effect=build):
            first = tls.context()
            second = tls.context()
        self.assertIs(first, second)
        self.assertEqual(len(built), 1)
        self.assertIsInstance(first, ssl.SSLContext)
        self.assertEqual(first.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(first.check_hostname)

    @unittest.skipUnless(os.path.isfile(tls.MAC_BUNDLE), "this machine has no /etc/ssl/cert.pem")
    def test_exported_roots_load_into_a_real_context(self):
        with open(tls.MAC_BUNDLE, encoding="ascii", errors="replace") as handle:
            text = handle.read()

        def run(args, **options):
            return SimpleNamespace(returncode=0, stdout=text)

        with mock.patch.object(tls.ssl, "get_default_verify_paths", return_value=paths()), mock.patch.object(
            tls.os.path, "exists", return_value=False
        ):
            found = tls.build(system="darwin", run=run)
        self.assertGreater(found.cert_store_stats()["x509_ca"], 0)


if __name__ == "__main__":
    unittest.main()
