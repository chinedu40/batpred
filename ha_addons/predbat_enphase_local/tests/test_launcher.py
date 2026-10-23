"""Check persistent configuration, image integrity and local-control shutdown."""

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "launcher.py"
SPEC = importlib.util.spec_from_file_location("predbat_enphase_addon_launcher_tests", LAUNCHER_PATH)
launcher = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launcher
SPEC.loader.exec_module(launcher)
REQUIRED_FILES = ("hass.py", "predbat.py", "enphase_local.py", "enphase_local_client.py")


def _git_sha(data):
    """Return the Git blob checksum used by the image's integrity manifest."""
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class ConfigPreparationTests(unittest.TestCase):
    """Preserve user configuration and keep ownership changes inside its directory."""

    def setUp(self):
        """Prepare distinct configuration and external paths for every test."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.root = self.parent / "config"
        self.template = self.parent / "template.yaml"
        self.template.write_text("predbat:\n  source: template\n", encoding="utf-8")

    def _prepare(self, root=None):
        """Exercise root-mode setup without changing any actual file owner."""
        with patch.object(launcher.os, "geteuid", return_value=0), patch.object(launcher.os, "fchown"):
            return launcher.prepare_config(root or self.root, self.template, uid=1234, gid=1235)

    def test_seed_missing_config_and_return_apps_path(self):
        """A first launch seeds exactly the packaged template in persistent storage."""
        result = self._prepare()
        self.assertEqual(Path(result), self.root / "apps.yaml")
        self.assertEqual((self.root / "apps.yaml").read_bytes(), self.template.read_bytes())

    def test_existing_config_is_never_overwritten(self):
        """Restarting an add-on must preserve the user's complete apps.yaml contents."""
        self.root.mkdir()
        original = b"predbat:\n  username: user-config-marker\n"
        (self.root / "apps.yaml").write_bytes(original)
        self._prepare()
        self.assertEqual((self.root / "apps.yaml").read_bytes(), original)

    def test_repeated_setup_preserves_user_edits(self):
        """Setup remains repeatable after the initial seeded file is edited."""
        self._prepare()
        changed = b"predbat:\n  local_control: configured-by-user\n"
        (self.root / "apps.yaml").write_bytes(changed)
        self._prepare()
        self.assertEqual((self.root / "apps.yaml").read_bytes(), changed)

    def test_symlink_config_root_is_rejected(self):
        """A user-controlled root symlink cannot redirect configuration writes elsewhere."""
        outside = self.parent / "outside"
        outside.mkdir()
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(launcher.LauncherError):
            self._prepare()
        self.assertFalse((outside / "apps.yaml").exists())

    def test_symlink_apps_file_is_rejected_without_touching_target(self):
        """An apps.yaml symlink cannot overwrite or read an unrelated file."""
        self.root.mkdir()
        outside = self.parent / "outside.yaml"
        outside.write_bytes(b"private-external-marker")
        (self.root / "apps.yaml").symlink_to(outside)
        with self.assertRaises(launcher.LauncherError):
            self._prepare()
        self.assertEqual(outside.read_bytes(), b"private-external-marker")

    def test_recursive_ownership_never_follows_symlinks(self):
        """Ownership changes reach regular nested files while external symlink targets stay untouched."""
        self.root.mkdir()
        nested = self.root / "cache"
        nested.mkdir()
        local_file = nested / "local.json"
        local_file.write_bytes(b"{}")
        outside = self.parent / "outside"
        outside.mkdir()
        external_file = outside / "external.json"
        external_file.write_bytes(b"{}")
        (nested / "external-dir").symlink_to(outside, target_is_directory=True)
        (nested / "external-file").symlink_to(external_file)
        changed = []

        def record(fd, uid, gid):
            """Capture inode identities before the helper closes its descriptor."""
            info = os.fstat(fd)
            changed.append((info.st_dev, info.st_ino, uid, gid))

        with patch.object(launcher.os, "geteuid", return_value=0), patch.object(launcher.os, "fchown", side_effect=record):
            launcher.prepare_config(self.root, self.template, uid=1234, gid=1235)
        changed_inodes = {(device, inode) for device, inode, _, _ in changed}
        for path in [self.root, nested, local_file, self.root / "apps.yaml"]:
            info = path.stat()
            self.assertIn((info.st_dev, info.st_ino), changed_inodes)
        for path in [outside, external_file]:
            info = path.stat()
            self.assertNotIn((info.st_dev, info.st_ino), changed_inodes)
        self.assertTrue(all((uid, gid) == (1234, 1235) for _, _, uid, gid in changed))

    def test_nonroot_setup_does_not_change_ownership(self):
        """An already unprivileged runtime does not try privileged ownership operations."""
        with patch.object(launcher.os, "geteuid", return_value=1000), patch.object(launcher.os, "fchown") as chown:
            launcher.prepare_config(self.root, self.template)
        chown.assert_not_called()


class ManifestVerificationTests(unittest.TestCase):
    """Reject incomplete or altered runtime source before application imports."""

    def setUp(self):
        """Create a complete manifest over isolated stand-in runtime files."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.entries = []
        for name in (*REQUIRED_FILES, "support.py"):
            content = f"# fixture: {name}\n".encode()
            (self.root / name).write_bytes(content)
            self.entries.append({"name": name, "size": len(content), "sha": _git_sha(content)})
        self._write_manifest(self.entries)

    def _write_manifest(self, data):
        """Write the exact manifest under test without touching repository files."""
        (self.root / "manifest.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def test_complete_manifest_accepts_runtime_source(self):
        """All required, covered and checksum-matching files form an installable image."""
        launcher.verify_manifest(self.root)

    def test_generated_git_marker_does_not_require_a_manifest_entry(self):
        """The launcher's generated version marker remains outside source integrity entries."""
        (self.root / "git_version.txt").write_text("fixture-version\n", encoding="utf-8")
        launcher.verify_manifest(self.root)

    def test_empty_and_nonlist_manifests_are_rejected(self):
        """An empty manifest or legacy object shape cannot silently bypass verification."""
        for payload in [[], {}, {"files": self.entries}, None, "invalid-manifest"]:
            with self.subTest(payload_type=type(payload).__name__):
                self._write_manifest(payload)
                with self.assertRaises(launcher.LauncherError):
                    launcher.verify_manifest(self.root)

    def test_partial_manifest_is_rejected(self):
        """Leaving a required local-control module out is a failed image verification."""
        self._write_manifest([entry for entry in self.entries if entry["name"] != "enphase_local_client.py"])
        with self.assertRaises(launcher.LauncherError):
            launcher.verify_manifest(self.root)

    def test_missing_manifest_file_is_rejected(self):
        """A fresh image must contain its precomputed integrity manifest."""
        (self.root / "manifest.yaml").unlink()
        with self.assertRaises(launcher.LauncherError):
            launcher.verify_manifest(self.root)

    def test_missing_source_file_is_rejected(self):
        """A manifest entry cannot stand in for an actually absent source file."""
        (self.root / "hass.py").unlink()
        with self.assertRaises(launcher.LauncherError):
            launcher.verify_manifest(self.root)

    def test_same_size_source_corruption_is_rejected(self):
        """Content hashing catches altered code even when its byte count still matches."""
        path = self.root / "predbat.py"
        path.write_bytes(b"x" * path.stat().st_size)
        with self.assertRaises(launcher.LauncherError):
            launcher.verify_manifest(self.root)

    def test_uncovered_immediate_source_file_is_rejected(self):
        """Unlisted source cannot be imported alongside a superficially valid manifest."""
        (self.root / "unlisted.py").write_text("raise RuntimeError('unexpected')\n", encoding="utf-8")
        with self.assertRaises(launcher.LauncherError):
            launcher.verify_manifest(self.root)

    def test_manifest_cannot_escape_source_directory(self):
        """A manifest name is a bare filename, never a path outside the verified source."""
        outside = self.root.parent / "outside.py"
        secret = b"secret-external-source-marker"
        outside.write_bytes(secret)
        self._write_manifest(self.entries + [{"name": "../outside.py", "size": len(secret), "sha": _git_sha(secret)}])
        with self.assertRaises(launcher.LauncherError) as caught:
            launcher.verify_manifest(self.root)
        self.assertNotIn(secret.decode(), str(caught.exception))

    def test_source_symlink_is_rejected_even_when_target_bytes_match(self):
        """Matching hashes cannot permit a symlink to escape the source-file boundary."""
        path = self.root / "enphase_local.py"
        outside = self.root.parent / "outside.py"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(launcher.LauncherError):
            launcher.verify_manifest(self.root)

    def test_invalid_manifest_body_is_not_printed_in_error(self):
        """A malformed manifest cannot expose its raw body through a parser traceback."""
        secret = "private-manifest-body-marker"
        (self.root / "manifest.yaml").write_text(f"[unterminated, {secret}\n", encoding="utf-8")
        with self.assertRaises(launcher.LauncherError) as caught:
            launcher.verify_manifest(self.root)
        self.assertNotIn(secret, str(caught.exception))


class _LocalComponent:
    """Record actual component shutdown independently from the application's cleanup."""

    def __init__(self, events):
        """Share an event sequence with the fake application."""
        self.events = events

    async def stop(self):
        """Represent the real component's idempotent verified override release."""
        self.events.append("local-stop")


class _Components:
    """Expose the existing application component-lookup contract."""

    def __init__(self, local):
        """Retain an optional local component for lookup."""
        self.local = local

    def get_component(self, name):
        """Resolve only the local Enphase component used by the launcher."""
        return self.local if name == "enphase_local" else None


class _App:
    """Provide application lifecycle hooks without Home Assistant or network access."""

    def __init__(self, action=None, initialize_error=None, local=True):
        """Initialise controllable tick behavior and a shared lifecycle record."""
        self.events = []
        self.fatal_error = False
        self.action = action
        self.initialize_error = initialize_error
        self.started = asyncio.Event()
        self.components = _Components(_LocalComponent(self.events) if local else None)

    def initialize(self):
        """Record startup and optionally raise a fixture failure."""
        self.events.append("initialize")
        if self.initialize_error:
            raise self.initialize_error

    async def timer_tick(self):
        """Run one controlled iteration, exposing when a stalled tick has started."""
        self.events.append("tick")
        self.started.set()
        if self.action:
            result = self.action(self)
            if asyncio.iscoroutine(result):
                await result

    async def stop_all(self):
        """Represent global shutdown after the local mode-release step."""
        self.events.append("global-stop")


class _Deadline:
    """Record watchdog lifecycle without ever terminating a test process."""

    def __init__(self, interval, function, args=None, kwargs=None):
        """Retain the configured fallback and its deadline for explicit assertions."""
        self.interval = interval
        self.function = function
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self):
        """Record activation instead of creating a real exit-capable timer thread."""
        self.started = True

    def cancel(self):
        """Record safe cancellation after a complete cleanup."""
        self.cancelled = True


class ApplicationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Every application exit must release local control before global shutdown."""

    async def asyncSetUp(self):
        """Replace exit-capable timers so scheduling delays cannot kill the test runner."""
        self.deadlines = []

        def create(*args, **kwargs):
            """Retain watchdog instances for lifecycle assertions."""
            deadline = _Deadline(*args, **kwargs)
            self.deadlines.append(deadline)
            return deadline

        patcher = patch.object(launcher.threading, "Timer", side_effect=create)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _run(self, app, event=None, **kwargs):
        """Use short deterministic intervals without installing process signal handlers."""
        return await launcher.run_app(app, stop_event=event, tick_seconds=0.001, shutdown_timeout=0.1, release_timeout=0.1, install_signals=False, **kwargs)

    def _assert_cleanup(self, app):
        """Require local component shutdown before the global lifecycle cleanup."""
        self.assertIn("local-stop", app.events)
        self.assertIn("global-stop", app.events)
        self.assertLess(app.events.index("local-stop"), app.events.index("global-stop"))

    async def test_requested_stop_returns_zero_and_cleans_up(self):
        """A normal stop request follows the graceful local-release path."""
        event = asyncio.Event()
        app = _App(action=lambda _: event.set())
        result = await self._run(app, event)
        self.assertEqual(result, 0)
        self.assertEqual(app.events[0], "initialize")
        self._assert_cleanup(app)
        self.assertEqual(len(self.deadlines), 1)
        self.assertTrue(self.deadlines[0].daemon)
        self.assertTrue(self.deadlines[0].cancelled)

    async def test_reload_returns_restart_code_and_cleans_up(self):
        """A source/config reload uses the supervised restart status after safe release."""
        app = _App()
        result = await self._run(app, check_changed=lambda: True)
        self.assertEqual(result, 75)
        self._assert_cleanup(app)

    async def test_fatal_application_state_requests_restart_after_cleanup(self):
        """An application fatal state restarts only after the component is stopped."""

        def fatal(app):
            """Simulate the base application signalling a fatal condition."""
            app.fatal_error = True

        app = _App(action=fatal)
        result = await self._run(app)
        self.assertEqual(result, 75)
        self._assert_cleanup(app)

    async def test_runtime_exception_is_safe_and_still_cleans_up(self):
        """A failed timer iteration cannot leak an exception's credential-like contents."""
        secret = "private-runtime-exception-marker"

        def fail(app):
            """Raise an application error containing a value that must not be printed."""
            raise RuntimeError(secret)

        app = _App(action=fail)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = await self._run(app)
        self.assertEqual(result, launcher.RELOAD)
        self.assertNotIn(secret, output.getvalue())
        self._assert_cleanup(app)

    async def test_initialize_exception_still_cleans_up(self):
        """Partially constructed components get a cleanup attempt even if initialization fails."""
        secret = "private-initialize-exception-marker"
        app = _App(initialize_error=RuntimeError(secret))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = await self._run(app)
        self.assertEqual(result, launcher.RELOAD)
        self.assertNotIn(secret, output.getvalue())
        self._assert_cleanup(app)

    async def test_stop_event_cancels_current_iteration_before_cleanup(self):
        """A pending network-like iteration cannot prevent an external stop request."""
        event = asyncio.Event()
        cancelled = asyncio.Event()

        async def stall(app):
            """Wait until lifecycle shutdown cancels the simulated outstanding operation."""
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        app = _App(action=stall)
        task = asyncio.create_task(self._run(app, event))
        await asyncio.wait_for(app.started.wait(), 0.5)
        event.set()
        try:
            result = await asyncio.wait_for(asyncio.shield(task), 0.2)
        finally:
            if not task.done():
                task.cancel()
                await task
        self.assertEqual(result, 0)
        self.assertTrue(cancelled.is_set())
        self._assert_cleanup(app)

    async def test_failed_local_release_still_attempts_global_cleanup(self):
        """A failed priority release must not bypass cleanup of every other component."""
        event = asyncio.Event()
        app = _App(action=lambda _: event.set())

        async def fail():
            """Represent a local release failure without making any gateway request."""
            app.events.append("local-stop")
            raise RuntimeError("private-release-exception-marker")

        app.components.local.stop = fail
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = await self._run(app, event)
        self.assertEqual(result, 1)
        self.assertNotIn("private-release-exception-marker", output.getvalue())
        self._assert_cleanup(app)

    def test_hard_shutdown_fallback_is_explicit_and_safe_to_log(self):
        """The final process deadline must report unconfirmed release and exit unsuccessfully."""
        output = io.StringIO()
        with patch.object(launcher.os, "_exit") as exit_process, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            launcher._shutdown_deadline()
        exit_process.assert_called_once_with(1)
        self.assertIn("release", output.getvalue().lower())

    async def test_missing_local_component_still_runs_global_cleanup(self):
        """Monitor-only or partially initialized applications retain normal shutdown."""
        event = asyncio.Event()
        app = _App(action=lambda _: event.set(), local=False)
        self.assertEqual(await self._run(app, event), 0)
        self.assertIn("global-stop", app.events)

    async def test_sigterm_requests_graceful_cleanup(self):
        """The installed signal callback requests cancellation and the same cleanup path."""
        handlers = {}

        def register(number, callback):
            """Capture process handlers without changing actual test-runner signals."""
            previous = handlers.get(number, signal.SIG_DFL)
            handlers[number] = callback
            return previous

        async def stall(app):
            """Keep a timer iteration pending until the fake SIGTERM callback is invoked."""
            await asyncio.Event().wait()

        app = _App(action=stall)
        real_thread = launcher.threading.Thread
        threads = []

        def create_thread(*args, **kwargs):
            """Track the real daemon worker invoking the actual component stop method."""
            thread = real_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        with patch.object(launcher.signal, "signal", side_effect=register), patch.object(launcher.signal, "getsignal", return_value=signal.SIG_DFL), patch.object(launcher.threading, "Thread", side_effect=create_thread):
            task = asyncio.create_task(launcher.run_app(app, tick_seconds=0.001, shutdown_timeout=0.1, release_timeout=0.1, install_signals=True))
            await asyncio.wait_for(app.started.wait(), 0.5)
            self.assertIn(signal.SIGTERM, handlers)
            handler = handlers[signal.SIGTERM]
            handler(signal.SIGTERM, None)
            handler(signal.SIGTERM, None)
            result = await asyncio.wait_for(task, 0.5)
        self.assertEqual(result, 0)
        self._assert_cleanup(app)
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0].daemon)
        self.assertEqual(app.events.count("local-stop"), 1)
        self.assertEqual(len(self.deadlines), 1)


class PrivilegeDropTests(unittest.TestCase):
    """Application imports follow a complete drop of root's supplementary groups and IDs."""

    def test_root_drops_groups_then_gid_then_uid(self):
        """The launcher must not retain root supplementary groups after changing its user."""
        actions = []
        with patch.object(launcher.os, "geteuid", return_value=0), patch.object(launcher.os, "setgroups", side_effect=lambda groups: actions.append(("groups", groups))), patch.object(
            launcher.os, "setgid", side_effect=lambda value: actions.append(("gid", value))
        ), patch.object(launcher.os, "setuid", side_effect=lambda value: actions.append(("uid", value))):
            launcher.drop_privileges(uid=1234, gid=1235)
        self.assertEqual(actions, [("groups", []), ("gid", 1235), ("uid", 1234)])

    def test_dedicated_user_keeps_existing_privileges(self):
        """An already unprivileged application user needs no privileged system calls."""
        with patch.object(launcher.os, "geteuid", return_value=1000), patch.object(launcher.os, "setgroups") as groups, patch.object(launcher.os, "setgid") as gid, patch.object(launcher.os, "setuid") as uid:
            launcher.drop_privileges()
        groups.assert_not_called()
        gid.assert_not_called()
        uid.assert_not_called()

    def test_unexpected_user_is_rejected(self):
        """The immutable source must not run under an arbitrary application's user ID."""
        with patch.object(launcher.os, "geteuid", return_value=2000), self.assertRaises(launcher.LauncherError):
            launcher.drop_privileges()


class LauncherEntryPointTests(unittest.TestCase):
    """Unverified source is rejected before any application module can be imported."""

    def test_main_rejects_missing_or_corrupt_manifest_before_import(self):
        """An absent or corrupted image manifest cannot activate Predbat's self-updater."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            entries = []
            for name in REQUIRED_FILES:
                content = f"# fixture: {name}\n".encode()
                (source / name).write_bytes(content)
                entries.append({"name": name, "size": len(content), "sha": _git_sha(content)})
            arguments = ["--source", str(source), "--config", str(root / "config"), "--template", str(root / "template.yaml"), "--check"]
            for corrupt in (False, True):
                with self.subTest(corrupt=corrupt):
                    if corrupt:
                        (source / "manifest.yaml").write_text(yaml.safe_dump(entries), encoding="utf-8")
                        (source / "predbat.py").write_text("# corrupted source\n", encoding="utf-8")
                    output = io.StringIO()
                    with patch.object(launcher, "prepare_config", return_value=root / "config" / "apps.yaml"), patch.object(launcher, "drop_privileges") as drop, patch.object(launcher, "import_core") as import_core, contextlib.redirect_stdout(
                        output
                    ), contextlib.redirect_stderr(output):
                        result = launcher.main(arguments)
                    self.assertEqual(result, 1)
                    drop.assert_not_called()
                    import_core.assert_not_called()


if __name__ == "__main__":
    unittest.main()
