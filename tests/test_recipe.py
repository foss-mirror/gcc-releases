# SPDX-License-Identifier: GPL-3.0-or-later
"""Check explicit recipe retention and reproduction outside a Git checkout."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

import recipe

ROOT = Path(__file__).resolve().parent.parent


class RecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.output = self.root / "recipe"
        self.config = {"resolved": True}
        (self.inputs / "build.json").write_text("{}\n")
        (self.inputs / "recipe-files.txt").write_text("recipe-files.txt\nbuild.json\n")

    def test_unlisted_files_and_symlinks_never_enter_recipe_or_hash(self) -> None:
        (self.inputs / ".env").write_text("private local settings\n")
        (self.inputs / "unlisted-link").symlink_to(self.root)
        first = recipe.snapshot(self.inputs, self.output, self.config)
        self.assertEqual({path.name for path in self.output.iterdir()}, {"recipe-files.txt", "build.json"})
        self.assertEqual(json.loads((self.output / "build.json").read_text()), self.config)
        (self.inputs / ".env").write_text("different private settings\n")
        second = recipe.snapshot(self.inputs, self.root / "second", self.config)
        self.assertEqual(first, second)

    def test_listed_contents_and_resolved_configuration_affect_hash(self) -> None:
        (self.inputs / "recipe-files.txt").write_text("recipe-files.txt\nbuild.json\ntool\n")
        tool = self.inputs / "tool"
        tool.write_text("first")
        tool.chmod(0o755)
        first = recipe.snapshot(self.inputs, self.output, self.config)
        self.assertEqual((self.output / "tool").stat().st_mode & 0o777, 0o755)
        tool.write_text("second")
        second = recipe.snapshot(self.inputs, self.root / "second", self.config)
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, recipe.snapshot(self.inputs, self.root / "third", {"resolved": False}))

    def test_readonly_configuration_can_be_frozen_without_modifying_source(self) -> None:
        config = self.inputs / "build.json"
        config.chmod(0o444)
        recipe.snapshot(self.inputs, self.output, self.config)
        self.assertEqual(config.read_text(), "{}\n")
        self.assertEqual((self.output / "build.json").stat().st_mode & 0o777, 0o444)
        self.assertEqual(json.loads((self.output / "build.json").read_text()), self.config)

    def test_file_list_is_frozen_before_copying_remaining_inputs(self) -> None:
        original = (self.inputs / "recipe-files.txt").read_bytes()
        open_input = recipe.recipe_input

        def input_with_edit(root: Path, relative: Path) -> AbstractContextManager[BinaryIO]:
            if relative == Path("build.json"):
                (root / "recipe-files.txt").write_text("edited during snapshot\n")
            return open_input(root, relative)

        with patch.object(recipe, "recipe_input", side_effect=input_with_edit):
            recipe.snapshot(self.inputs, self.output, self.config)
        self.assertEqual((self.output / "recipe-files.txt").read_bytes(), original)

    def test_invalid_duplicate_or_incomplete_file_lists_are_rejected(self) -> None:
        for entry in ("../private", "/private", "nested/../private", "./private", "nested//private", ".", "build.json"):
            (self.inputs / "recipe-files.txt").write_text(f"recipe-files.txt\nbuild.json\n{entry}\n")
            with self.subTest(entry=entry), self.assertRaisesRegex(ValueError, "unique, normalized"):
                recipe.snapshot(self.inputs, self.output, self.config)
            self.assertFalse(self.output.exists())
        for entries in ("build.json\n", "recipe-files.txt\n", "# no files\n"):
            (self.inputs / "recipe-files.txt").write_text(entries)
            with self.subTest(entries=entries), self.assertRaisesRegex(ValueError, "retain itself"):
                recipe.snapshot(self.inputs, self.output, self.config)

    def test_missing_inputs_symlinks_and_nonregular_files_are_rejected(self) -> None:
        (self.inputs / "recipe-files.txt").write_text("recipe-files.txt\nbuild.json\nselected\n")
        selected = self.inputs / "selected"
        with self.assertRaises(FileNotFoundError):
            recipe.snapshot(self.inputs, self.root / "missing", self.config)
        selected.symlink_to(self.inputs / "build.json")
        with self.assertRaises(OSError):
            recipe.snapshot(self.inputs, self.root / "symlink", self.config)
        selected.unlink()
        selected.mkdir()
        with self.assertRaisesRegex(ValueError, "regular file"):
            recipe.snapshot(self.inputs, self.root / "directory", self.config)
        selected.rmdir()
        os.mkfifo(selected)
        with self.assertRaisesRegex(ValueError, "regular file"):
            recipe.snapshot(self.inputs, self.root / "fifo", self.config)

    def test_parent_directory_and_file_list_symlinks_are_rejected(self) -> None:
        (self.inputs / "recipe-files.txt").write_text("recipe-files.txt\nbuild.json\nlinked/build.json\n")
        (self.inputs / "linked").symlink_to(self.inputs, target_is_directory=True)
        with self.assertRaises(OSError):
            recipe.snapshot(self.inputs, self.root / "parent-link", self.config)
        listing = self.inputs / "recipe-files.txt"
        listing.unlink()
        listing.symlink_to(self.inputs / "build.json")
        with self.assertRaises(OSError):
            recipe.snapshot(self.inputs, self.root / "list-link", self.config)

    def test_shipped_recipe_refreezes_without_git_and_has_runnable_entrypoints(self) -> None:
        config = json.loads((ROOT / "build.json").read_text())
        first = recipe.snapshot(ROOT, self.output, config)
        self.assertFalse((self.output / ".git").exists())
        second = recipe.snapshot(self.output, self.root / "refrozen", config)
        self.assertEqual(first, second)
        for name in ("build", "discover", "finalize", "portability", "publish", "refresh_sdk", "verify"):
            with self.subTest(script=name):
                subprocess.run(
                    [sys.executable, str(self.output / "scripts" / (name + ".py")), "--help"],
                    cwd=self.output,
                    env={**os.environ, "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"},
                    capture_output=True,
                    check=True,
                )
        for source in (config["gcc"], config["binutils"]):
            for key in source.get("keys", [source.get("key")]):
                self.assertTrue((self.output / key).is_file())
        self.assertTrue((self.output / "keys/RPM-GPG-KEY-EPEL-8").is_file())
