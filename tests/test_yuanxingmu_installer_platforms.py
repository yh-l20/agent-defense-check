"""Candidate admission tests; mocked distro facts are not native Debian acceptance."""
from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from install import check_reuse
from install.yxm_setup import setup as installer


class InstallerPlatformArgumentTests(unittest.TestCase):
    def test_cli_passes_explicit_experiment_to_install(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(installer, "install", return_value={"features": []}) as install, \
                mock.patch.object(installer.os, "umask"), redirect_stdout(io.StringIO()):
            root = Path(temporary) / "new-install"
            self.assertEqual(installer.main(["--install-root", str(root), "--experimental-debian13", "--no-shortcut"]), 0)
            self.assertTrue(install.call_args.kwargs["experimental_debian13"])
            self.assertFalse(install.call_args.kwargs["system_deps"])
            self.assertFalse(root.exists())

    def test_reuse_forwards_only_the_explicit_experiment(self):
        normal = check_reuse.installer_command("candidate.pyz", "installation")
        experimental = check_reuse.installer_command("candidate.pyz", "installation", experimental_debian13=True)
        self.assertNotIn("--experimental-debian13", normal)
        self.assertEqual(experimental, normal + ["--experimental-debian13"])
        self.assertNotIn("--system-deps", experimental)
        self.assertNotIn("--development-wheel", experimental)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux path and receipt semantics")
class InstallerPlatformAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.base = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="yxm-platform-test-")))
        self.home = self.base / "home"
        self.home.mkdir()
        self.root = self.home / "candidate"
        self.release = self.base / "os-release"
        self.release.write_text('ID=debian\nVERSION_ID="13"\n')
        self.bwrap = self.base / "fixture-bwrap"
        self.bwrap.write_bytes(b"synthetic binary; never executed")
        def path_factory(*parts):
            if len(parts) == 1 and str(parts[0]) == "/etc/os-release":
                return self.release
            if len(parts) == 1 and str(parts[0]) == "/usr/bin/bwrap":
                return self.bwrap
            return Path(*parts)
        factory = mock.Mock(side_effect=path_factory)
        factory.home.return_value = self.home
        self.stack.enter_context(mock.patch.object(installer, "Path", factory))
        self.system = SimpleNamespace(platform="linux", version_info=(3, 13, 5), executable="/usr/bin/python3")
        self.stack.enter_context(mock.patch.object(installer, "sys", self.system))
        self.machine = self.stack.enter_context(mock.patch.object(installer.platform, "machine", return_value="x86_64"))
        self.uid = self.stack.enter_context(mock.patch.object(installer.os, "geteuid", return_value=1000))
        self.stack.enter_context(mock.patch.object(installer.shutil, "disk_usage", return_value=SimpleNamespace(free=10 * 1024 ** 3)))
        self.system_dependencies = self.stack.enter_context(mock.patch.object(installer, "system_dependencies", side_effect=AssertionError("no system modifications")))
        self.apparmor = self.stack.enter_context(mock.patch.object(installer, "scoped_apparmor", side_effect=AssertionError("no AppArmor changes")))
        self.stack.enter_context(mock.patch.object(installer.subprocess, "run", side_effect=AssertionError("no subprocesses")))
        self.stack.enter_context(mock.patch.object(installer.subprocess, "Popen", side_effect=AssertionError("no subprocesses")))

    def test_default_ubuntu_admission_remains_available(self):
        self.release.write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        self.system.version_info = (3, 12, 3)
        installer.check_environment(self.root)
        self.assertFalse(self.root.exists())
        with self.assertRaises(installer.InstallError):
            installer.check_environment(self.root, experimental_debian13=True)

    def test_debian13_requires_explicit_opt_in(self):
        with self.assertRaises(installer.InstallError):
            installer.check_environment(self.root)
        installer.check_environment(self.root, experimental_debian13=True)
        self.assertFalse(self.root.exists())

    def test_other_distributions_and_versions_are_not_admitted(self):
        for distro, version in (("debian", "12"), ("debian", "testing"), ("ubuntu", "24.10"), ("fedora", "43")):
            with self.subTest(distro=distro, version=version):
                self.release.write_text(f'ID={distro}\nVERSION_ID="{version}"\n')
                with self.assertRaises(installer.InstallError):
                    installer.check_environment(self.root, experimental_debian13=True)

    def test_debian13_uses_the_actual_fixed_system_interpreter(self):
        for version in ((3, 12, 3), (3, 14, 0)):
            with self.subTest(version=version):
                self.system.version_info = version
                with self.assertRaises(installer.InstallError):
                    installer.check_environment(self.root, experimental_debian13=True)
        self.system.version_info = (3, 13, 5)
        self.system.executable = "/usr/bin/a-different-python"
        with self.assertRaises(installer.InstallError):
            installer.check_environment(self.root, experimental_debian13=True)

    def test_platform_architecture_and_normal_user_are_still_required(self):
        self.machine.return_value = "aarch64"
        with self.assertRaises(installer.InstallError):
            installer.check_environment(self.root, experimental_debian13=True)
        self.machine.return_value = "x86_64"
        self.system.platform = "win32"
        with self.assertRaises(installer.InstallError):
            installer.check_environment(self.root, experimental_debian13=True)
        self.system.platform = "linux"
        self.uid.return_value = 0
        with self.assertRaises(installer.InstallError):
            installer.check_environment(self.root, experimental_debian13=True)

    def test_system_deps_combination_fails_before_pins_download_or_receipt(self):
        with mock.patch.object(installer, "bundled", side_effect=AssertionError("pins must not be read")), \
                mock.patch.object(installer, "installation", side_effect=AssertionError("no receipt")), \
                mock.patch.object(installer, "fetch", side_effect=AssertionError("no download")):
            with self.assertRaises(installer.InstallError):
                installer.install(self.root, experimental_debian13=True, system_deps=True)
        self.assertFalse(self.root.exists())
        self.system_dependencies.assert_not_called()
        self.apparmor.assert_not_called()

    def test_missing_preinstalled_bwrap_fails_before_download_or_root_creation(self):
        self.bwrap.unlink()
        with mock.patch.object(installer, "bundled", side_effect=AssertionError("pins must not be read")), \
                mock.patch.object(installer, "fetch", side_effect=AssertionError("no download")):
            with self.assertRaises(installer.InstallError):
                installer.install(self.root, experimental_debian13=True)
        self.assertFalse(self.root.exists())

    def test_isolation_failure_has_no_automatic_system_remediation(self):
        with mock.patch.object(installer, "command", side_effect=installer.InstallError("synthetic namespace refusal")) as command:
            with self.assertRaisesRegex(installer.InstallError, "namespace refusal"):
                installer.isolation(self.root, False, experimental_debian13=True)
            command.assert_called_once()
        self.system_dependencies.assert_not_called()
        self.apparmor.assert_not_called()
        with self.assertRaises(installer.InstallError):
            installer.isolation(self.root, True, experimental_debian13=True)
        self.system_dependencies.assert_not_called()

    def test_experimental_receipt_requires_the_same_profile_and_is_not_rewritten(self):
        with installer.installation(self.root, experimental_platform="debian13") as receipt:
            self.assertEqual(receipt["experimental_platform"], "debian13")
        before = (self.root / installer.MARKER).read_bytes()
        with self.assertRaises(installer.InstallError):
            with installer.installation(self.root):
                self.fail("experiment receipt accepted without profile")
        with installer.installation(self.root, experimental_platform="debian13"):
            pass
        self.assertEqual((self.root / installer.MARKER).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
