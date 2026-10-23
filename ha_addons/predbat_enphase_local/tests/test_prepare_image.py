"""Offline image-preparation checks against small real Git repositories."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

import yaml


HELPER_PATH = Path(__file__).resolve().parents[1] / "prepare_image.py"
SPEC = importlib.util.spec_from_file_location("prepare_image", HELPER_PATH)
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


class PrepareImageTests(unittest.TestCase):
    """Verify image metadata never blesses source that differs from its pinned commit."""

    def setUp(self):
        """Commit a tiny application tree containing every required entry point."""
        self.temporary = tempfile.TemporaryDirectory(prefix="predbat-image-prepare-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "checkout"
        self.app = self.source / "apps" / "predbat"
        self.app.mkdir(parents=True)
        self.metadata = self.root / "source.json"
        self.files = {name: f'"""Fixture {name}."""\n'.encode() for name in PREPARE.REQUIRED_FILES}
        self.files["tracked_asset.bin"] = b"\x00\xff\x80asset\n"
        for name, data in self.files.items():
            (self.app / name).write_bytes(data)
        nested = self.app / "config"
        nested.mkdir()
        (nested / "apps.yaml").write_text("pred_bat: {}\n", encoding="utf-8")
        self.git("init", "--quiet")
        self.git("add", ".")
        self.commit()

    def tearDown(self):
        """Remove only the Git fixture created by this test."""
        self.temporary.cleanup()

    def git(self, *arguments):
        """Execute a bounded local-only Git command in the temporary fixture."""
        return subprocess.run(["git", "-C", str(self.source), *arguments], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10).stdout

    def commit(self):
        """Create a deterministic-identity fixture commit and update its source metadata."""
        self.git("-c", "user.name=Image test", "-c", "user.email=image-test@example.invalid", "commit", "--quiet", "-m", "Fixture source")
        self.ref = self.git("rev-parse", "HEAD").decode().strip()
        self.write_metadata()

    def write_metadata(self, **overrides):
        """Write public fixture metadata with optional invalid values for rejection tests."""
        self.metadata.write_text(json.dumps({"repository": "chinedu40/batpred", "ref": self.ref, **overrides}), encoding="utf-8")

    def prepare(self):
        """Run actual preparation while keeping successful informational output out of the log."""
        with contextlib.redirect_stdout(io.StringIO()):
            return PREPARE.prepare_image(self.source, self.metadata)

    def test_manifest_matches_all_tracked_immediate_git_blobs(self):
        """Every top-level tracked file receives its original Git SHA and exact byte size."""
        entries = self.prepare()
        parsed = yaml.safe_load((self.app / "manifest.yaml").read_text())
        self.assertEqual(entries, parsed)
        self.assertEqual({entry["name"] for entry in parsed}, set(self.files))
        for entry in parsed:
            data = self.files[entry["name"]]
            expected = self.git("rev-parse", f"HEAD:apps/predbat/{entry['name']}").decode().strip()
            self.assertEqual(entry["sha"], expected)
            self.assertEqual(entry["size"], len(data))
        self.assertEqual((self.app / "git_version.txt").read_text(), f"{self.ref[:8]} Enphase testing\n")
        for name in PREPARE.GENERATED_FILES:
            self.assertTrue(stat.S_IMODE((self.app / name).stat().st_mode) & 0o044)

    def test_repeated_preparation_excludes_generated_metadata(self):
        """Rebuilding the same source does not add the manifest or marker to their own checksums."""
        self.prepare()
        before = (self.app / "manifest.yaml").read_bytes()
        entries = self.prepare()
        self.assertEqual((self.app / "manifest.yaml").read_bytes(), before)
        self.assertFalse(PREPARE.GENERATED_FILES & {entry["name"] for entry in entries})

    def test_fresh_native_kernel_is_the_only_permitted_untracked_artifact(self):
        """The compiled kernel is included using a Git-style checksum of its exact build bytes."""
        kernel = b"\x7fELFfixture native kernel"
        (self.app / PREPARE.NATIVE_KERNEL).write_bytes(kernel)
        entries = self.prepare()
        entry = next(entry for entry in entries if entry["name"] == PREPARE.NATIVE_KERNEL)
        self.assertEqual(entry, {"name": PREPARE.NATIVE_KERNEL, "size": len(kernel), "sha": hashlib.sha1(f"blob {len(kernel)}\0".encode() + kernel).hexdigest()})

    def test_modified_same_length_tracked_bytes_are_rejected(self):
        """A size-preserving source mutation fails before either metadata output is written."""
        path = self.app / "enphase_local.py"
        data = path.read_bytes()
        path.write_bytes(b"!" + data[1:])
        with self.assertRaisesRegex(ValueError, "differs from the pinned Git blob"):
            self.prepare()
        self.assertFalse((self.app / "manifest.yaml").exists())
        self.assertFalse((self.app / "git_version.txt").exists())

    def test_missing_tracked_entrypoint_is_rejected(self):
        """Deleting an entry point cannot produce a passing partial manifest."""
        (self.app / "hass.py").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.prepare()

    def test_required_file_absent_from_commit_is_rejected(self):
        """An incomplete commit fails even when an untracked replacement file exists on disk."""
        self.git("rm", "--quiet", "apps/predbat/enphase_local_client.py")
        self.commit()
        (self.app / "enphase_local_client.py").write_bytes(self.files["enphase_local_client.py"])
        with self.assertRaisesRegex(ValueError, "missing required application files"):
            self.prepare()

    def test_zero_length_entrypoint_is_rejected(self):
        """An empty entry point is rejected even if its empty blob was actually committed."""
        (self.app / "predbat.py").write_bytes(b"")
        self.git("add", "apps/predbat/predbat.py")
        self.commit()
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            self.prepare()

    def test_working_tree_symlink_is_rejected(self):
        """A symlink cannot stand in for otherwise byte-identical trusted source."""
        path = self.app / "enphase_local.py"
        copy = self.root / "copied.py"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.prepare()

    def test_committed_symlink_is_rejected(self):
        """A pinned symlink object remains an unsupported image input."""
        path = self.app / "enphase_local.py"
        path.unlink()
        path.symlink_to("predbat.py")
        self.git("add", "apps/predbat/enphase_local.py")
        self.commit()
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.prepare()

    def test_generated_output_symlink_is_rejected(self):
        """Metadata creation cannot overwrite a file reached through a symlink."""
        target = self.root / "preserve.txt"
        target.write_text("preserve", encoding="utf-8")
        (self.app / "manifest.yaml").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.prepare()
        self.assertEqual(target.read_text(), "preserve")

    def test_ref_mismatch_is_rejected(self):
        """A valid-looking commit pin must match the checkout's actual HEAD."""
        self.write_metadata(ref="f" * 40)
        with self.assertRaisesRegex(ValueError, "HEAD does not match"):
            self.prepare()

    def test_short_and_nonhex_refs_are_rejected(self):
        """Mutable branches, abbreviations and malformed pins are never accepted."""
        for value in ("main", self.ref[:8], "z" * 40, "a" * 39, "a" * 41, None):
            with self.subTest(value=value):
                self.write_metadata(ref=value)
                with self.assertRaisesRegex(ValueError, "40-character"):
                    self.prepare()

    def test_repository_urls_and_traversal_are_rejected(self):
        """Repository metadata cannot contain an URL, credentials or path traversal."""
        for value in ("https://github.com/chinedu40/batpred", "../batpred", "chinedu40/..", "chinedu40/../batpred", "user:secret@example.invalid/repo", "owner/repo#branch", "owner/repo extra", "owner//repo"):
            with self.subTest(value=value):
                self.write_metadata(repository=value)
                with self.assertRaisesRegex(ValueError, "owner/repository"):
                    self.prepare()

    def test_unknown_untracked_regular_file_is_rejected(self):
        """Unexpected source beside the pinned application cannot enter a trusted image."""
        (self.app / "unexpected.py").write_text("untrusted\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unexpected untracked"):
            self.prepare()

    def test_empty_untracked_native_kernel_is_rejected(self):
        """A failed compiler output cannot be blessed as a valid native library."""
        (self.app / PREPARE.NATIVE_KERNEL).write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "kernel must not be empty"):
            self.prepare()

    def test_app_directory_symlink_is_rejected(self):
        """A symlinked application directory cannot redirect the build verification."""
        original = self.source / "apps" / "moved"
        self.app.rename(original)
        self.app.symlink_to(original, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "directory must not be a symlink"):
            self.prepare()

    def test_command_line_accepts_explicit_build_paths(self):
        """The actual Docker build entry point prepares a fixture and exits successfully."""
        result = subprocess.run([sys.executable, str(HELPER_PATH), "--source", str(self.source), "--metadata", str(self.metadata)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(self.ref.encode(), result.stdout)
        self.assertEqual(len(yaml.safe_load((self.app / "manifest.yaml").read_text())), len(self.files))

    def test_command_line_failure_does_not_echo_invalid_metadata(self):
        """Invalid metadata fails the image build without echoing its supplied contents."""
        invalid_value = "https://user:placeholder-password@example.invalid/repo"
        self.write_metadata(repository=invalid_value)
        result = subprocess.run([sys.executable, str(HELPER_PATH), "--source", str(self.source), "--metadata", str(self.metadata)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"Image preparation failed", result.stderr)
        self.assertNotIn(invalid_value.encode(), result.stdout + result.stderr)
        self.assertFalse((self.app / "manifest.yaml").exists())


if __name__ == "__main__":
    unittest.main()
