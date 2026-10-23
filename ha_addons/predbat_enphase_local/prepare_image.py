"""Verify a pinned Predbat checkout and prepare its image integrity metadata."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import sys

import yaml


REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}")
COMMIT_PATTERN = re.compile(r"[0-9a-fA-F]{40}")
REQUIRED_FILES = {"predbat.py", "hass.py", "enphase_local.py", "enphase_local_client.py"}
GENERATED_FILES = {"manifest.yaml", "git_version.txt"}
NATIVE_KERNEL = "prediction_kernel_lib.so"


def _git(source, *arguments):
    """Read local Git metadata without accessing a remote or exposing Git errors."""
    try:
        return subprocess.run(["git", "-C", str(source), *arguments], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        raise ValueError("Could not read the pinned checkout's Git metadata") from None


def _regular_file(path):
    """Require a real regular file, refusing symlinks and special file types."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise ValueError(f"Required image file is missing: {path.name}") from None
    if not stat.S_ISREG(mode):
        raise ValueError(f"Image file must be regular and not a symlink: {path.name}")
    return path.read_bytes()


def _directory(path):
    """Require a directory at this path without following a final symlink."""
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError(f"Image source directory must not be a symlink: {path.name}")


def _source_metadata(path):
    """Validate the public GitHub identity and full pinned commit identifier."""
    try:
        metadata = json.loads(_regular_file(path))
    except (ValueError, UnicodeError):
        raise ValueError("Source metadata must be a readable JSON object") from None
    if not isinstance(metadata, dict):
        raise ValueError("Source metadata must be a JSON object")
    repository, commit = metadata.get("repository"), metadata.get("ref")
    if not isinstance(repository, str) or not REPOSITORY_PATTERN.fullmatch(repository) or repository.split("/")[1] in {".", ".."}:
        raise ValueError("Source repository must be a GitHub owner/repository name")
    if not isinstance(commit, str) or not COMMIT_PATTERN.fullmatch(commit):
        raise ValueError("Source ref must be a full 40-character hexadecimal commit")
    return repository, commit.lower()


def _blob_sha(data):
    """Compute the Git SHA-1 object identifier for the exact supplied file bytes."""
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


def _tracked_entries(source, app_dir, commit):
    """Verify every tracked top-level application blob before creating a manifest."""
    entries = []
    for item in _git(source, "ls-tree", "-l", "-z", f"{commit}:apps/predbat").split(b"\0"):
        if not item:
            continue
        fields, name = item.split(b"\t", 1)
        mode, kind, sha, size = fields.split()
        if kind == b"tree":
            continue
        filename = name.decode("utf-8")
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise ValueError(f"Tracked image file must be regular and not a symlink: {filename}")
        if filename in GENERATED_FILES:
            raise ValueError(f"Generated image metadata must not be tracked: {filename}")
        data = _regular_file(app_dir / filename)
        if not data:
            raise ValueError(f"Tracked image file must not be empty: {filename}")
        if len(data) != int(size) or _blob_sha(data) != sha.decode("ascii"):
            raise ValueError(f"Tracked image file differs from the pinned Git blob: {filename}")
        entries.append({"name": filename, "size": len(data), "sha": sha.decode("ascii")})
    missing = REQUIRED_FILES - {entry["name"] for entry in entries}
    if missing:
        raise ValueError("Pinned checkout is missing required application files: " + ", ".join(sorted(missing)))
    return entries


def _check_extra_files(app_dir, entries):
    """Add only the freshly built native kernel and reject unexpected application files."""
    tracked = {entry["name"] for entry in entries}
    for path in app_dir.iterdir():
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Application paths must not be symlinks: {path.name}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Application path is not a regular file: {path.name}")
        if path.name in tracked or path.name in GENERATED_FILES:
            continue
        if path.name != NATIVE_KERNEL:
            raise ValueError(f"Unexpected untracked application file: {path.name}")
        data = _regular_file(path)
        if not data:
            raise ValueError("Built native prediction kernel must not be empty")
        entries.append({"name": path.name, "size": len(data), "sha": _blob_sha(data)})


def prepare_image(source=Path("/opt/predbat-source"), metadata_path=Path("/addon/source.json")):
    """Validate pinned source and write a complete readable manifest and version marker."""
    source, metadata_path = Path(source), Path(metadata_path)
    repository, commit = _source_metadata(metadata_path)
    app_parent, app_dir = source / "apps", source / "apps" / "predbat"
    for directory in (source, app_parent, app_dir):
        _directory(directory)
    if Path(_git(source, "rev-parse", "--show-toplevel").decode().strip()).resolve() != source.resolve():
        raise ValueError("Source path must be the pinned checkout's Git root")
    if _git(source, "rev-parse", "--verify", "HEAD^{commit}").decode().strip() != commit:
        raise ValueError("Checkout HEAD does not match the pinned source ref")
    entries = _tracked_entries(source, app_dir, commit)
    _check_extra_files(app_dir, entries)
    entries.sort(key=lambda entry: entry["name"])
    manifest, marker = app_dir / "manifest.yaml", app_dir / "git_version.txt"
    # All tracked bytes and output path types were checked before either write.
    manifest.write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")
    marker.write_text(f"{commit[:8]} Enphase testing\n", encoding="utf-8")
    manifest.chmod(0o644)
    marker.chmod(0o644)
    print(f"Prepared {len(entries)} verified application files from {repository}@{commit}")
    return entries


def main():
    """Prepare an image using explicit fixture paths or the normal Docker build paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/opt/predbat-source"))
    parser.add_argument("--metadata", type=Path, default=Path("/addon/source.json"))
    arguments = parser.parse_args()
    try:
        prepare_image(arguments.source, arguments.metadata)
    except (ValueError, OSError):
        print("Image preparation failed: source, metadata or integrity validation was unsuccessful", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
