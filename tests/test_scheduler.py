"""Offline scheduler tests: no network, homes, credentials, or native jobs."""

import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from agent_sync import core, scheduler as s


class FakeWindows:
    def __init__(self):
        self.tasks = {}
        self.calls = []
        self.fail_create = False
        self.partial_create = False
        self.fail_query = False
        self.fail_list = False
        self.lie_create = False

    def __call__(self, args):
        self.calls.append(args)
        output, code = b"", 0
        if args[0] == "whoami.exe":
            output = b'"synthetic-account","S-1-5-21-11-22-33-1001"\r\n'
        elif "/Create" in args:
            name = args[args.index("/TN") + 1]
            if name in self.tasks and "/F" not in args:
                code = 1
            elif self.fail_create:
                code = 1
            else:
                if not self.lie_create:
                    self.tasks[name] = Path(args[args.index("/XML") + 1]).read_bytes()
                if self.partial_create:
                    code = 1
        elif "/Delete" in args:
            name = args[args.index("/TN") + 1]
            if name not in self.tasks:
                code = 1
            else:
                del self.tasks[name]
        elif "/XML" in args:
            name = args[args.index("/TN") + 1]
            if name not in self.tasks or self.fail_query:
                code = 1
            else:
                output = self.tasks[name]
        elif "/FO" in args:
            if self.fail_list:
                code = 1
            else:
                output = "".join('"\\%s","N/A","Ready"\r\n' % name for name in self.tasks).encode()
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, output, b"secret-subprocess-detail")


# Independent expected defaults: full paths, not a copy of production internals.
WINDOWS_DEFAULTS = {
    "Task/Triggers/TimeTrigger/Repetition/StopAtDurationEnd": "false",
    "Task/Triggers/TimeTrigger/Enabled": "true",
    "Task/Principals/Principal/RunLevel": "LeastPrivilege",
    "Task/Settings/Enabled": "true",
    "Task/Settings/RunOnlyIfNetworkAvailable": "false",
    "Task/Settings/AllowStartOnDemand": "true",
    "Task/Settings/RunOnlyIfIdle": "false",
    "Task/Settings/WakeToRun": "false",
    "Task/Settings/Priority": "7",
    "Task/Settings/DisallowStartOnRemoteAppSession": "false",
    "Task/Settings/UseUnifiedSchedulingEngine": "false",
    "Task/Settings/Volatile": "false",
}


def xml_parent(root, path):
    parts = path.split("/")
    parent = root
    for part in parts[1:-1]:
        parent = parent.find("{%s}%s" % (s.NS, part))
        assert parent is not None, path
    return parent, "{%s}%s" % (s.NS, parts[-1])


def omit_windows_defaults(root):
    for path, value in WINDOWS_DEFAULTS.items():
        parent, tag = xml_parent(root, path)
        for node in list(parent):
            if node.tag == tag and (node.text or "").strip() == value:
                parent.remove(node)
    return root


def command_xml(root, codec="utf-8"):
    # Exactly the reproduced transport mismatch; ASCII uses character references.
    body = ET.tostring(root, encoding="ascii" if codec == "ascii" else "unicode",
                       xml_declaration=False)
    if isinstance(body, bytes):
        body = body.decode("ascii")
    return ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + body).encode(codec)


class FakeMac:
    def __init__(self):
        self.jobs = {}
        self.calls = []
        self.fail_bootstrap = False
        self.partial_bootstrap = False
        self.disabled = False

    def __call__(self, args):
        self.calls.append(args)
        code, text = 0, ""
        if args[1] == "bootstrap":
            path = Path(args[3])
            spec = plistlib.loads(path.read_bytes())
            if not self.fail_bootstrap:
                self.jobs[spec["Label"]] = (path, spec)
            if self.fail_bootstrap or self.partial_bootstrap:
                code = 5
        elif args[1] == "bootout":
            label = args[2].split("/", 2)[2]
            if label not in self.jobs:
                code = 113
            else:
                del self.jobs[label]
        elif args[1] == "print-disabled":
            text = "disabled services = {\n"
            if self.disabled:
                text += ''.join('  "%s" => true\n' % label for label in self.jobs)
            text += "}\n"
        elif args[1] == "print":
            if args[2] == "gui/501":
                text = "gui/501 = {\n services = {\n" + "\n".join(self.jobs) + "\n }\n}\n"
            else:
                label = args[2].split("/", 2)[2]
                if label not in self.jobs:
                    code = 113
                else:
                    path, spec = self.jobs[label]
                    text = (args[2] + " = {\n active count = 0\n path = " + str(path) +
                            "\n type = LaunchAgent\n state = not running\n program = " +
                            spec["ProgramArguments"][0] + "\n arguments = {\n" +
                            "\n".join("  " + x for x in spec["ProgramArguments"]) +
                            "\n }\n working directory = " + spec["WorkingDirectory"] +
                            "\n run interval = %d seconds\n}\n" % spec["StartInterval"])
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, text.encode(), b"secret-launchctl-detail")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # Resolve only this trusted temporary root (not supplied user paths).
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "本地 workspace & cache"
        self.repo = self.root / "私有 data & repo"
        self.workspace.mkdir()
        self.repo.mkdir()
        core.init_repo(self.repo)
        self.runtime = self.root / "运行环境 runtime"
        self.runtime.mkdir()
        self.python = self.runtime / "python.exe"
        self.python.write_bytes(b"synthetic runtime")
        (self.runtime / "pythonw.exe").write_bytes(b"synthetic hidden runtime")
        self.git = self.runtime / "git.exe"
        self.git.write_bytes(b"synthetic git; never executed")
        self.git.chmod(0o700)
        self.git_lookup = patch.object(s.shutil, "which", return_value=str(self.git)).start()
        self.private = patch.object(s, "assert_private_repo", return_value={"private": True}).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.temp.cleanup)
        self.os = FakeWindows()
        self.backend = s.WindowsBackend(command=self.os, executable=self.python)
        self.job = s.Scheduler(self.workspace, self.repo, self.backend)

    def mac(self):
        fake = FakeMac()
        backend = s.MacOSBackend(command=fake, executable=self.python,
                                launch_dir=self.root / "synthetic LaunchAgents", uid=501)
        return s.Scheduler(self.workspace, self.repo, backend), fake

    def state(self):
        return json.loads((self.workspace / "schedule/state.json").read_text("utf-8"))["record"]

    def test_default_install_readback_status_update_remove_and_reinstall(self):
        installed = self.job.install()
        name = installed["name"]
        self.assertEqual(installed["interval_minutes"], 60)
        self.assertTrue(installed["enabled"])
        self.private.assert_called_once_with(self.repo)
        creates = [a for a in self.os.calls if "/Create" in a]
        self.assertNotIn("/F", creates[0])
        status, code = self.job.status()
        self.assertEqual(code, 0)
        self.assertTrue(status["installed"])
        self.assertIsNone(status["running"])
        self.assertEqual(self.state()["runtime"], str(self.runtime / "pythonw.exe"))
        self.assertEqual(self.state()["git_executable"], str(self.git))
        before = self.state()["created_at"]
        self.job.install(17)
        self.assertEqual(self.state()["created_at"], before)
        self.assertEqual(self.job.status()[0]["interval_minutes"], 17)
        self.assertIn("/F", [a for a in self.os.calls if "/Create" in a][-1])
        self.os.tasks["UnrelatedJob"] = b"not ours"
        result = self.job.remove()
        self.assertFalse(result["installed"])
        self.assertNotIn(name, self.os.tasks)
        self.assertIn("UnrelatedJob", self.os.tasks)
        self.assertEqual(self.job.status()[0]["status"], "removed")
        self.assertTrue((self.workspace / "schedule/task.xml").is_file())
        self.job.install(10080)
        self.assertNotIn("/F", [a for a in self.os.calls if "/Create" in a][-1])
        self.assertFalse((self.workspace / "schedule/pending.json").exists())

    def test_windows_security_settings_and_quoting(self):
        workspace = 'C:\\本地 workspace\\quoted "项目𠀀" & data'
        repo = 'D:\\私有 data\\trailing\\'
        executable = 'C:\\运行环境 space\\pythonw.exe'
        source = 'C:\\程序源码 & tools'
        data = s.windows_spec("AgentSync-abcd", "owner & marker", "S-1-5-21-1001",
                              executable, workspace, repo, source)
        path = self.root / "中文候选.xml"
        path.write_bytes(data)
        saved = path.read_bytes()
        self.assertEqual(saved[:2], b"\xff\xfe")
        text = saved[2:].decode("utf-16-le")
        self.assertTrue(text.startswith("<?xml version='1.0' encoding='utf-16'?>\n"))
        for value in ("本地", "项目𠀀", "私有", "运行环境", "程序源码"):
            self.assertIn(value, text)
            self.assertIn(value.encode("utf-16-le"), saved)
        root = ET.fromstring(saved)
        utf8 = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        self.assertEqual(s._xml_semantics(saved), s._xml_semantics(utf8))
        ns = {"t": s.NS}
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns), "InteractiveToken")
        self.assertEqual(root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=ns), "LeastPrivilege")
        for tag, expected in (("Hidden", "true"), ("WakeToRun", "false"),
                              ("DisallowStartIfOnBatteries", "false"),
                              ("StopIfGoingOnBatteries", "false"),
                              ("AllowHardTerminate", "false"),
                              ("ExecutionTimeLimit", "PT0S"),
                              ("MultipleInstancesPolicy", "IgnoreNew")):
            self.assertEqual(root.findtext("t:Settings/t:" + tag, namespaces=ns), expected)
        self.assertEqual(root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns), executable)
        self.assertEqual(root.findtext("t:Actions/t:Exec/t:WorkingDirectory", namespaces=ns), source)
        args = ["-B", "-m", "agent_sync.scheduler", "run", "--workspace", workspace, "--repo", repo]
        self.assertEqual(root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns), subprocess.list2cmdline(args))
        self.assertEqual(root.findtext("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", namespaces=ns), "PT60M")
        self.assertIsNone(root.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Duration", ns))
        self.assertEqual(len(root.findall("t:Actions/*", ns)), 1)
        self.assertEqual(len(root.findall("t:Triggers/*", ns)), 1)

    def test_windows_saved_file_and_utf8_utf16_query_equivalence(self):
        name = self.job.install()["name"]
        saved = (self.workspace / "schedule/task.xml").read_bytes()
        # FakeWindows captured the actual /Create /XML file, not a rebuilt spec.
        self.assertEqual(saved, self.os.tasks[name])
        self.assertEqual(saved[:2], b"\xff\xfe")
        text = saved[2:].decode("utf-16-le")
        self.assertTrue(text.startswith("<?xml version='1.0' encoding='utf-16'?>\n"))
        for value in ("本地", "私有", "运行环境"):
            self.assertIn(value, text)
        root = ET.fromstring(saved)
        ns = {"t": s.NS}
        self.assertEqual(root.findtext("t:Actions/t:Exec/t:Command", namespaces=ns),
                         str(self.runtime / "pythonw.exe"))
        expected_args = subprocess.list2cmdline(s._argv(
            self.runtime / "pythonw.exe", self.workspace, self.repo)[1:])
        self.assertEqual(root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=ns), expected_args)
        utf8 = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        expected = s._xml_semantics(saved)
        exports = (utf8, b"\xef\xbb\xbf" + utf8, saved,
                   b"\xfe\xff" + text.encode("utf-16-be"),
                   text.encode("utf-16-le"), text.encode("utf-16-be"))
        for raw in exports:
            with self.subTest(prefix=raw[:8]):
                self.assertEqual(s._xml_semantics(raw), expected)
                self.os.tasks[name] = raw
                self.assertEqual(self.backend.query(name)["raw"], raw)
                self.assertEqual(self.job.status()[1], 0)
        self.assertFalse(self.job.remove()["installed"])

    def test_windows_dtd_rejected_in_utf8_and_utf16_before_expansion(self):
        declarations = (
            ('<!DOCTYPE Task>', ''),
            ('<!DOCTYPE Task [<!ENTITY payload "中文">]>', '&payload;'),
            ('<!DOCTYPE Task SYSTEM "file:///must-not-be-read.dtd">', ''),
            ('<!DOCTYPE Task PUBLIC "synthetic" "https://invalid.example/task.dtd">', ''),
            ('<!DOCTYPE Task [<!ENTITY payload SYSTEM "file:///must-not-be-read.txt">]>',
             '&payload;'),
            # Would fail on recursive expansion if the DTD were allowed through.
            ('<!DOCTYPE Task [<!ENTITY payload "&payload;">]>', '&payload;'),
            ('<!DOCTYPE Task [<!ENTITY % remote SYSTEM "file:///must-not-be-read.dtd">'
             '%remote;]>', ''),
        )
        encodings = (("utf-8", "utf-8", b""),
                     ("utf-8", "utf-8", b"\xef\xbb\xbf"),
                     ("utf-16-le", "utf-16", b"\xff\xfe"),
                     ("utf-16-be", "utf-16", b"\xfe\xff"),
                     ("utf-16-le", "utf-16", b""),
                     ("utf-16-be", "utf-16", b""))
        for dtd, content in declarations:
            for codec, declaration, bom in encodings:
                with self.subTest(dtd=dtd, codec=codec, bom=bom):
                    text = ('<?xml version="1.0" encoding="%s"?>\n%s\n'
                            '<Task xmlns="%s"><RegistrationInfo><Description>%s'
                            '</Description></RegistrationInfo></Task>' %
                            (declaration, dtd, s.NS, content))
                    raw = bom + text.encode(codec)
                    # Require the doctype rejection, not an incidental parse error.
                    with self.assertRaisesRegex(s.Review, "不允许 DTD"):
                        s._xml_semantics(raw)
                    self.os.tasks["synthetic-dtd"] = raw
                    with self.assertRaisesRegex(s.Review, "不允许 DTD"):
                        self.backend.query("synthetic-dtd")

    def test_windows_misdeclared_stdout_defaults_query_verify_status_and_local_guard(self):
        name = self.job.install()["name"]
        spec_path = self.workspace / "schedule/task.xml"
        saved = spec_path.read_bytes()
        root = omit_windows_defaults(ET.fromstring(saved))
        for codec in ("ascii", "utf-8"):
            with self.subTest(codec=codec):
                raw = command_xml(root, codec)
                self.assertTrue(raw.startswith(b'<?xml version="1.0" encoding="UTF-16"?>'))
                self.assertNotIn(b"\x00", raw)
                if codec == "utf-8":
                    self.assertIn("本地".encode("utf-8"), raw)
                with self.assertRaises(s.Review):
                    s._xml_semantics(raw)
                self.os.tasks[name] = raw
                live = self.backend.query(name)
                self.assertIs(live["raw"], raw)
                self.assertIs(live["enabled"], True)
                self.backend.verify(live, saved, None)
                status, code = self.job.status()
                self.assertEqual(code, 0)
                self.assertTrue(status["enabled"])
                self.assertEqual(spec_path.read_bytes(), saved)
                # The transport exception must not apply to the spec operand.
                with self.assertRaises(s.Review):
                    self.backend.verify(live, raw, None)
                state = (self.workspace / "schedule/state.json").read_bytes()
                spec_path.write_bytes(raw)
                before = len(self.os.calls)
                self.assertEqual(self.job.status()[1], 2)
                for operation in (lambda: self.job.install(30), self.job.remove):
                    with self.assertRaises(s.Review):
                        operation()
                self.assertEqual(spec_path.read_bytes(), raw)
                self.assertEqual((self.workspace / "schedule/state.json").read_bytes(), state)
                self.assertFalse(any("/Create" in a or "/Delete" in a
                                     for a in self.os.calls[before:]))
                spec_path.write_bytes(saved)

    def test_windows_defaults_are_exact_path_singletons_with_no_extras(self):
        spec = s.windows_spec("AgentSync-synthetic", "synthetic-owner", "S-1-5-21-11-22-33-1001",
                              "C:/synthetic/pythonw.exe", "C:/synthetic/workspace",
                              "D:/synthetic/repo", "C:/synthetic/source")
        root = omit_windows_defaults(ET.fromstring(spec))
        boundary = root.find("{%s}Triggers/{%s}TimeTrigger/{%s}StartBoundary" % ((s.NS,) * 3))
        boundary.text = "2032-04-05T01:02:03Z"
        base = ET.tostring(root)
        expected = s._xml_semantics(base)
        opposite = {"true": "false", "false": "true", "7": "8",
                    "LeastPrivilege": "HighestAvailable"}
        for path, value in WINDOWS_DEFAULTS.items():
            with self.subTest(path=path):
                good = ET.fromstring(base)
                parent, tag = xml_parent(good, path)
                ET.SubElement(parent, tag).text = value
                self.assertEqual(s._xml_semantics(ET.tostring(good)), expected)
                self.os.tasks["synthetic-default"] = command_xml(good)
                self.backend.verify(self.backend.query("synthetic-default"), base, None)
            for edit in ("opposite", "duplicate", "foreign", "attribute", "child"):
                with self.subTest(path=path, edit=edit):
                    changed = ET.fromstring(base)
                    parent, tag = xml_parent(changed, path)
                    if edit == "foreign":
                        tag = "{urn:synthetic:foreign}" + tag.split("}")[1]
                    node = ET.SubElement(parent, tag)
                    node.text = opposite[value] if edit == "opposite" else value
                    if edit == "duplicate":
                        ET.SubElement(parent, tag).text = value
                    elif edit == "attribute":
                        node.set("synthetic", "edit")
                    elif edit == "child":
                        ET.SubElement(node, "{%s}Unknown" % s.NS)
                    raw = ET.tostring(changed)
                    if edit in {"duplicate", "foreign"}:
                        with self.assertRaises(s.Review):
                            s._xml_semantics(raw)
                    else:
                        self.assertNotEqual(s._xml_semantics(raw), expected)
                    self.os.tasks["synthetic-default"] = command_xml(changed)
                    with self.assertRaises(s.Review):
                        live = self.backend.query("synthetic-default")
                        self.backend.verify(live, base, None)
            # Compare wrong paths against a baseline containing the wrong parent.
            wrong = ET.fromstring(base)
            parent = wrong.find("{%s}RegistrationInfo" % s.NS)
            for part in path.split("/")[1:-1]:
                parent = ET.SubElement(parent, "{%s}%s" % (s.NS, part))
            before = s._xml_semantics(ET.tostring(wrong))
            ET.SubElement(parent, "{%s}%s" % (s.NS, path.split("/")[-1])).text = value
            self.assertNotEqual(s._xml_semantics(ET.tostring(wrong)), before, path)
        for parent_tag in ("RegistrationInfo", "Settings"):
            for tag in ("Unknown", "Date", "Author"):
                changed = ET.fromstring(base)
                parent = changed.find("{%s}%s" % (s.NS, parent_tag))
                ET.SubElement(parent, "{%s}%s" % (s.NS, tag)).text = "synthetic"
                self.assertNotEqual(s._xml_semantics(ET.tostring(changed)), expected)
                self.os.tasks["synthetic-default"] = command_xml(changed)
                with self.assertRaises(s.Review):
                    self.backend.verify(self.backend.query("synthetic-default"), base, None)
        equivalent = ET.fromstring(base)
        equivalent.find("{%s}Triggers/{%s}TimeTrigger/{%s}StartBoundary" % ((s.NS,) * 3)).text = "2032-04-05T09:02:03+08:00"
        equivalent.find("{%s}Triggers/{%s}TimeTrigger/{%s}Repetition/{%s}Interval" % ((s.NS,) * 4)).text = "PT1H"
        self.assertEqual(s._xml_semantics(ET.tostring(equivalent)), expected)

    def test_windows_chinese_local_codepage_command_decode_is_strict(self):
        # Emulate a Chinese Windows ACP offline, without replacing _text.
        class ACPBytes(bytes):
            def decode(self, encoding="utf-8", errors="strict"):
                if encoding == "mbcs":
                    encoding = "gbk"
                return super().decode(encoding, errors)

        spec = s.windows_spec("AgentSync-synthetic", "synthetic-owner", "S-1-5-21-11-22-33-1001",
                              "C:/合成/pythonw.exe", "C:/合成/workspace",
                              "D:/合成/repo", "C:/合成/source")
        root = ET.fromstring(spec)
        root.find("{%s}Triggers/{%s}TimeTrigger/{%s}StartBoundary" % ((s.NS,) * 3)).text = "2032-04-05T01:02:03Z"
        spec = ET.tostring(root, encoding="utf-16")
        raw = ACPBytes(command_xml(omit_windows_defaults(root), "gbk"))
        self.os.tasks["synthetic-acp"] = raw
        with patch.object(s.os, "name", "nt"):
            self.assertIn("合成", s._text(raw))
            live = self.backend.query("synthetic-acp")
            self.assertIs(live["raw"], raw)
            self.backend.verify(live, spec, None)
            with self.assertRaises(s.Review):
                s._text(ACPBytes(b"\x81"))
        with self.assertRaises(s.Review):
            s._xml_semantics(raw)
        for malformed in (b"\xff\xfe<", b"<\x00?", b"\x00<?"):
            with self.assertRaises(s.Review):
                s._text(malformed)

    def test_windows_decoded_misdeclared_command_still_rejects_dtd(self):
        for codec in ("ascii", "utf-8"):
            for dtd in ('<!DOCTYPE Task>',
                        '<!DOCTYPE Task SYSTEM "file:///synthetic-never-read.dtd">',
                        '<!DOCTYPE Task [<!ENTITY payload "synthetic">]>'):
                text = ('<?xml version="1.0" encoding="UTF-16"?>\n' + dtd +
                        '<Task xmlns="' + s.NS + '"/>')
                raw = text.encode(codec)
                self.os.tasks["synthetic-dtd"] = raw
                with self.assertRaisesRegex(s.Review, "不允许 DTD"):
                    self.backend.query("synthetic-dtd")
                with self.assertRaisesRegex(s.Review, "不允许 DTD"):
                    self.backend.verify({"raw": raw}, b"unused", None)

    def test_windows_malformed_mixed_encoding_is_review(self):
        raw = s.windows_spec("AgentSync-test", "owner", "S-1-5-21-1001",
                             self.python, self.workspace, self.repo, s.SOURCE_ROOT)
        with self.assertRaises(s.Review):
            s._xml_semantics(raw + b"\n<!-- edited -->")

    def test_mac_builder_special_characters_and_default(self):
        workspace = '/synthetic/local "quoted" & spaces'
        repo = "/synthetic/private repo 'quoted'"
        data = s.macos_spec("com.agentsync.sync.test", "marker", "/runtime/python", workspace,
                            repo, "/public/program")
        value = plistlib.loads(data)
        self.assertEqual(value["StartInterval"], 3600)
        self.assertFalse(value["RunAtLoad"])
        self.assertEqual(value["ProcessType"], "Background")
        self.assertEqual(value["ProgramArguments"], ["/runtime/python", "-B", "-m", "agent_sync.scheduler",
                                                    "run", "--workspace", workspace, "--repo", repo])
        self.assertNotIn("StandardErrorPath", value)

    def test_mac_install_status_update_remove(self):
        job, fake = self.mac()
        first = job.install()
        external = Path(self.state()["external"])
        self.assertTrue(external.is_file())
        self.assertEqual(first["interval_minutes"], 60)
        self.assertEqual(job.status()[0]["running"], False)
        job.install(3)
        self.assertEqual(plistlib.loads(external.read_bytes())["StartInterval"], 180)
        calls = [a[1] for a in fake.calls]
        self.assertIn("bootout", calls)
        self.assertEqual(job.status()[1], 0)
        job.remove()
        self.assertFalse(external.exists())
        self.assertFalse(fake.jobs)
        self.assertEqual(job.status()[0]["installed"], False)

    def test_mac_modified_file_live_action_and_disabled_are_not_overwritten(self):
        job, fake = self.mac()
        job.install()
        external = Path(self.state()["external"])
        original = external.read_bytes()
        external.write_bytes(original + b"\n<!-- user edit -->\n")
        calls = len(fake.calls)
        with self.assertRaises(s.Review):
            job.remove()
        self.assertNotIn("bootout", [a[1] for a in fake.calls[calls:]])
        self.assertIn(b"user edit", external.read_bytes())
        external.write_bytes(original)
        fake.disabled = True
        self.assertEqual(job.status()[1], 2)
        with self.assertRaises(s.Review):
            job.install(15)
        fake.disabled = False
        name = self.state()["name"]
        fake.jobs[name][1]["ProgramArguments"] = ["/unrelated/program"]
        with self.assertRaises(s.Review):
            job.remove()

    def test_windows_export_normalization_is_accepted_but_extra_actions_are_not(self):
        name = self.job.install()["name"]
        root = ET.fromstring(self.os.tasks[name])
        interval = root.find("{%s}Triggers/{%s}TimeTrigger/{%s}Repetition/{%s}Interval" % ((s.NS,) * 4))
        interval.text = "PT1H"
        settings = root.find("{%s}Settings" % s.NS)
        ET.SubElement(settings, "{%s}UseUnifiedSchedulingEngine" % s.NS).text = "false"
        self.os.tasks[name] = ET.tostring(root)
        self.assertEqual(self.job.status()[1], 0)
        actions = root.find("{%s}Actions" % s.NS)
        ET.SubElement(actions, "{%s}Exec" % s.NS)
        edited = ET.tostring(root)
        self.os.tasks[name] = edited
        with self.assertRaises(s.Review):
            self.job.remove()
        self.assertEqual(self.os.tasks[name], edited)

    def test_invalid_intervals_and_paths_fail_without_writes(self):
        for interval in (0, -1, 10081, True, "60"):
            with self.assertRaises(core.Error):
                self.job.install(interval)
        for workspace, repo in ((self.workspace, self.workspace),
                                (self.workspace, self.workspace / "sub"),
                                (s.SOURCE_ROOT, self.repo),
                                (s.SOURCE_ROOT.parent, self.repo),
                                (Path("relative"), self.repo),
                                (self.workspace / ".." / self.workspace.name, self.repo)):
            with self.subTest(workspace=workspace, repo=repo):
                with self.assertRaises(core.Error):
                    s.Scheduler(workspace, repo, self.backend)
        self.private.assert_not_called()
        self.assertFalse((self.workspace / "schedule").exists())
        self.assertFalse(self.os.calls)

    def test_uninitialized_and_non_directory_paths(self):
        (self.repo / ".agent-sync/baseline.json").unlink()
        with self.assertRaises(core.Error):
            s.Scheduler(self.workspace, self.repo, self.backend)
        with self.assertRaises(core.Error):
            s.Scheduler(self.python, self.repo, self.backend)
        self.assertFalse((self.workspace / "schedule").exists())

    def test_private_validation_failure_precedes_all_writes_and_registration(self):
        self.private.side_effect = core.Error("secret-private-validator-detail")
        with self.assertRaises(core.Error):
            self.job.install()
        self.assertFalse((self.workspace / "schedule").exists())
        self.assertFalse(self.os.calls)

    def test_missing_pythonw_fails_without_registration(self):
        (self.runtime / "pythonw.exe").unlink()
        with self.assertRaises(s.Review):
            self.job.install()
        self.assertFalse((self.workspace / "schedule").exists())
        self.assertFalse(self.os.calls)

    def test_collision_and_local_modified_spec_preserved(self):
        name, _ = s.managed_identity(self.workspace, self.repo, "win32")
        foreign = s.windows_spec(name, "some other owner", "S-1-5-21-1001", self.python,
                                 self.workspace, self.repo, s.SOURCE_ROOT)
        self.os.tasks[name] = foreign
        with self.assertRaises(s.Review):
            self.job.install()
        self.assertEqual(self.os.tasks[name], foreign)
        self.assertFalse(any("/Create" in a for a in self.os.calls))
        del self.os.tasks[name]
        self.job.install()
        spec = self.workspace / "schedule/task.xml"
        # Deliberately retain the mixed-encoding tamper: UTF-8 suffix on UTF-16.
        changed = spec.read_bytes() + b"\n<!-- edited -->"
        spec.write_bytes(changed)
        before = len(self.os.calls)
        live = self.os.tasks[name]
        for operation in (lambda: self.job.install(30), self.job.remove):
            with self.assertRaises(s.Review):
                operation()
        self.assertEqual(spec.read_bytes(), changed)
        self.assertEqual(self.os.tasks[name], live)
        self.assertFalse(any("/Create" in a or "/Delete" in a
                             for a in self.os.calls[before:]))

    def test_query_errors_are_not_absence(self):
        self.os.fail_list = True
        with self.assertRaises(s.Review):
            self.job.install()
        self.assertFalse(any("/Create" in a for a in self.os.calls))
        self.os.fail_list = False
        name = self.job.install()["name"]
        self.os.fail_query = True
        with self.assertRaises(s.Review):
            self.job.remove()
        self.assertIn(name, self.os.tasks)
        self.assertEqual(self.job.status()[1], 2)

    def test_failed_and_partially_failed_registration_never_adopted(self):
        self.os.partial_create = True
        with self.assertRaises(s.Review):
            self.job.install()
        self.assertFalse((self.workspace / "schedule/state.json").exists())
        self.assertTrue((self.workspace / "schedule/pending.json").exists())
        self.assertEqual(len(self.os.tasks), 1)
        status, code = self.job.status()
        self.assertEqual(code, 2)
        self.assertIsNone(status["installed"])
        self.os.partial_create = False
        before = len(self.os.calls)
        with self.assertRaises(s.Review):
            self.job.install()
        self.assertFalse(any("/Create" in a for a in self.os.calls[before:]))

    def test_success_exit_without_real_registration_is_failure(self):
        self.os.lie_create = True
        with self.assertRaises(s.Review):
            self.job.install()
        self.assertFalse((self.workspace / "schedule/state.json").exists())
        self.assertEqual(self.job.status()[1], 2)

    def test_failed_update_retains_old_committed_state_and_spec(self):
        self.job.install()
        state = (self.workspace / "schedule/state.json").read_bytes()
        spec = (self.workspace / "schedule/task.xml").read_bytes()
        self.os.fail_create = True
        with self.assertRaises(s.Review):
            self.job.install(2)
        self.assertEqual((self.workspace / "schedule/state.json").read_bytes(), state)
        self.assertEqual((self.workspace / "schedule/task.xml").read_bytes(), spec)
        self.assertEqual(self.job.status()[1], 2)

    def test_mac_failed_bootstrap_keeps_evidence_not_success(self):
        job, fake = self.mac()
        fake.partial_bootstrap = True
        with self.assertRaises(s.Review):
            job.install()
        self.assertFalse((self.workspace / "schedule/state.json").exists())
        self.assertTrue((self.workspace / "schedule/pending.json").exists())
        self.assertEqual(job.status()[1], 2)
        self.assertTrue(fake.jobs)

    def test_run_calls_bridge_once_and_preserves_nonzero_result(self):
        raw = {"status": "conflict", "message": "secret-bridge-message",
               "source_files": 8, "unique_source_reads": 4, "elapsed_seconds": 0.5,
               "uploaded": False, "source_write_count": 0,
               "changed_files": ["secret-file-name"], "direct_pull_files": 0}
        with patch.object(s, "bridge_run", return_value=(raw, 2)) as bridge:
            result, code = self.job.run_once()
        bridge.assert_called_once_with(self.workspace, self.repo, check=False)
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["uploaded"])
        self.assertEqual(result["source_write_count"], 0)
        self.assertEqual(result["changed_files_count"], 1)
        latest = json.loads((self.workspace / "schedule/latest-run.json").read_text("utf-8"))
        self.assertEqual(latest, result)
        self.assertFalse((self.workspace / "schedule/run.lock").exists())
        self.assertFalse((self.workspace / "schedule/management.lock").exists())
        for path in (self.workspace / "schedule").iterdir():
            self.assertNotIn(b"secret-", path.read_bytes())
        self.assertFalse(self.os.calls)
        self.private.assert_not_called()  # The real bridge owns per-run privacy checks.

    def test_duplicate_lock_preserves_previous_success_and_does_not_sync(self):
        with patch.object(s, "bridge_run", return_value=({"status": "success"}, 0)):
            self.assertEqual(self.job.run_once()[1], 0)
        latest = (self.workspace / "schedule/latest-run.json").read_bytes()
        lock = self.workspace / "schedule/run.lock"
        lock.write_bytes(b"another invocation\n")
        with patch.object(s, "bridge_run") as bridge:
            result, code = self.job.run_once()
        bridge.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "busy")
        self.assertEqual(lock.read_bytes(), b"another invocation\n")
        self.assertEqual((self.workspace / "schedule/latest-run.json").read_bytes(), latest)
        with self.assertRaises(s.Busy):
            self.job.install()

    def test_hardlinked_git_runtime_path_is_injected_and_restored(self):
        os.link(self.git, self.root / "git-hardlink.exe")
        self.assertEqual(self.git.lstat().st_nlink, 2)
        self.assertEqual(s._validate_git(str(self.git)), self.git)
        with self.assertRaises(core.Error):
            s._ordinary(self.git)
        for installed in (False, True):
            if installed:
                self.job.install()
            for original in ("minimal-system-path", "", None):
                for fails in (False, True):
                    with self.subTest(installed=installed, original=original, fails=fails):
                        self.git_lookup.reset_mock()
                        with patch.dict(os.environ):
                            if original is None:
                                os.environ.pop("PATH", None)
                            else:
                                os.environ["PATH"] = original

                            def bridge(workspace, repo, check=False):
                                expected = str(self.git.parent)
                                if original:
                                    expected += os.pathsep + original
                                self.assertEqual(os.environ["PATH"], expected)
                                self.assertFalse((workspace / "quick-sync.lock").exists())
                                if fails:
                                    raise RuntimeError("secret-environment-error")
                                return {"status": "up_to_date"}, 0

                            with patch.object(s, "bridge_run", side_effect=bridge) as mocked:
                                result, code = s.Scheduler(self.workspace, self.repo).run_once()
                            mocked.assert_called_once_with(self.workspace, self.repo, check=False)
                            self.assertEqual(code, 2 if fails else 0)
                            self.assertEqual(result["status"], "needs_review" if fails else "up_to_date")
                            self.assertEqual(os.environ.get("PATH"), original)
                            self.assertNotIn("secret-", json.dumps(result))
                        if installed:
                            self.git_lookup.assert_not_called()
                        else:
                            self.git_lookup.assert_called_once_with("git")

    def test_git_rejects_directory_endpoint_and_symlink_ancestor(self):
        with self.assertRaises(s.Review):
            s._validate_git(str(self.runtime))
        linked_parent = self.root / "linked-runtime"
        linked_parent.symlink_to(self.runtime, target_is_directory=True)
        self.assertTrue(linked_parent.is_symlink())
        with self.assertRaises(s.Review):
            s._validate_git(str(linked_parent / self.git.name))

    def test_stale_git_blocks_run_but_not_status_or_removal(self):
        self.job.install()
        state_path = self.workspace / "schedule/state.json"
        original = state_path.read_bytes()
        for invalid in (None, "relative/git", str(self.git) + "\n"):
            state = self.state()
            state["git_executable"] = invalid
            state_path.write_bytes(s._json_bytes(s._envelope(state)))
            with self.assertRaises(s.Review):
                self.job._state()
            state_path.write_bytes(original)
        state = self.state()
        del state["git_executable"]
        state_path.write_bytes(s._json_bytes(s._envelope(state)))
        with self.assertRaises(s.Review):
            self.job._state()
        state_path.write_bytes(original)

        self.git.unlink()
        for linked in (False, True):
            if linked:
                target = self.root / "replacement-git.exe"
                target.write_bytes(b"never executed")
                target.chmod(0o700)
                self.git.symlink_to(target)
                self.assertTrue(self.git.is_symlink())
            with patch.object(s, "bridge_run") as bridge:
                result, code = self.job.run_once()
            bridge.assert_not_called()
            self.assertEqual(code, 2)
            self.assertIn("Git", result["message"])
            self.assertNotIn(str(self.git), json.dumps(result))
            latest = json.loads((self.workspace / "schedule/latest-run.json").read_text("utf-8"))
            self.assertEqual(latest["exit_code"], 2)
            self.assertIn("Git", latest["message"])
            self.assertEqual(self.job.status()[1], 0)
        self.assertFalse(self.job.remove()["installed"])

    def test_headless_main_records_controlled_status_and_actual_exit_code(self):
        args = ["run", "--workspace", str(self.workspace), "--repo", str(self.repo)]
        for status, expected in (("up_to_date", 0), ("checked", 0), ("changes_pending", 0),
                                 ("remote_changed", 2), ("needs_skill_install_review", 2),
                                 ("busy", 7)):
            with self.subTest(status=status):
                raw = {"status": status, "message": "secret-message", "error": "secret-error"}
                with patch.object(s.sys, "stdout", None), patch.object(
                        s, "bridge_run", return_value=(raw, expected)), patch("builtins.print") as output:
                    self.assertEqual(s.main(args), expected)
                output.assert_not_called()
                latest = json.loads((self.workspace / "schedule/latest-run.json").read_text("utf-8"))
                self.assertEqual(latest["status"], status)
                self.assertEqual(latest["exit_code"], expected)
                self.assertNotIn("secret-", json.dumps(latest))

    def test_lock_replacement_is_not_removed(self):
        self.job._mkdir()
        lock = self.workspace / "schedule/run.lock"
        with s._exclusive(lock):
            replacement = self.workspace / "replacement"
            replacement.write_bytes(b"replacement lock\n")
            os.replace(replacement, lock)
        self.assertEqual(lock.read_bytes(), b"replacement lock\n")

    def test_exception_redaction_and_log_rotation(self):
        with patch.object(s, "bridge_run", side_effect=RuntimeError("secret-exception-detail")):
            result, code = self.job.run_once()
        self.assertEqual(code, 2)
        self.assertNotIn("secret", json.dumps(result))
        # A small test-only limit exercises production rotation without large files.
        with patch.object(s, "LOG_LIMIT", 160):
            with patch.object(s, "bridge_run", return_value=({"status": "ok"}, 0)):
                for _ in range(12):
                    self.assertEqual(self.job.run_once()[1], 0)
        directory = self.workspace / "schedule"
        self.assertTrue((directory / "runs.log.2").exists())
        for name in ("runs.log", "runs.log.1", "runs.log.2"):
            self.assertLessEqual((directory / name).stat().st_size, 160)
            self.assertNotIn(b"secret", (directory / name).read_bytes())
        self.assertLess((directory / "latest-run.json").stat().st_size, 8192)

    def test_modified_logs_are_not_overwritten(self):
        with patch.object(s, "bridge_run", return_value=({"status": "ok"}, 0)):
            self.job.run_once()
        log = self.workspace / "schedule/runs.log"
        log.write_bytes(b"user annotation\n")
        with patch.object(s, "bridge_run") as bridge:
            self.assertEqual(self.job.run_once()[1], 2)
        bridge.assert_not_called()
        self.assertEqual(log.read_bytes(), b"user annotation\n")

    def test_hardlinked_generated_state_rejected_without_security_mock(self):
        self.job._mkdir()
        original = self.root / "ordinary.json"
        original.write_bytes(b"{}")
        os.link(original, self.workspace / "schedule/state.json")
        self.assertEqual(original.lstat().st_nlink, 2)
        with self.assertRaises(core.Error):
            s.Scheduler(self.workspace, self.repo, self.backend)
        with patch.object(s, "bridge_run") as bridge:
            result, code = self.job.run_once()
        bridge.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(original.read_bytes(), b"{}")

    def test_status_is_read_only_before_install(self):
        result, code = self.job.status()
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "not_installed")
        self.assertFalse((self.workspace / "schedule").exists())

    def test_cli_json_default_custom_invalid_and_redaction(self):
        base = ["--workspace", str(self.workspace), "--repo", str(self.repo)]
        with patch.object(s, "native_backend", return_value=self.backend):
            for tail, expected in (([], 60), (["--interval-minutes", "9"], 9)):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = s.main(["install"] + base + tail)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out.getvalue())["interval_minutes"], expected)
            for args in (["install"] + base + ["--interval-minutes", "0"],
                         ["install"] + base + ["--interval-minutes", "not-an-integer"],
                         ["status", "--workspace", str(self.workspace)]):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = s.main(args)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(out.getvalue())["status"], "needs_review")
                self.assertEqual(err.getvalue(), "")
            self.private.side_effect = core.Error("secret-validator-output")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = s.main(["install"] + base)
            self.assertEqual(code, 2)
            self.assertNotIn("secret", out.getvalue())


if __name__ == "__main__":
    unittest.main()
