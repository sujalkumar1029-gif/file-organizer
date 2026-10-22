#!/usr/bin/env python3
"""Tests for organize.py. Run: python3 -m unittest -v test_organize.py"""

import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import organize  # noqa: E402

ENV_KEYS = ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "FILE_ORGANIZER_CONFIG")


class OrganizerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.src = self.home / "Downloads"
        self.src.mkdir(parents=True)
        self.state = base / "state"
        self.conf = base / "conf"
        self._saved = {key: os.environ.get(key) for key in ENV_KEYS}
        os.environ["XDG_STATE_HOME"] = str(self.state)
        os.environ["XDG_CONFIG_HOME"] = str(self.conf)
        os.environ.pop("FILE_ORGANIZER_CONFIG", None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def make(self, name, data=b"hello", age_hours=0.0):
        path = self.src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if age_hours:
            stamp = time.time() - age_hours * 3600
            os.utime(path, (stamp, stamp))
        return path

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = organize.main(list(args))
        return code, out.getvalue() + err.getvalue()

    def run_args(self, *extra):
        return ("run", str(self.src), "--min-age", "0", *extra)


class DryRun(OrganizerTest):
    def test_dry_run_moves_nothing(self):
        image = self.make("photo.png")
        report = self.make("report.pdf")
        code, output = self.cli(*self.run_args())
        self.assertEqual(code, 0)
        self.assertTrue(image.exists() and report.exists())
        self.assertIn("Dry run", output)
        self.assertIn("Images", output)
        self.assertIn("PDFs", output)
        self.assertFalse((self.src / "Images").exists())

    def test_recent_files_are_left_alone(self):
        self.make("just-downloaded.jpg")
        code, output = self.cli("run", str(self.src), "--min-age", "60")
        self.assertEqual(code, 0)
        self.assertTrue((self.src / "just-downloaded.jpg").exists())
        self.assertIn("modified moments ago", output)


class ApplyAndUndo(OrganizerTest):
    def test_apply_sorts_files(self):
        image = self.make("photo.png")
        pdf = self.make("report.pdf")
        archive = self.make("bundle.zip")
        code, _ = self.cli(*self.run_args("--apply"))
        self.assertEqual(code, 0)
        self.assertTrue((self.src / "Images" / "photo.png").exists())
        self.assertTrue((self.src / "PDFs" / "report.pdf").exists())
        self.assertTrue((self.src / "Archives" / "bundle.zip").exists())
        self.assertFalse(image.exists() or pdf.exists() or archive.exists())

    def test_screenshot_pattern(self):
        shot = self.make("Screenshot From 2026-10-22 10-00-00.png")
        self.cli(*self.run_args("--apply"))
        self.assertTrue((self.src / "Screenshots" / shot.name).exists())
        self.assertFalse((self.src / "Images" / shot.name).exists())

    def test_existing_file_is_never_overwritten(self):
        self.make("pic.png", b"new")
        existing = self.src / "Images" / "pic.png"
        existing.parent.mkdir()
        existing.write_bytes(b"old")
        self.cli(*self.run_args("--apply"))
        self.assertEqual(existing.read_bytes(), b"old")
        moved = sorted(p.name for p in (self.src / "Images").iterdir())
        self.assertEqual(moved, ["pic (2).png", "pic.png"])

    def test_undo_restores_everything(self):
        image = self.make("photo.png")
        pdf = self.make("report.pdf")
        self.cli(*self.run_args("--apply"))
        self.assertFalse(image.exists())
        code, output = self.cli("undo", "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(image.exists())
        self.assertTrue(pdf.exists())
        self.assertFalse((self.src / "Images").exists())
        self.assertIn("restored 2", output)

    def test_undo_dry_run_keeps_state(self):
        self.make("photo.png")
        self.cli(*self.run_args("--apply"))
        code, output = self.cli("undo")
        self.assertEqual(code, 0)
        self.assertTrue((self.src / "Images" / "photo.png").exists())
        self.assertIn("would restore", output)

    def test_journal_written(self):
        self.make("photo.png")
        self.cli(*self.run_args("--apply"))
        runs = organize.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertIn('"moves"', runs[0].read_text())


class Downloads(OrganizerTest):
    def test_fresh_partial_download_is_skipped(self):
        self.make("film.mp4.part", b"x" * 10, age_hours=1)
        code, output = self.cli(*self.run_args("--apply"))
        self.assertEqual(code, 0)
        self.assertTrue((self.src / "film.mp4.part").exists())
        self.assertIn("download in progress", output)

    def test_stale_partial_goes_to_incomplete(self):
        self.make("film.mp4.part", b"x" * 10, age_hours=48)
        self.cli(*self.run_args("--apply"))
        self.assertTrue((self.src / "Incomplete" / "film.mp4.part").exists())

    def test_stale_empty_file_goes_to_incomplete(self):
        self.make("broken.AppImage", b"", age_hours=48)
        self.cli(*self.run_args("--apply"))
        self.assertTrue((self.src / "Incomplete" / "broken.AppImage").exists())

    def test_hidden_and_symlinks_untouched(self):
        hidden = self.make(".secret")
        real = self.make("real.png")
        link = self.src / "link.png"
        link.symlink_to(real)
        self.cli(*self.run_args("--apply"))
        self.assertTrue(hidden.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue((self.src / "Images" / "real.png").exists())


class Folders(OrganizerTest):
    def test_top_level_folders_are_classified(self):
        (self.src / "my-repo" / ".git").mkdir(parents=True)
        (self.src / "old-backup").mkdir()
        (self.src / "random-folder").mkdir()
        self.cli(*self.run_args("--include-folders", "--apply"))
        self.assertTrue((self.src / "Projects" / "my-repo").is_dir())
        self.assertTrue((self.src / "Backups" / "old-backup").is_dir())
        self.assertTrue((self.src / "Folders" / "random-folder").is_dir())

    def test_folders_untouched_without_flag(self):
        (self.src / "my-repo" / ".git").mkdir(parents=True)
        self.cli(*self.run_args("--apply"))
        self.assertTrue((self.src / "my-repo").is_dir())
        self.assertFalse((self.src / "Projects").exists())

    def test_recursive_skips_projects_and_output_dirs(self):
        self.make("sub/nested.pdf")
        (self.src / "repo" / ".git").mkdir(parents=True)
        self.make("repo/main.py")
        self.make("Images/already.png")
        self.cli(*self.run_args("--recursive", "--apply"))
        self.assertTrue((self.src / "PDFs" / "nested.pdf").exists())
        self.assertTrue((self.src / "repo" / "main.py").exists())
        self.assertTrue((self.src / "Images" / "already.png").exists())

    def test_recursive_and_folders_conflict(self):
        code, output = self.cli(*self.run_args("--recursive", "--include-folders"))
        self.assertEqual(code, 2)
        self.assertIn("cannot be combined", output)


class Extras(OrganizerTest):
    def test_dedupe_moves_copies(self):
        self.make("a.png", b"same-bytes")
        self.make("b.png", b"same-bytes")
        self.cli(*self.run_args("--dedupe", "--apply"))
        self.assertTrue((self.src / "Images" / "a.png").exists())
        self.assertTrue((self.src / "Duplicates" / "b.png").exists())

    def test_json_plan(self):
        import json
        self.make("photo.png")
        code, output = self.cli(*self.run_args("--json"))
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["summary"]["items"], 1)
        self.assertEqual(payload["moves"][0]["category"], "Images")

    def test_date_folders(self):
        self.make("photo.png", age_hours=24 * 40)
        self.cli(*self.run_args("--date-folders", "--apply"))
        matches = list((self.src / "Images").glob("*/[0-9][0-9]/photo.png"))
        self.assertEqual(len(matches), 1)

    def test_refuses_root(self):
        code, output = self.cli("run", "/", "--json")
        self.assertEqual(code, 2)
        self.assertIn("refusing", output)

    def test_config_init(self):
        code, output = self.cli("config", "--init")
        self.assertEqual(code, 0)
        path = Path(os.environ["XDG_CONFIG_HOME"]) / "file-organizer" / "rules.toml"
        self.assertTrue(path.exists())
        self.assertEqual(organize.load_config(path)["categories"]["PDFs"], ["pdf"])


if __name__ == "__main__":
    unittest.main()
