"""Lifeline can run beside another Lifeline: its own names, a port setting, an output folder, a safe status line."""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from lifeline import accounts, cli, devbox, history, ident, pairing, server, statusline, store
from tests.test_server import FakeKeychain

CORE = Path(__file__).resolve().parents[1]
ROOT = CORE.parent
OTHER = "org.me.other"
HOME = Path("/home/x")


def python(code, **env):
    clean = {key: value for key, value in os.environ.items() if not key.startswith("LIFELINE_")}
    with TemporaryDirectory() as tmp:
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp,
                              env=dict(clean, PYTHONPATH=str(CORE), HOME=tmp, **env), check=True)
    return done.stdout.strip()


class NamesTests(unittest.TestCase):
    def test_the_default_id_is_neutral_and_names_follow_it(self):
        self.assertEqual(ident.bundle_id({}), "com.example.lifeline")
        self.assertEqual(ident.data_folder(HOME, "darwin", {}), HOME / "Library/Application Support/com.example.lifeline")
        self.assertEqual(ident.data_folder(HOME, "linux", {}), HOME / ".local/share/com.example.lifeline")
        self.assertEqual(ident.signin_folder(HOME, {}), HOME / ".config/com.example.lifeline/signins")
        self.assertEqual(ident.log_folder(HOME, {}), HOME / "Library/Logs/com.example.lifeline")
        self.assertEqual(statusline.socket_path(HOME, {}, "Darwin"), str(HOME / "Library/Application Support/com.example.lifeline/lifeline.sock"))
        self.assertEqual(statusline.socket_path(HOME, {}, "Linux"), str(HOME / ".local/share/com.example.lifeline/lifeline.sock"))
        self.assertEqual(server.log_folder(HOME), ident.log_folder(HOME))
        self.assertEqual(pairing.default_path(HOME).parent, HOME / ".config" / ident.bundle_id())

    def test_nothing_collides_with_the_owners_names(self):
        taken = {"Lifeline", "lifeline", "lifeline.service"}
        for system in ("darwin", "linux"):
            self.assertNotIn(ident.data_folder(HOME, system, {}).name, taken)
        for found in (ident.signin_folder(HOME, {}).parent.name, ident.log_folder(HOME, {}).name, ident.bundle_id({})):
            self.assertNotIn(found, taken)
        for path in (accounts.default_registry_path(HOME, "Darwin"), statusline.registry_path(HOME, "Darwin")):
            self.assertNotIn("Lifeline", path.parts)
        self.assertNotEqual(store.STORE_SERVICE, "Lifeline")
        self.assertNotIn("Lifeline", history.default_path().parts)

    def test_another_id_changes_every_name(self):
        env = {"LIFELINE_BUNDLE_ID": OTHER}
        self.assertEqual(ident.data_folder(HOME, "darwin", env), HOME / f"Library/Application Support/{OTHER}")
        self.assertEqual(ident.signin_folder(HOME, env), HOME / f".config/{OTHER}/signins")
        self.assertEqual(ident.log_folder(HOME, env), HOME / f"Library/Logs/{OTHER}")
        self.assertEqual(statusline.socket_path(HOME, env, "Linux"), str(HOME / f".local/share/{OTHER}/lifeline.sock"))
        self.assertEqual(python("from lifeline import store; print(store.STORE_SERVICE)", LIFELINE_BUNDLE_ID=OTHER), OTHER)
        self.assertEqual(python("from lifeline import store; print(store.STORE_SERVICE)"), "com.example.lifeline")

    def test_the_id_can_come_from_a_file_beside_the_code(self):
        with TemporaryDirectory() as tmp, mock.patch.object(ident, "HERE", Path(tmp)):
            (Path(tmp) / "bundle-id.txt").write_text(OTHER + "\n")
            self.assertEqual(ident.bundle_id({}), OTHER)
            self.assertEqual(ident.bundle_id({"LIFELINE_BUNDLE_ID": "env.wins"}), "env.wins")

    def test_an_id_that_could_name_another_folder_falls_back_to_the_default(self):
        for bad in ("../x", "..", ".hidden", "-x", "a b", "a/b", "caf\u00e9.x"):
            with self.subTest(bad=bad):
                self.assertEqual(ident.bundle_id({"LIFELINE_BUNDLE_ID": bad}), ident.DEFAULT_ID)
        self.assertEqual(ident.bundle_id({"LIFELINE_BUNDLE_ID": "org.me.other_2-b"}), "org.me.other_2-b")

    def test_claude_codes_own_items_stay_shared(self):
        from lifeline import slot
        self.assertEqual(slot.SERVICE, "Claude Code-credentials")
        self.assertEqual(statusline.read_oauth_account(HOME / "nowhere"), None)
        self.assertIn('Path(home) / ".claude.json"', (CORE / "lifeline" / "statusline.py").read_text())


class PortTests(unittest.TestCase):
    def test_the_port_is_a_setting_with_7433_as_default(self):
        self.assertEqual(ident.port({}), 7433)
        self.assertEqual(ident.port({"LIFELINE_PORT": "7500"}), 7500)
        for bad in ("0", "70000", "abc", "-1"):
            self.assertEqual(ident.port({"LIFELINE_PORT": bad}), 7433)
        self.assertEqual(python("from lifeline import pairing; print(pairing.PORT)", LIFELINE_PORT="7500"), "7500")
        self.assertEqual(python("from lifeline import pairing; print(pairing.PORT)"), "7433")

    def test_the_address_and_the_pairing_code_carry_the_port(self):
        with mock.patch.object(pairing, "PORT", 7500):
            self.assertEqual(pairing.parse_address("studio"), ("studio", 7500))
            self.assertEqual(pairing.parse_address("studio:7600"), ("studio", 7600))
            with TemporaryDirectory() as tmp, mock.patch.object(pairing, "default_path", return_value=Path(tmp) / "pairing.json"), \
                    mock.patch.object(cli, "run_security", FakeKeychain()), mock.patch.object(cli, "_write") as write:
                cli._pair()
        self.assertRegex(write.call_args[0][1][0], r":7500\.$")


class ListenTests(unittest.TestCase):
    """The other computer listens only on the address it prints, unless told otherwise."""

    def setUp(self):
        self.keychain = FakeKeychain()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "pairing.json"
        self.runner = self.keychain if sys.platform == "darwin" else None

    def pair_lines(self, listen=None, reset=False):
        clean = {key: value for key, value in os.environ.items() if key != "LIFELINE_LISTEN"}
        environ = clean if listen is None else dict(clean, LIFELINE_LISTEN=listen)
        with mock.patch.object(pairing, "default_path", return_value=self.path), mock.patch.object(cli, "run_security", self.keychain), \
                mock.patch.object(pairing, "own_address", return_value="192.168.1.20"), mock.patch.dict(os.environ, environ, clear=True), \
                mock.patch.object(cli, "_write") as write:
            self.assertEqual(cli._pair(reset), 0)
        return write.call_args[0][1]

    def test_the_listen_address_and_claude_are_settings_with_no_default(self):
        with TemporaryDirectory() as tmp, mock.patch.object(ident, "HERE", Path(tmp)):
            self.assertEqual((ident.listen({}), ident.claude({})), ("", ""))
            (Path(tmp) / "listen.txt").write_text("10.0.0.5\n")
            (Path(tmp) / "claude.txt").write_text("/opt/claude/bin/claude\n")
            self.assertEqual((ident.listen({}), ident.claude({})), ("10.0.0.5", "/opt/claude/bin/claude"))
            self.assertEqual(ident.listen({"LIFELINE_LISTEN": "0.0.0.0"}), "0.0.0.0")
            self.assertEqual(ident.claude({"LIFELINE_CLAUDE": "/usr/local/bin/claude"}), "/usr/local/bin/claude")

    def test_pair_prints_the_address_it_listens_on(self):
        lines = self.pair_lines()
        self.assertEqual(lines[0], f"This computer is {pairing.own_name()}, at 192.168.1.20:{pairing.PORT}.")
        self.assertEqual(self.pair_lines("10.0.0.5")[0], f"This computer is {pairing.own_name()}, at 10.0.0.5:{pairing.PORT}.")
        text = " ".join(lines)
        self.assertIn("--listen", text)
        self.assertIn("VPN", text)
        self.assertIn("Tailscale is one option", text)
        self.assertNotIn("Tailscale address", text)
        self.assertNotIn("every network", text)
        self.assertNotIn("public", text)

    def test_pair_warns_when_the_port_is_open_on_every_network_or_a_public_address(self):
        lines = self.pair_lines("0.0.0.0")
        self.assertEqual(lines[0], f"This computer is {pairing.own_name()}, at 192.168.1.20:{pairing.PORT}.")
        self.assertIn("every network", lines[-1])
        self.assertIn("not encrypted", lines[-1])
        lines = self.pair_lines("8.8.8.8")
        self.assertIn("public address 8.8.8.8", lines[-1])
        for private in ("10.0.0.5", "192.168.1.20", "100.64.0.7", "build-box.local"):
            with self.subTest(private=private):
                self.assertNotIn("not encrypted", " ".join(self.pair_lines(private)))

    def test_pair_reset_cuts_off_the_mac_paired_before(self):
        self.assertEqual(cli.parse(["pair", "--reset"]).flags, {"--reset"})
        with mock.patch.object(pairing, "default_path", return_value=self.path):
            keeper = devbox.keeper(None, self.runner)
            keeper.new_code()
            keeper.pair(os.urandom(16), pairing.new_half()[1])
            self.assertIsNotNone(keeper.key())
        self.assertNotIn("no longer", " ".join(self.pair_lines()))
        with mock.patch.object(pairing, "default_path", return_value=self.path):
            self.assertIsNotNone(devbox.keeper(None, self.runner).key(), "a plain pair keeps the key")
        lines = self.pair_lines(reset=True)
        self.assertIn("no longer", lines[0])
        with mock.patch.object(pairing, "default_path", return_value=self.path):
            self.assertIsNone(devbox.keeper(None, self.runner).key())


class OutFolderTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "bundle.sh uses PlistBuddy")
    def test_bundle_sh_builds_into_the_folder_it_is_given(self):
        with TemporaryDirectory() as tmp:
            tmp = Path(tmp).resolve()
            fake = tmp / "fakebin"
            fake.mkdir()
            (fake / "swift").write_text(f'#!/bin/bash\n[ "$1" = build ] && [[ " $* " == *--show-bin-path* ]] && echo "{tmp}/built"\nexit 0\n')
            (fake / "codesign").write_text("#!/bin/bash\nexit 0\n")
            for name in ("swift", "codesign"):
                (fake / name).chmod(0o755)
            (tmp / "built").mkdir()
            (tmp / "built" / "Lifeline").write_text("")
            out = tmp / "elsewhere" / "here"
            done = subprocess.run(["bash", str(ROOT / "scripts" / "bundle.sh"), "--out", str(out)], capture_output=True, text=True,
                                  env=dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", LIFELINE_BUNDLE_ID=OTHER))
            self.assertEqual(done.returncode, 0, done.stderr)
            core = out / "Lifeline.app" / "Contents" / "Resources" / "core"
            self.assertTrue((core / "build.txt").is_file())
            self.assertEqual((core / "core" / "lifeline" / "bundle-id.txt").read_text().strip(), OTHER)
            self.assertIn(OTHER, done.stdout)
            self.assertFalse((ROOT / "build" / "Lifeline.app").exists() and (ROOT / "build" / "Lifeline.app").stat().st_mtime > out.stat().st_mtime)

    def fake_tools(self, tmp):
        fake = tmp / "fakebin"
        fake.mkdir()
        log = tmp / "codesign.log"
        (fake / "swift").write_text(f'#!/bin/bash\n[ "$1" = build ] && [[ " $* " == *--show-bin-path* ]] && echo "{tmp}/built"\nexit 0\n')
        (fake / "codesign").write_text(f'#!/bin/bash\necho "$*" >> "{log}"\nexit 0\n')
        for name in ("swift", "codesign"):
            (fake / name).chmod(0o755)
        (tmp / "built").mkdir()
        (tmp / "built" / "Lifeline").write_text("")
        return fake, log

    def test_bundle_sh_refuses_an_id_that_could_name_another_folder(self):
        with TemporaryDirectory() as tmp:
            fake, log = self.fake_tools(Path(tmp).resolve())
            for bad in ("../x", "..", "a b", "-x"):
                with self.subTest(bad=bad):
                    done = subprocess.run(["bash", str(ROOT / "scripts" / "bundle.sh"), "--out", str(Path(tmp) / "out")], capture_output=True,
                                          text=True, env=dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}", LIFELINE_BUNDLE_ID=bad))
                    self.assertEqual(done.returncode, 1)
                    self.assertIn("LIFELINE_BUNDLE_ID may use only", done.stderr)
            self.assertFalse(log.exists())

    @unittest.skipUnless(sys.platform == "darwin", "bundle.sh uses PlistBuddy")
    def test_bundle_sh_turns_on_the_hardened_runtime_with_a_real_identity(self):
        for identity, hardened in (("Developer ID Application: Example", True), (None, False)):
            with self.subTest(identity=identity), TemporaryDirectory() as tmp:
                tmp = Path(tmp).resolve()
                fake, log = self.fake_tools(tmp)
                env = dict(os.environ, PATH=f"{fake}:{os.environ['PATH']}")
                env.pop("LIFELINE_SIGN_IDENTITY", None)
                env.pop("LIFELINE_TEAM_ID", None)
                if identity:
                    env["LIFELINE_SIGN_IDENTITY"] = identity
                done = subprocess.run(["bash", str(ROOT / "scripts" / "bundle.sh"), "--out", str(tmp / "out")], capture_output=True, text=True, env=env)
                self.assertEqual(done.returncode, 0, done.stderr)
                signing = [line for line in log.read_text().splitlines() if "--sign" in line]
                self.assertEqual(len(signing), 1)
                self.assertEqual("--options runtime" in signing[0], hardened, signing[0])

    def test_bundle_sh_refuses_an_empty_out(self):
        done = subprocess.run(["bash", str(ROOT / "scripts" / "bundle.sh"), "--out"], capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("--out needs a folder", done.stderr)


class InstallerTests(unittest.TestCase):
    """The installer is never run here. Its options are checked where they stop before it changes anything."""

    script = ROOT / "devbox" / "install.sh"

    def run_script(self, *args, **env):
        return subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, env=dict(os.environ, **env))

    def test_it_takes_a_port_and_refuses_a_bad_one(self):
        for bad in ("abc", "0", "70000", ""):
            done = self.run_script("--port", bad)
            self.assertEqual(done.returncode, 1, bad)
            self.assertIn("--port needs a number", done.stderr)
        self.assertIn("Unknown option", self.run_script("--nope").stderr)

    def test_it_refuses_an_id_that_could_name_another_folder(self):
        done = self.run_script(LIFELINE_BUNDLE_ID="../x")
        self.assertEqual(done.returncode, 1)
        self.assertIn("LIFELINE_BUNDLE_ID may use only", done.stderr)

    def test_its_names_come_from_the_id_and_the_port_is_saved(self):
        text = self.script.read_text()
        for line in ('APP="$HOME/.local/share/$ID/app"', 'LABEL="$ID.remote"', 'UNIT="$HOME/.config/systemd/user/$ID.service"',
                     '"$HOME/Library/Logs/$ID"', "port.txt", "bundle-id.txt"):
            self.assertIn(line, text)
        for old in ("lifeline.service", "local.lifeline", "Logs/Lifeline", ".local/share/lifeline"):
            self.assertNotIn(old, text)
        self.assertEqual(subprocess.run(["bash", "-n", str(self.script)]).returncode, 0)


class InstallerPartsTests(unittest.TestCase):
    """Runs only the installer's two quoting helpers, never the installer itself."""

    script = ROOT / "devbox" / "install.sh"

    def helper(self, shell, name, value):
        lines = self.script.read_text().splitlines()
        helpers = "\n".join(line for line in lines if line.startswith(("shopt -u patsub_replacement", "xml() {", "word() {")))
        self.assertEqual(helpers.count("() {"), 2)
        done = subprocess.run([shell, "-c", helpers + f'\n{name} "$1"', "helper", value], capture_output=True, text=True, check=True)
        return done.stdout

    def test_any_path_is_escaped_for_the_plist_and_quoted_for_systemd(self):
        shells = {"/bin/bash", shutil.which("bash") or "/bin/bash"}
        value = '/home/A & B <x> "q" 50% $HOME \\back'
        for shell in sorted(shells):
            with self.subTest(shell=shell):
                xml = self.helper(shell, "xml", value)
                self.assertEqual(xml, '/home/A &amp; B &lt;x&gt; "q" 50% $HOME \\back')
                plist = plistlib.loads(f'<plist version="1.0"><string>{xml}</string></plist>'.encode())
                self.assertEqual(plist, value)
                self.assertEqual(self.helper(shell, "word", value), '"/home/A & B <x> \\"q\\" 50%% $$HOME \\\\back"')

    def test_it_listens_where_it_says_and_keeps_what_it_found(self):
        text = self.script.read_text()
        for part in ('--listen) LISTEN=', "listen.txt", "claude.txt", "pairing.own_address()", '"$APP/lifeline"',
                     '$(xml "$python")', '$(xml "$APP/bin/lifeline")', '$(word "$python")', '$(word "$APP/bin/lifeline")'):
            self.assertIn(part, text)
        self.assertNotRegex(text, r'LISTEN="?0\.0\.0\.0')
        self.assertNotIn("<string>$python</string>", text)
        self.assertNotIn("ExecStart=$python", text)
        self.assertEqual(subprocess.run(["bash", "-n", str(self.script)]).returncode, 0)


class StatuslineUninstallTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.keep = Path(tmp.name) / "keep"
        self.path = self.home / ".claude" / "settings.json"
        self.moment = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

    def change(self, on):
        return server.change_statusline(self.home, on, self.moment, keep=self.keep)

    def backups(self):
        return [p.read_bytes() for p in self.path.parent.iterdir() if ".lifeline-backup-" in p.name]

    def test_the_other_lifelines_entry_comes_back_exactly(self):
        entry = {"type": "command", "command": "/Applications/Lifeline.app/Contents/Resources/core/bin/lifeline-statusline",
                 "refreshInterval": 9, "padding": 1}
        before = json.dumps({"model": "opus", "statusLine": entry, "env": {"A": "b"}}, indent=2).encode() + b"\n"
        self.path.write_bytes(before)
        self.change(True)
        self.assertEqual(self.backups(), [before], "a copy of settings.json is made before the change")
        self.assertNotEqual(json.loads(self.path.read_bytes())["statusLine"], entry)
        self.change(True)
        self.assertEqual(len(self.backups()), 1, "turning it on again changes nothing and keeps the saved entry")
        self.change(False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.keep / server.STATUSLINE_PREVIOUS).exists())

    def test_with_no_entry_before_turning_off_removes_ours(self):
        before = b'{\n  "model": "opus"\n}\n'
        self.path.write_bytes(before)
        self.change(True)
        self.assertIn("statusLine", json.loads(self.path.read_bytes()))
        self.change(False)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIn(before, self.backups())

    def test_a_status_line_that_is_not_lifelines_is_still_refused(self):
        self.path.write_text(json.dumps({"statusLine": {"type": "command", "command": "/usr/bin/true"}}))
        with self.assertRaises(server.Refusal):
            self.change(True)


if __name__ == "__main__":
    unittest.main()
