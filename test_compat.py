#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# ///
"""Offline configuration migration tests: temporary files and mocked Codex only."""

import contextlib
import io
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import witches

LEGACY = """# user comment
openai_base_url = "http://127.0.0.1:10777/backend-api/codex" # witches
disable_response_storage = true
[features]
child_agents_md = true
codex_hooks = true # preserve hook comment
js_repl = false
[env]
USE_OMX_EXPLORE_CMD = "1"
[mcp_servers."browser.test"]
type = "stdio"
command = "node"
args = ["keep", "these"]
[mcp_servers."browser.test".env]
FOO = "untouched"
[shell_environment_policy.set]
EXISTING = "unchanged"
[projects."/a/b"]
trust_level = "trusted"
"""


class PlanTests(unittest.TestCase):
    def test_full_migration_preserves_unrelated_settings_and_is_idempotent(self):
        text, changes = witches.codex_compat_plan(LEGACY)
        d = tomllib.loads(text)
        self.assertEqual(len(changes), 5)
        self.assertNotIn("disable_response_storage", d)
        self.assertNotIn("env", d)
        self.assertEqual(d["features"], {"hooks": True, "js_repl": False})
        self.assertEqual(
            d["shell_environment_policy"]["set"],
            {"USE_OMX_EXPLORE_CMD": "1", "EXISTING": "unchanged"},
        )
        self.assertEqual(
            d["mcp_servers"]["browser.test"],
            {"command": "node", "args": ["keep", "these"], "env": {"FOO": "untouched"}},
        )
        self.assertEqual(d["projects"], tomllib.loads(LEGACY)["projects"])
        self.assertIn("# preserve hook comment", text)
        self.assertTrue(text.startswith("# user comment"))
        self.assertEqual(witches.codex_compat_plan(text), (text, []))

    def test_quoted_dotted_keys_and_multiline_strings(self):
        before = """note = '''
[env]
REMOVE = "not really a setting"
'''
"features"."codex_hooks" = false
["env"]
"A.B" = "quoted"
"""
        text, _ = witches.codex_compat_plan(before)
        d = tomllib.loads(text)
        self.assertEqual(d["note"], tomllib.loads(before)["note"])
        self.assertEqual(d["features"]["hooks"], False)
        self.assertEqual(d["shell_environment_policy"]["set"], {"A.B": "quoted"})

    def test_new_env_table_with_parent_and_without_parent(self):
        for suffix in ("", '\n[shell_environment_policy]\ninherit = "all"\n'):
            text, _ = witches.codex_compat_plan('[env]\nA = "1"\n' + suffix)
            self.assertEqual(
                tomllib.loads(text)["shell_environment_policy"]["set"], {"A": "1"}
            )

    def test_equal_duplicates_and_empty_env(self):
        for text in (
            "[env]\n",
            '[env]\nA="1"\n[shell_environment_policy.set]\nA="1"\n',
            "[features]\ncodex_hooks=true\nhooks=true\n",
        ):
            updated, _ = witches.codex_compat_plan(text)
            self.assertEqual(witches.codex_compat_plan(updated), (updated, []))

    def test_conflicts_fail_without_silently_choosing(self):
        for text in (
            "[features]\ncodex_hooks=true\nhooks=false\n",
            '[env]\nA="legacy"\n[shell_environment_policy.set]\nA="new"\n',
            "[env]\nA=42\n",
            '[features]\ncodex_hooks="yes"\n',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                witches.codex_compat_plan(text)

    def test_unsupported_inline_form_fails_closed(self):
        with self.assertRaises(ValueError):
            witches.codex_compat_plan("features = { codex_hooks = true }\n")

    def test_mcp_transport_validation(self):
        text, _ = witches.codex_compat_plan(
            '[mcp_servers.remote]\ntype="http"\nurl="https://example.invalid"\n'
        )
        self.assertNotIn("type", tomllib.loads(text)["mcp_servers"]["remote"])
        for body in (
            'type="stdio"\nurl="https://example.invalid"',
            'type="sse"\nurl="https://example.invalid"',
            'type="stdio"\ncommand="node"\nurl="https://example.invalid"',
        ):
            with self.assertRaises(ValueError):
                witches.codex_compat_plan("[mcp_servers.x]\n" + body)

    def test_header_comments_and_quoted_brackets(self):
        text = '[features] # comment with ]\ncodex_hooks = true\n[mcp_servers."a]b"] # ]\ntype = "stdio"\ncommand = "node"\n'
        updated, _ = witches.codex_compat_plan(text)
        self.assertTrue(tomllib.loads(updated)["features"]["hooks"])
        self.assertNotIn("type", tomllib.loads(updated)["mcp_servers"]["a]b"])

    def test_unknown_keys_preserved_for_validator(self):
        text = 'unknown_key="keep"\n[features]\nfuture_flag=true\n'
        self.assertEqual(witches.codex_compat_plan(text), (text, []))


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / "config.toml"
        self.path.write_text(LEGACY)
        self.home_patch = patch.object(witches, "CODEX_HOME", self.home)
        self.home_patch.start()
        self.addCleanup(self.home_patch.stop)
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(contextlib.redirect_stderr(self.output))

    def test_preview_then_backup_atomic_fix_and_repeat(self):
        with patch.object(
            witches, "check_codex_config", return_value="codex-cli 0.153.4"
        ):
            self.assertEqual(witches.doctor(), 0)
            self.assertEqual(self.path.read_text(), LEGACY)
            self.assertEqual(list(self.home.glob("*.bak")), [])
            self.assertEqual(witches.doctor(True), 0)
            backups = list(self.home.glob("*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), LEGACY)
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            updated = self.path.read_bytes()
            self.assertEqual(witches.doctor(True), 0)
            self.assertEqual(self.path.read_bytes(), updated)
            self.assertEqual(list(self.home.glob("*.bak")), backups)

    def test_failed_validation_writes_nothing(self):
        with patch.object(
            witches, "check_codex_config", side_effect=ValueError("校验失败")
        ):
            self.assertEqual(witches.doctor(True), 1)
        self.assertEqual(self.path.read_text(), LEGACY)
        self.assertEqual(list(self.home.glob("*.bak")), [])

    def test_concurrent_edit_is_preserved(self):
        def validate(*_):
            self.path.write_text("# concurrent update\n")
            return "codex-cli 0.153.4"

        with patch.object(witches, "check_codex_config", side_effect=validate):
            self.assertEqual(witches.doctor(True), 1)
        self.assertEqual(self.path.read_text(), "# concurrent update\n")
        self.assertEqual(list(self.home.glob("*.bak")), [])

    def test_symlink_not_replaced(self):
        target = self.home / "managed.toml"
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assertEqual(witches.doctor(True), 1)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), LEGACY)

    def test_invalid_toml_does_not_print_values(self):
        self.path.write_text("api_key = SECRET_MUST_NOT_BE_PRINTED\n")
        self.assertEqual(witches.doctor(True), 1)
        self.assertNotIn("SECRET_MUST_NOT_BE_PRINTED", self.output.getvalue())

    def test_failed_atomic_replace_retains_original_and_backup(self):
        with (
            patch.object(
                witches, "check_codex_config", return_value="codex-cli 0.153.4"
            ),
            patch.object(witches.os, "replace", side_effect=OSError("private-secret")),
        ):
            self.assertEqual(witches.doctor(True), 1)
        self.assertEqual(self.path.read_text(), LEGACY)
        self.assertEqual(len(list(self.home.glob("*.bak"))), 1)
        self.assertEqual(list(self.home.glob(".witches-doctor-*")), [])
        self.assertNotIn("private-secret", self.output.getvalue())


class ValidationTests(unittest.TestCase):
    def test_isolated_eof_validation(self):
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            if args[-1] == "--version":
                return subprocess.CompletedProcess(args, 0, "codex-cli 0.153.4\n", "")
            self.assertEqual(
                args, ["/fake/codex", "--strict-config", "app-server", "--stdio"]
            )
            self.assertEqual(kwargs["input"], "")
            home = Path(kwargs["env"]["CODEX_HOME"])
            self.assertEqual(kwargs["cwd"], str(home))
            self.assertEqual(
                (home / "config.toml").read_text(), "[features]\nhooks=true\n"
            )
            self.assertEqual((home / "config.toml").stat().st_mode & 0o777, 0o600)
            self.assertFalse((home / "auth.json").exists())
            return subprocess.CompletedProcess(args, 0, "", "")

        with patch.object(witches.subprocess, "run", side_effect=run):
            self.assertEqual(
                witches.check_codex_config("[features]\nhooks=true\n", "/fake/codex"),
                "codex-cli 0.153.4",
            )
        self.assertEqual(len(calls), 2)

    def test_old_codex_or_timeout_fails_closed(self):
        for response in (
            subprocess.CompletedProcess([], 0, "codex-cli 0.152.0", ""),
            subprocess.CompletedProcess([], 0, "unknown version", ""),
        ):
            with (
                patch.object(witches.subprocess, "run", return_value=response),
                self.assertRaises(ValueError),
            ):
                witches.check_codex_config("")
        with (
            patch.object(
                witches.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired("codex", 10),
            ),
            self.assertRaises(ValueError),
        ):
            witches.check_codex_config("")

    def test_unknown_field_report_redacts_error_contents(self):
        responses = [
            subprocess.CompletedProcess([], 0, "codex-cli 0.153.4", ""),
            subprocess.CompletedProcess(
                [], 1, "", "SECRET unknown configuration field `new.key`"
            ),
        ]
        with (
            patch.object(witches.subprocess, "run", side_effect=responses),
            self.assertRaises(ValueError) as cm,
        ):
            witches.check_codex_config("")
        self.assertIn("new.key", str(cm.exception))
        self.assertNotIn("SECRET", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
