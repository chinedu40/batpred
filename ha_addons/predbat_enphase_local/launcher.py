"""Run a pinned Predbat checkout with persistent settings and graceful shutdown."""

import argparse
import asyncio
from concurrent.futures import Future
from datetime import datetime
import errno
import hashlib
import importlib
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
import time

import yaml


RELOAD = 75
REQUIRED_FILES = {"hass.py", "predbat.py", "enphase_local.py", "enphase_local_client.py"}


class LauncherError(Exception):
    """Describe a startup failure without exposing configuration or credentials."""


def _open_config_root(path):
    """Open or create a directory through parents that cannot be symbolic links."""
    parts = Path(path).absolute().parts
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:]:
            if part in (".", ".."):
                raise LauncherError("Configuration path must not contain parent references")
            try:
                os.mkdir(part, mode=0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _chown_tree(descriptor, uid, gid):
    """Change ownership of regular files and directories without following links."""
    os.fchown(descriptor, uid, gid)
    for entry in os.scandir(descriptor):
        information = entry.stat(follow_symlinks=False)
        if not (stat.S_ISDIR(information.st_mode) or stat.S_ISREG(information.st_mode)):
            continue
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if stat.S_ISDIR(information.st_mode):
            flags |= os.O_DIRECTORY
        try:
            child = os.open(entry.name, flags, dir_fd=descriptor)
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOENT):
                continue
            raise
        try:
            if stat.S_ISDIR(os.fstat(child).st_mode):
                _chown_tree(child, uid, gid)
            elif stat.S_ISREG(os.fstat(child).st_mode):
                os.fchown(child, uid, gid)
        finally:
            os.close(child)


def prepare_config(config_root, template, uid=1000, gid=1000):
    """Seed settings once and preserve existing configuration and cache contents."""
    try:
        descriptor = _open_config_root(config_root)
        try:
            try:
                information = os.stat("apps.yaml", dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISREG(information.st_mode):
                    raise LauncherError("Existing apps.yaml must be a regular file")
            except FileNotFoundError:
                seed = Path(template)
                if seed.is_symlink() or not seed.is_file():
                    raise LauncherError("Default configuration is unavailable")
                output = os.open("apps.yaml", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=descriptor)
                with os.fdopen(output, "wb") as handle:
                    handle.write(seed.read_bytes())
            if os.geteuid() == 0:
                _chown_tree(descriptor, uid, gid)
        finally:
            os.close(descriptor)
    except OSError:
        raise LauncherError("Configuration directory is unsafe or inaccessible") from None
    return Path(config_root).absolute() / "apps.yaml"


def verify_manifest(source):
    """Verify every immediate source file against the complete Git-blob manifest."""
    source = Path(source)
    manifest = source / "manifest.yaml"
    try:
        if source.is_symlink() or not source.is_dir() or manifest.is_symlink() or not manifest.is_file():
            raise LauncherError("Pinned source or its integrity manifest is unavailable")
        entries = yaml.safe_load(manifest.read_text())
        if not isinstance(entries, list) or not entries:
            raise LauncherError("Pinned source manifest must be a nonempty file list")
        names = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise LauncherError("Pinned source manifest entry is invalid")
            name, size, digest = entry.get("name"), entry.get("size"), entry.get("sha")
            if not isinstance(name, str) or name in ("", ".", "..") or any(char in name for char in ("/", "\\", "\0")) or name in names:
                raise LauncherError("Pinned source manifest contains an unsafe or duplicate filename")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0 or not isinstance(digest, str) or re.fullmatch(r"[a-fA-F0-9]{40}", digest) is None:
                raise LauncherError("Pinned source manifest contains invalid integrity metadata")
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise LauncherError("Pinned source contains a missing file or symbolic link")
            data = path.read_bytes()
            actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
            if len(data) != size or actual != digest.lower():
                raise LauncherError("Pinned source integrity verification failed")
            names.add(name)
        files = set()
        for path in source.iterdir():
            if path.is_symlink():
                raise LauncherError("Pinned source must not contain symbolic links")
            if path.is_file() and path.name not in ("manifest.yaml", "git_version.txt"):
                files.add(path.name)
        if not REQUIRED_FILES <= names or not files <= names:
            raise LauncherError("Pinned source integrity manifest is incomplete")
    except (OSError, UnicodeError, yaml.YAMLError):
        raise LauncherError("Pinned source integrity manifest could not be read") from None
    return len(names)


def drop_privileges(uid=1000, gid=1000):
    """Drop root before importing application code, including on a clean reload."""
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    elif os.geteuid() != uid:
        raise LauncherError("Predbat must run under its dedicated application user")


def import_core(source):
    """Import only the already verified, pinned application modules."""
    sys.path.insert(0, str(Path(source).absolute()))
    return importlib.import_module("predbat"), importlib.import_module("hass")


def _local_component(app):
    """Find the local controller without performing IO or assuming initialization completed."""
    components = getattr(app, "components", None)
    return components.get_component("enphase_local") if components else None


def _shutdown_deadline():
    """Bound blocking legacy joins when cooperative shutdown cannot complete."""
    try:
        print("Predbat shutdown deadline exceeded; gateway override release may remain pending", flush=True)
    finally:
        os._exit(1)


async def run_app(app, stop_event=None, check_changed=None, tick_seconds=1, shutdown_timeout=90, release_timeout=35, install_signals=True):
    """Run the core and release local control before potentially slow global cleanup."""
    stop_event = stop_event if stop_event is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    iteration, watchdog, release_future, releasing = None, None, None, None
    stopping = asyncio.create_task(stop_event.wait())
    handlers = {}
    outcome, cleanup_ok, stopped_all = 0, True, False

    def start_deadline():
        """Start one process deadline even if a synchronous core callback is blocked."""
        nonlocal watchdog
        if watchdog is None:
            watchdog = threading.Timer(shutdown_timeout, _shutdown_deadline)
            watchdog.daemon = True
            watchdog.start()

    def emergency_release():
        """Route stop through the controller's owner loop without posting modes directly."""
        try:
            asyncio.run(asyncio.wait_for(releasing.stop(), release_timeout))
        except BaseException:
            release_future.set_exception(LauncherError("Priority gateway shutdown did not complete"))
        else:
            release_future.set_result(None)

    def request_stop(signum, frame):
        """Request cancellation and independently release control during a blocked core tick."""
        nonlocal release_future, releasing
        start_deadline()
        stop_event.set()
        if iteration is not None:
            loop.call_soon_threadsafe(iteration.cancel)
        if release_future is None:
            releasing = _local_component(app)
            if releasing is not None:
                release_future = Future()
                threading.Thread(target=emergency_release, name="predbat-priority-stop", daemon=True).start()

    if install_signals:
        for signum in (signal.SIGTERM, signal.SIGINT):
            handlers[signum] = signal.signal(signum, request_stop)
    try:
        app.initialize()
        while not stop_event.is_set():
            iteration = asyncio.create_task(app.timer_tick())
            await asyncio.wait((iteration, stopping), return_when=asyncio.FIRST_COMPLETED)
            if stop_event.is_set():
                iteration.cancel()
                break
            try:
                await iteration
            except asyncio.CancelledError:
                if stop_event.is_set():
                    break
                raise
            if getattr(app, "fatal_error", False) or (check_changed and check_changed()):
                outcome = RELOAD
                break
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=tick_seconds)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        stop_event.set()
    except Exception as error:
        print(f"Predbat runtime failed ({type(error).__name__})", flush=True)
        outcome = RELOAD
    finally:
        start_deadline()
        stopping.cancel()
        if iteration is not None:
            iteration.cancel()
            try:
                await iteration
            except BaseException:
                pass
        try:
            async with asyncio.timeout(shutdown_timeout):
                try:
                    local = _local_component(app)
                    if release_future is not None:
                        await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(release_future)), timeout=release_timeout)
                    if local is not None and local is not releasing:
                        await asyncio.wait_for(local.stop(), timeout=release_timeout)
                except Exception as error:
                    print(f"Priority gateway shutdown remains incomplete ({type(error).__name__})", flush=True)
                    cleanup_ok = False
                await app.stop_all()
                stopped_all = True
        except BaseException as error:
            print(f"Predbat cleanup remains incomplete ({type(error).__name__})", flush=True)
            cleanup_ok = False
        if stopped_all:
            watchdog.cancel()
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    return outcome if cleanup_ok else 1


def main(argv=None):
    """Prepare persistent settings, drop privileges and run or verify the addon."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="/opt/predbat-source/apps/predbat")
    parser.add_argument("--config", default="/config")
    parser.add_argument("--template", default="/addon/apps.yaml")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    args.source, args.config, args.template = (str(Path(value).absolute()) for value in (args.source, args.config, args.template))
    try:
        apps = prepare_config(args.config, args.template)
        verify_manifest(args.source)
        drop_privileges()
        if os.access(args.source, os.W_OK):
            raise LauncherError("Pinned source must not be writable by the application user")
        os.chdir(args.config)
        os.environ["PREDBAT_APPS_FILE"] = str(apps)
        if not args.check and not os.environ.get("SUPERVISOR_TOKEN"):
            raise LauncherError("Supervisor authentication is unavailable")
        # Catch YAML syntax errors before the legacy loader can print a source
        # line containing a configured credential. Composing does not resolve tags.
        for name in ("apps.yaml", "secrets.yaml"):
            configuration = Path(args.config, name)
            if configuration.is_file():
                yaml.compose(configuration.read_text())
        core, hass = import_core(args.source)
        if args.check:
            marker = Path(args.source, "git_version.txt").read_text().split()[0]
            public_ref = marker if re.fullmatch(r"[a-fA-F0-9]{7,40}", marker) else "unknown"
            print(f"Predbat image check passed: source={Path(args.source).absolute()} ref={public_ref} uid={os.geteuid()} code_writable=False seed_defaults=Monitor/read-only", flush=True)
            return 0
        from multiprocessing import set_start_method

        set_start_method("fork", force=True)
        app = core.PredBat()
        started = datetime.now()
        files = hass.collect_watch_files([args.config, args.source], str(apps))
        stop_event = asyncio.Event()
        result = asyncio.run(run_app(app, stop_event, check_changed=lambda: hass.check_modified(files, started)))
        if result == RELOAD and not stop_event.is_set():
            time.sleep(5)
            if not stop_event.is_set():
                os.execv(sys.executable, [sys.executable, str(Path(__file__).absolute()), *(sys.argv[1:] if argv is None else argv)])
        return result
    except Exception as error:
        print(f"Predbat launcher failed ({type(error).__name__})", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
