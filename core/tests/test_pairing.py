import hashlib
import hmac
import http.server
import json
import os
import stat
import threading
import unittest
from unittest import mock

from lifeline import devbox, ident, pairing, server
from tests.test_devbox import TEST_KEY, DevboxWorld
from tests.test_server import FakeKeychain, World


def pi_bits(bits):
    """floor(pi * 2**bits), from Machin's formula in whole numbers, to check the group against RFC 3526."""
    guard = 64
    one = 1 << (bits + guard)

    def arctan_inverse(x):
        total, term, n, sign = 0, one // x, 1, 1
        while term:
            total += sign * (term // n)
            term //= x * x
            n, sign = n + 2, -sign
        return total

    return (4 * (4 * arctan_inverse(5) - arctan_inverse(239))) >> guard


class KeeperTests(unittest.TestCase):
    def setUp(self):
        self.world = DevboxWorld(self)
        self.world.pairing_path.unlink()
        self.now = [1_000_000.0]
        self.keeper = self.restarted()

    def restarted(self):
        return pairing.Keeper(self.world.pairing_path, server._write_private, server._read_private, clock=lambda: self.now[0])

    def signed(self, key, target="/account", body=b"", method="GET", at=None):
        with mock.patch.object(pairing.time, "time", return_value=self.now[0] if at is None else at):
            return pairing.request_headers(key, method, target, body)

    def paired(self):
        self.keeper.new_code()
        key, _ = self.keeper.pair(os.urandom(16), pairing.new_half()[1])
        return key

    def test_a_code_pairs_once_and_then_only_the_shared_key_works(self):
        code = self.keeper.new_code()
        self.assertRegex(code, r"^([2-9A-HJ-NP-Z]{4}-){4}[2-9A-HJ-NP-Z]{4}$")
        first = pairing.code_key(code.lower().replace("-", " "))
        self.assertIsNone(self.keeper.check("GET", "/account", self.signed(first), b""), "the code is only for pairing")
        nonce = os.urandom(16)
        secret, half = pairing.new_half()
        self.assertIsNotNone(self.keeper.check("POST", "/pair", self.signed(first, "/pair", b"{}", "POST"), b"{}"))
        key, theirs = self.keeper.pair(nonce, half)
        self.assertEqual(key, pairing.shared_key(first, nonce, secret, theirs))
        self.assertEqual(stat.S_IMODE(os.stat(self.world.pairing_path).st_mode), 0o600)
        self.assertIsNone(self.keeper.check("POST", "/pair", self.signed(first, "/pair", b"{}", "POST"), b"{}"), "the code works once")
        self.assertIsNotNone(self.keeper.check("GET", "/account", self.signed(key), b""))

    def test_someone_who_learns_the_code_later_still_cannot_work_out_the_key(self):
        code = self.keeper.new_code()
        first = pairing.code_key(code)
        nonce = os.urandom(16)
        secret, half = pairing.new_half()
        key, theirs = self.keeper.pair(nonce, half)
        self.assertNotEqual(key, hmac.new(first, b"key\0" + nonce, hashlib.sha256).digest(), "the code and the nonce alone")
        self.assertNotEqual(key, pairing.shared_key(first, nonce, pairing.new_half()[0], theirs), "a guessed secret")
        self.assertNotIn(format(secret, "x"), json.dumps(server._read_private(self.world.pairing_path)))
        for bad in ("", "0", "1", format(pairing.GROUP - 1, "x"), format(pairing.GROUP, "x"), "zz", "AB", "1" * 513, 7, None):
            with self.subTest(half=bad), self.assertRaises(ValueError):
                pairing.half_from(bad)
        self.assertEqual(pairing.half_from(format(half, "x")), half)

    def test_the_group_is_rfc_3526_group_14(self):
        expected = 2**2048 - 2**1984 - 1 + 2**64 * (pi_bits(1918) + 124476)
        self.assertEqual(pairing.GROUP, expected)
        self.assertEqual(pow(2, pairing.GROUP - 1, pairing.GROUP), 1)

    def test_nothing_unsigned_old_tampered_or_replayed_is_accepted(self):
        code = self.keeper.new_code()
        key, _ = self.keeper.pair(os.urandom(16), pairing.new_half()[1])
        self.assertIsNone(self.keeper.check("GET", "/account", {}, b""))
        headers = self.signed(key, "/registry", b'{"accounts":[]}', "POST")
        self.assertIsNone(self.keeper.check("POST", "/registry", headers, b'{"accounts":[1]}'))
        self.assertIsNone(self.keeper.check("POST", "/other", headers, b'{"accounts":[]}'))
        self.assertIsNotNone(self.keeper.check("POST", "/registry", headers, b'{"accounts":[]}'))
        self.assertIsNone(self.keeper.check("POST", "/registry", headers, b'{"accounts":[]}'), "a replay is refused")
        old = self.signed(key)
        self.now[0] += pairing.SKEW_SECONDS + 1
        self.assertIsNone(self.keeper.check("GET", "/account", old, b""))
        self.assertIsNone(self.keeper.check("GET", "/account", self.signed(pairing.code_key(code)), b""))
        self.now[0] += pairing.CODE_SECONDS
        fresh = self.keeper.new_code()
        self.now[0] += pairing.CODE_SECONDS + 1
        self.assertIsNone(self.keeper.check("POST", "/pair", self.signed(pairing.code_key(fresh), "/pair"), b""), "an hour-old code")

    def test_a_replay_after_a_restart_is_still_refused(self):
        key = self.paired()
        headers = self.signed(key, "/registry", b"{}", "POST")
        self.assertIsNotNone(self.keeper.check("POST", "/registry", headers, b"{}"))
        older = self.signed(key, at=self.now[0] - 30)
        again = self.restarted()
        self.assertIsNone(again.check("POST", "/registry", headers, b"{}"), "the same request after a restart")
        self.assertIsNone(again.check("GET", "/account", older, b""), "an older request after a restart")
        self.now[0] += 1
        self.assertIsNotNone(again.check("GET", "/account", self.signed(key), b""), "the Mac's next request")

    def test_the_headers_are_checked_before_any_body_is_read(self):
        key = self.paired()
        self.assertTrue(self.keeper.fresh(self.signed(key)))
        self.assertTrue(self.keeper.fresh(self.signed(bytes(32))), "the proof itself needs the body")
        self.assertFalse(self.keeper.fresh({}))
        self.assertFalse(self.keeper.fresh(self.signed(key, at=self.now[0] - pairing.SKEW_SECONDS - 1)))
        for missing in (pairing.TIME_HEADER, pairing.NONCE_HEADER, pairing.PROOF_HEADER):
            headers = self.signed(key)
            del headers[missing]
            self.assertFalse(self.keeper.fresh(headers), missing)

    def test_unpairing_and_a_reset_drop_the_key(self):
        key = self.paired()
        self.keeper.unpair()
        self.assertIsNone(self.keeper.key())
        self.assertIsNone(self.keeper.check("GET", "/account", self.signed(key), b""))
        key = self.paired()
        code = self.keeper.new_code(reset=True)
        self.assertIsNone(self.keeper.key())
        self.assertIsNotNone(self.keeper.check("POST", "/pair", self.signed(pairing.code_key(code), "/pair", b"{}", "POST"), b"{}"))
        self.keeper.new_code()
        self.assertIsNone(self.keeper.key())

    def test_on_a_mac_the_pairing_lives_in_the_keychain(self):
        keychain = FakeKeychain()
        keeper = devbox.keeper(self.world.home, keychain)
        code = keeper.new_code()
        key, _ = keeper.pair(os.urandom(16), pairing.new_half()[1])
        self.assertFalse(self.world.pairing_path.exists())
        saved = json.loads(keychain.items[(ident.bundle_id(), devbox.PAIRING_ACCOUNT)])
        self.assertEqual(saved, {"key": key.hex()})
        self.assertNotIn(code, json.dumps(saved))
        self.assertIsInstance(devbox.keeper(self.world.home, None).path, type(self.world.pairing_path))
        self.assertIsNone(devbox.keeper(self.world.home, None).key())

    def test_addresses_and_codes_are_checked(self):
        self.assertEqual(pairing.parse_address("build-box"), ("build-box", pairing.PORT))
        self.assertEqual(pairing.parse_address(" 192.0.2.10:8000 "), ("192.0.2.10", 8000))
        for bad in ("", "a b", "host:http", "host:0", "user@host", "http://host"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pairing.parse_address(bad)
        with self.assertRaises(ValueError):
            pairing.code_key("ABCD-EFGH")


class PairingTests(unittest.TestCase):
    def pair(self):
        mac = World(self)
        dev = DevboxWorld(self, clock=mac.clock)
        dev.pairing_path.unlink()
        dev.run(dev.service())
        code = dev.service_obj.keeper.new_code()
        service = mac.service()
        return mac, dev, service, code

    def record(self, mac):
        found = mac.keychain.items.get((ident.bundle_id(), server.COMPUTER_ACCOUNT))
        path = mac.folder / server.COMPUTER_NAME
        if found is None:
            self.assertFalse(path.exists())
            return None
        shown = server._read_private(path)
        self.assertEqual(set(shown), {"name", "address"}, "the key is not in the plain file")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        return dict(shown, **json.loads(found))

    def test_the_mac_pairs_with_the_code_and_then_reads_the_other_computer(self):
        mac, dev, service, code = self.pair()
        address = f"127.0.0.1:{dev.port()}"
        with mock.patch.object(pairing, "own_name", return_value="build-box"):
            reply = service.pair_computer({"address": address, "code": code})
        self.assertEqual(reply, {"message": "This Mac is paired with build-box now.", "name": "build-box"})
        saved = self.record(mac)
        self.assertEqual((saved["name"], saved["address"]), ("build-box", address))
        self.assertEqual(bytes.fromhex(saved["key"]), dev.service_obj.keeper.key())
        self.assertIsInstance(service.parts.link, devbox.HttpLink)
        self.assertTrue(service.parts.link.reachable())
        self.assertEqual(dev.tcp("GET", "/account", key=None)[0], 401)
        self.assertEqual(dev.tcp("GET", "/account", key=bytes(32))[0], 401)
        with self.assertRaises(server.Refusal) as caught:
            service.pair_computer({"address": address, "code": code})
        self.assertEqual(caught.exception.sentence, server.PAIR_REFUSED.format(address=address))
        service.tick()
        state = service.state()
        self.assertEqual((state["devbox"]["name"], state["devbox"]["answers"]), ("build-box", True))
        again = server.Service(mac.parts())
        self.assertEqual(again.computer_name(), "build-box")
        self.assertEqual(server.named({"a": ["The other computer uses the other computer's sign-in."]}, "build-box"),
                         {"a": ["build-box uses build-box's sign-in."]})
        self.assertEqual(service.forget_computer("forget"), {"message": "This Mac no longer reads build-box. Pair it again whenever you like."})
        self.assertIsNone(self.record(mac))
        self.assertIsNone(dev.service_obj.keeper.key(), "forget also tells the other computer to drop the key")
        self.assertIsInstance(service.parts.link, server.NullLink)
        self.assertEqual(service.forget_computer("forget"), {"message": server.NOTHING_PAIRED})
        dev.assert_clean()

    def test_forget_works_when_the_other_computer_is_gone(self):
        mac, dev, service, code = self.pair()
        with mock.patch.object(pairing, "own_name", return_value="build-box"):
            service.pair_computer({"address": f"127.0.0.1:{dev.port()}", "code": code})
        dev.service_obj.keeper.unpair()
        self.assertEqual(service.forget_computer("forget")["message"], server.FORGOTTEN.format(name="build-box"))
        self.assertIsNone(self.record(mac))

    def test_a_wrong_code_an_unreachable_address_or_a_forged_reply_pairs_nothing(self):
        mac, dev, service, code = self.pair()
        address = f"127.0.0.1:{dev.port()}"
        for body, sentence in (
            ({"address": address}, server.BAD_COMPUTER),
            ({"address": address, "code": "1234"}, server.BAD_CODE),
            ({"address": "", "code": code}, server.BAD_COMPUTER),
            ({"address": address, "code": pairing.new_code()}, server.PAIR_REFUSED.format(address=address)),
        ):
            with self.subTest(body=body), self.assertRaises(server.Refusal) as caught:
                service.pair_computer(body)
            self.assertEqual(caught.exception.sentence, sentence)
        with self.assertRaises(server.Refusal) as caught:
            service.pair_computer({"address": "127.0.0.1:1", "code": code})
        self.assertEqual(caught.exception.sentence, server.PAIR_SILENT.format(address="127.0.0.1:1"))
        other = DevboxWorld(self)
        other.run(other.service())
        self.assertEqual(devbox.HttpLink(("127.0.0.1", other.port()), key=TEST_KEY)._call_once("GET", "/account")[0], 200)
        forged = devbox.HttpLink(("127.0.0.1", other.port()), key=TEST_KEY, reply_key=os.urandom(32))
        with self.assertRaises(devbox.LinkError):
            forged._call_once("GET", "/account")
        self.assertIsNone(self.record(mac))
        self.assertIsInstance(service.parts.link, type(mac.link))

    def test_pairing_takes_only_a_nonce_and_a_good_key_half(self):
        mac, dev, service, code = self.pair()
        first = pairing.code_key(code)
        link = devbox.HttpLink(("127.0.0.1", dev.port()), key=first)
        for body in ({"nonce": "ab" * 16}, {"nonce": "ab" * 16, "half": "1"}, {"nonce": "ab" * 16, "half": "zz"}, {"nonce": "x", "half": "abc"}):
            with self.subTest(body=body):
                with self.assertRaises(devbox.LinkError):
                    link._call_once("POST", pairing.PAIR_ROUTE, body)
        self.assertIsNone(dev.service_obj.keeper.key())


class UnsignedRefusalTests(unittest.TestCase):
    """Someone between the Mac and the other computer answers with an unsigned 401 of their own."""

    BAIT = "Your Claude sign-in expired. Sign in again at https://claude-login.example."

    def setUp(self):
        bait = json.dumps({"error": self.BAIT, "job": "job-1"}).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def answer(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(bait)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(bait)

            do_GET = do_POST = answer

        self.fake = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.addCleanup(self.fake.server_close)
        self.addCleanup(self.fake.shutdown)
        self.link = devbox.HttpLink(("127.0.0.1", self.fake.server_address[1]), key=TEST_KEY)

    def test_its_words_never_reach_the_menu(self):
        with self.assertRaises(devbox.SigninRefused) as caught:
            devbox._answer(lambda: self.link.request("POST", "/registry", {"accounts": []}))
        self.assertEqual(caught.exception.sentence, devbox.DEVBOX_REFUSED)
        self.assertIsNone(caught.exception.job)
        self.assertEqual(self.link.request("GET", "/job/x"), (401, {}))

    def test_one_unsigned_refusal_does_not_ask_for_pairing_again(self):
        self.assertFalse(self.link.reachable())
        self.assertFalse(self.link.refused())
        self.assertFalse(self.link.reachable())
        self.assertFalse(self.link.refused())
        self.assertFalse(self.link.reachable())
        self.assertTrue(self.link.refused(), "several in a row do")


if __name__ == "__main__":
    unittest.main()
