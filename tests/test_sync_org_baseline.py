#!/usr/bin/env python3
"""Tests for local organization-baseline and skill synchronization."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "sync-org-baseline.py"
REAL_GIT = Path("/usr/bin/git")
SPEC = importlib.util.spec_from_file_location("sync_org_baseline", SCRIPT)
assert SPEC and SPEC.loader
syncer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = syncer
SPEC.loader.exec_module(syncer)


class SyncOrgBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        self.packaging = self.work / "packaging"
        self.org_profile = self.packaging / "org-profile"
        (self.org_profile / "baselines").mkdir(parents=True)
        (self.org_profile / "org-profile.yaml").write_text(
            "baseline:\n"
            "  enabled: true\n"
            "  id: aiscb-0.1.10\n"
            "  file: baselines/secure-coding-baseline.md\n",
            encoding="utf-8",
        )
        (self.org_profile / "package-policy.yaml").write_text(
            "plugin_surface:\n  skills:\n    include: []\n", encoding="utf-8"
        )
        self.org_skills = self.packaging / "org-skills"
        self.checkout = self.work / "checkout"
        self.checkout.mkdir()
        self.doc_bytes = b"# Acme Baseline\n\nbaseline-id: acme-sec-1.0.0\n\nRule.\n"
        (self.checkout / "baseline.md").write_bytes(self.doc_bytes)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def make_args(self, **overrides):
        defaults = dict(
            org_profile=self.org_profile,
            org_skills=self.org_skills,
            checkout=self.checkout,
            doc="baseline.md",
            skills_dir=None,
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def set_profile(self, baseline_id: str, baseline_file: str = "baselines/secure-coding-baseline.md") -> None:
        (self.org_profile / "org-profile.yaml").write_text(
            "baseline:\n"
            "  enabled: true\n"
            f"  id: {baseline_id}\n"
            f"  file: {baseline_file}\n",
            encoding="utf-8",
        )

    def add_skill(self, name: str, body: str = "body") -> Path:
        skill = self.checkout / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(body, encoding="utf-8")
        return skill

    def git_remote(self) -> tuple[Path, dict[str, str]]:
        source = self.work / "git-source"
        source.mkdir()
        subprocess.run([REAL_GIT, "init", "-q", "-b", "main"], cwd=source, check=True)
        subprocess.run([REAL_GIT, "config", "user.name", "Test"], cwd=source, check=True)
        subprocess.run(
            [REAL_GIT, "config", "user.email", "test@example.test"], cwd=source, check=True
        )
        (source / "dist" / "skills" / "review").mkdir(parents=True)
        (source / "dist" / "baseline.md").write_bytes(self.doc_bytes)
        (source / "dist" / "skills" / "review" / "SKILL.md").write_text(
            "skill-v1", encoding="utf-8"
        )
        subprocess.run([REAL_GIT, "add", "."], cwd=source, check=True)
        subprocess.run([REAL_GIT, "commit", "-q", "-m", "initial"], cwd=source, check=True)
        remote = self.work / "remote.git"
        subprocess.run([REAL_GIT, "clone", "-q", "--bare", str(source), str(remote)], check=True)
        ssh = self.work / "fake-ssh"
        ssh.write_text(
            "#!/usr/bin/env bash\nexec /usr/bin/git-upload-pack \"$ORG_BASELINE_TEST_REMOTE\"\n",
            encoding="utf-8",
        )
        ssh.chmod(0o755)
        real_bin = self.work / "real-git-bin"
        real_bin.mkdir()
        (real_bin / "git").symlink_to(REAL_GIT)
        environment = {
            "GIT_SSH_COMMAND": str(ssh),
            "GIT_SSH_VARIANT": "ssh",
            "ORG_BASELINE_TEST_REMOTE": str(remote),
            "PATH": f"{real_bin}:{os.environ['PATH']}",
        }
        return source, environment

    def run_main(self, *extra_args: str) -> tuple[int, str, str]:
        previous = sys.argv
        sys.argv = [
            "sync-org-baseline.py",
            "--org-profile",
            str(self.org_profile),
            "--org-skills",
            str(self.org_skills),
            "--checkout",
            str(self.checkout),
            "--doc",
            "baseline.md",
            *extra_args,
        ]
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = syncer.main()
        finally:
            sys.argv = previous
        return result, stdout.getvalue(), stderr.getvalue()

    def test_missing_source_is_an_error(self) -> None:
        with self.assertRaisesRegex(syncer.SyncError, "source not found"):
            syncer.plan(self.make_args(checkout=self.work / "missing"))

    def test_missing_document_is_an_error(self) -> None:
        with self.assertRaisesRegex(syncer.SyncError, "baseline document not found"):
            syncer.plan(self.make_args(doc="missing.md"))

    def test_document_path_traversal_is_rejected(self) -> None:
        with self.assertRaisesRegex(syncer.SyncError, "relative path"):
            syncer.plan(self.make_args(doc="../outside.md"))

    def test_document_symlink_is_rejected(self) -> None:
        (self.checkout / "baseline.md").unlink()
        outside = self.work / "outside.md"
        outside.write_bytes(self.doc_bytes)
        (self.checkout / "baseline.md").symlink_to(outside)
        with self.assertRaisesRegex(syncer.SyncError, "must not contain symlinks"):
            syncer.plan(self.make_args())

    def test_document_without_exactly_one_marker_is_rejected(self) -> None:
        (self.checkout / "baseline.md").write_text("# no marker\n", encoding="utf-8")
        with self.assertRaisesRegex(syncer.SyncError, "exactly one baseline-id"):
            syncer.plan(self.make_args())

    def test_html_document_is_named_as_html_not_as_a_missing_marker(self) -> None:
        (self.checkout / "baseline.md").write_text(
            "<!DOCTYPE html>\n<html><body>Sign in</body></html>\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(syncer.SyncError, "looks like an HTML page"):
            syncer.plan(self.make_args())

    def test_duplicate_identical_markers_are_rejected(self) -> None:
        (self.checkout / "baseline.md").write_text(
            "baseline-id: acme-sec-1.0.0\nbaseline-id: acme-sec-1.0.0\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(syncer.SyncError, "exactly one baseline-id"):
            syncer.plan(self.make_args())

    def test_invalid_utf8_document_is_rejected(self) -> None:
        (self.checkout / "baseline.md").write_bytes(b"baseline-id: acme-1\n\xff")
        with self.assertRaisesRegex(syncer.SyncError, "not UTF-8"):
            syncer.plan(self.make_args())

    def test_configured_baseline_file_is_the_only_target(self) -> None:
        self.set_profile("acme-sec-1.0.0", "baselines/shipped.md")
        (self.org_profile / "baselines" / "shipped.md").write_text("stale", encoding="utf-8")
        (self.org_profile / "baselines" / "baseline.md").write_bytes(self.doc_bytes)
        plan = syncer.plan(self.make_args())
        self.assertEqual(plan.doc_target.name, "shipped.md")
        self.assertTrue(any(change.startswith("text:") for change in plan.changes))

    def test_escaping_baseline_file_is_rejected(self) -> None:
        self.set_profile("acme-sec-1.0.0", "../escaped.md")
        with self.assertRaisesRegex(syncer.SyncError, "relative path"):
            syncer.plan(self.make_args())

    def test_remote_profile_source_is_rejected_in_organization_mode(self) -> None:
        (self.org_profile / "org-profile.yaml").write_text(
            "baseline:\n"
            "  enabled: true\n"
            "  id: acme-sec-1.0.0\n"
            "  url: https://example.test/baseline.md\n"
            "  file: baselines/secure-coding-baseline.md\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(syncer.SyncError, "reviewed baseline.file only"):
            syncer.plan(self.make_args())

    def test_symlinked_baseline_target_is_rejected(self) -> None:
        outside = self.work / "outside.md"
        outside.write_text("do not replace", encoding="utf-8")
        target = self.org_profile / "baselines" / "secure-coding-baseline.md"
        target.symlink_to(outside)
        with self.assertRaisesRegex(syncer.SyncError, "must not contain symlinks"):
            syncer.plan(self.make_args())

    def test_org_skills_target_outside_packaging_repo_is_rejected(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: outside.rmdir())
        with self.assertRaisesRegex(syncer.SyncError, "escapes the packaging repository"):
            syncer.plan(self.make_args(org_skills=outside))

    def test_id_and_text_drift_are_both_reported(self) -> None:
        target = self.org_profile / "baselines" / "secure-coding-baseline.md"
        target.write_text("stale", encoding="utf-8")
        plan = syncer.plan(self.make_args())
        self.assertIn("acme-sec-1.0.0", plan.changes[0])
        self.assertTrue(any(change.startswith("text:") for change in plan.changes))

    def test_same_size_skill_change_is_detected(self) -> None:
        self.add_skill("blueprint-loader", "v2")
        destination = self.org_skills / "blueprint-loader"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("v1", encoding="utf-8")
        plan = syncer.plan(self.make_args(skills_dir="skills"))
        self.assertIn("changed skill: blueprint-loader", plan.changes)

    def test_skill_without_skill_md_is_rejected(self) -> None:
        (self.checkout / "skills" / "broken").mkdir(parents=True)
        with self.assertRaisesRegex(syncer.SyncError, "must contain SKILL.md"):
            syncer.plan(self.make_args(skills_dir="skills"))

    def test_skill_symlink_is_rejected(self) -> None:
        outside = self.work / "outside-skill"
        outside.mkdir()
        (outside / "SKILL.md").write_text("body", encoding="utf-8")
        (self.checkout / "skills").mkdir()
        (self.checkout / "skills" / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(syncer.SyncError, "must not be a symlink"):
            syncer.plan(self.make_args(skills_dir="skills"))

    def test_removed_managed_skill_is_reported_but_manual_skill_is_left_alone(self) -> None:
        (self.checkout / "skills").mkdir()
        for name in ("removed", "manual"):
            destination = self.org_skills / name
            destination.mkdir(parents=True)
            (destination / "SKILL.md").write_text(name, encoding="utf-8")
        (self.packaging / syncer.DEFAULT_STATE_FILE).write_text(
            json.dumps({"version": 1, "managed_skills": ["removed"]}), encoding="utf-8"
        )
        plan = syncer.plan(self.make_args(skills_dir="skills"))
        self.assertEqual(plan.removed_skills, ("removed",))
        self.assertFalse(any("manual" in change for change in plan.changes))

    def test_unallowlisted_skill_is_reported_as_note_metadata(self) -> None:
        self.add_skill("blueprint-loader")
        plan = syncer.plan(self.make_args(skills_dir="skills"))
        self.assertEqual(plan.unallowlisted_skills, ("blueprint-loader",))

    def test_apply_writes_document_skills_state_and_profile_id(self) -> None:
        skill = self.add_skill("blueprint-loader", "v2")
        (skill / "extra.yaml").write_text("data", encoding="utf-8")
        plan = syncer.plan(self.make_args(skills_dir="skills"))
        syncer.apply(plan, self.org_skills, update_id=True)
        target = self.org_profile / "baselines" / "secure-coding-baseline.md"
        self.assertEqual(target.read_bytes(), self.doc_bytes)
        self.assertIn(
            "  id: acme-sec-1.0.0",
            (self.org_profile / "org-profile.yaml").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            (self.org_skills / "blueprint-loader" / "SKILL.md").read_text(encoding="utf-8"),
            "v2",
        )
        state = json.loads(
            (self.packaging / syncer.DEFAULT_STATE_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual(state["managed_skills"], ["blueprint-loader"])

    def test_profile_update_failure_leaves_document_untouched(self) -> None:
        (self.org_profile / "org-profile.yaml").write_text(
            "baseline:\n  enabled: true\n  file: baselines/secure-coding-baseline.md\n",
            encoding="utf-8",
        )
        plan = syncer.plan(self.make_args())
        with self.assertRaisesRegex(syncer.SyncError, "exactly one id field"):
            syncer.apply(plan, self.org_skills, update_id=True)
        self.assertFalse(
            (self.org_profile / "baselines" / "secure-coding-baseline.md").exists()
        )

    def test_dry_run_reports_drift_without_writing(self) -> None:
        rc, _, _ = self.run_main()
        self.assertEqual(rc, 1)
        self.assertFalse(
            (self.org_profile / "baselines" / "secure-coding-baseline.md").exists()
        )

    def test_new_id_write_requires_explicit_acceptance(self) -> None:
        rc, output, _ = self.run_main("--write")
        self.assertEqual(rc, 3)
        self.assertIn("--accept-id acme-sec-1.0.0", output)

    def test_wrong_accepted_id_is_rejected(self) -> None:
        rc, _, error = self.run_main("--write", "--accept-id", "wrong-1")
        self.assertEqual(rc, 2)
        self.assertIn("source publishes", error)

    def test_accepted_id_updates_profile_and_document_together(self) -> None:
        rc, _, _ = self.run_main("--write", "--accept-id", "acme-sec-1.0.0")
        self.assertEqual(rc, 0)
        self.assertEqual(
            (self.org_profile / "baselines" / "secure-coding-baseline.md").read_bytes(),
            self.doc_bytes,
        )
        self.assertIn(
            "id: acme-sec-1.0.0",
            (self.org_profile / "org-profile.yaml").read_text(encoding="utf-8"),
        )

    def test_print_id_validates_source_without_profile(self) -> None:
        missing_profile = self.work / "missing-profile"
        previous = sys.argv
        sys.argv = [
            "sync-org-baseline.py",
            "--org-profile",
            str(missing_profile),
            "--checkout",
            str(self.checkout),
            "--doc",
            "baseline.md",
            "--print-id",
        ]
        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                rc = syncer.main()
        finally:
            sys.argv = previous
        self.assertEqual(rc, 0)
        self.assertEqual(stdout.getvalue().strip(), "acme-sec-1.0.0")

    def test_remote_url_validation_rejects_credentials_queries_and_file_urls(self) -> None:
        for value in (
            "https://user:secret@example.test/baseline.md",
            "https://example.test/baseline.md?token=secret",
            "file:///tmp/baseline.md",
        ):
            with self.subTest(value=value), self.assertRaises(syncer.SyncError):
                syncer._validate_https_url(value)
        for value in (
            "https://user:secret@example.test/repo.git",
            "https://example.test/repo.git?token=secret",
            "file:///tmp/repo.git",
        ):
            with self.subTest(value=value), self.assertRaises(syncer.SyncError):
                syncer._validate_git_url(value)

    def test_https_source_materializes_exactly_one_document_and_no_skills(self) -> None:
        args = argparse.Namespace(
            checkout=None,
            git_url=None,
            git_ref="main",
            https_url="https://security.example.test/baseline.md",
            doc=None,
            skills_dir=None,
        )
        with mock.patch.object(syncer, "_fetch_https_document", return_value=self.doc_bytes):
            with syncer._resolved_source(args) as resolved:
                self.assertEqual((resolved.checkout / resolved.doc).read_bytes(), self.doc_bytes)
                self.assertIsNone(resolved.skills_dir)
                self.assertTrue(resolved.manage_skills)
                self.assertEqual(resolved.metadata["kind"], "https")

        args.skills_dir = "skills"
        with self.assertRaisesRegex(syncer.SyncError, "exactly one document"):
            with syncer._resolved_source(args):
                pass

    def test_git_source_materializes_only_configured_regular_blobs(self) -> None:
        commit = "a" * 40
        baseline_blob = "b" * 40
        skill_blob = "c" * 40

        def git_output(_git_dir, arguments, **_kwargs):
            if arguments[0] == "fetch":
                return b""
            if arguments[0] == "rev-parse":
                return (commit + "\n").encode("ascii")
            if arguments[0] == "ls-tree" and arguments[-1] == "dist/baseline.md":
                return f"100644 blob {baseline_blob}\tdist/baseline.md\0".encode()
            if arguments[0] == "ls-tree" and arguments[-1] == "dist/skills":
                return f"100644 blob {skill_blob}\tdist/skills/review/SKILL.md\0".encode()
            if arguments[:2] == ["cat-file", "-s"]:
                data = self.doc_bytes if arguments[2] == baseline_blob else b"skill"
                return f"{len(data)}\n".encode()
            if arguments[:2] == ["cat-file", "blob"]:
                return self.doc_bytes if arguments[2] == baseline_blob else b"skill"
            raise AssertionError(arguments)

        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(syncer, "_git_command", side_effect=git_output):
                resolved = syncer._materialize_git_source(
                    Path(temporary),
                    "https://git.example.test/security/baseline.git",
                    "main",
                    "dist/baseline.md",
                    "dist/skills",
                )
            self.assertEqual((resolved.checkout / resolved.doc).read_bytes(), self.doc_bytes)
            self.assertEqual(
                (resolved.checkout / "dist/skills/review/SKILL.md").read_bytes(), b"skill"
            )
            self.assertEqual(resolved.metadata["commit"], commit)
            self.assertTrue(resolved.manage_skills)

    def test_real_git_remote_fetch_copies_and_updates_baseline_and_skills(self) -> None:
        source, environment = self.git_remote()

        def run_git_sync(*extra: str) -> int:
            previous = sys.argv
            sys.argv = [
                "sync-org-baseline.py",
                "--org-profile",
                str(self.org_profile),
                "--org-skills",
                str(self.org_skills),
                "--git-url",
                "git@example.test:baseline.git",
                "--git-ref",
                "main",
                "--doc",
                "dist/baseline.md",
                "--skills-dir",
                "dist/skills",
                *extra,
            ]
            try:
                with mock.patch.dict(os.environ, environment):
                    return syncer.main()
            finally:
                sys.argv = previous

        self.assertEqual(run_git_sync("--accept-id", "acme-sec-1.0.0", "--write"), 0)
        target = self.org_profile / "baselines" / "secure-coding-baseline.md"
        skill = self.org_skills / "review" / "SKILL.md"
        self.assertEqual(target.read_bytes(), self.doc_bytes)
        self.assertEqual(skill.read_text(encoding="utf-8"), "skill-v1")

        (source / "dist" / "baseline.md").write_bytes(
            self.doc_bytes.replace(b"Rule.", b"Updated rule.")
        )
        (source / "dist" / "skills" / "review" / "SKILL.md").write_text(
            "skill-v2", encoding="utf-8"
        )
        subprocess.run([REAL_GIT, "add", "."], cwd=source, check=True)
        subprocess.run([REAL_GIT, "commit", "-q", "-m", "update"], cwd=source, check=True)
        subprocess.run(
            [REAL_GIT, "push", "-q", str(self.work / "remote.git"), "main"],
            cwd=source,
            check=True,
        )

        self.assertEqual(run_git_sync(), 1)
        self.assertEqual(run_git_sync("--write"), 0)
        self.assertIn(b"Updated rule.", target.read_bytes())
        self.assertEqual(skill.read_text(encoding="utf-8"), "skill-v2")
        state = json.loads(
            (self.packaging / syncer.DEFAULT_STATE_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual(state["source"]["kind"], "git")
        self.assertEqual(state["source"]["ref"], "main")

    def test_https_write_records_source_and_removes_previously_managed_skills(self) -> None:
        managed = self.org_skills / "old-managed"
        managed.mkdir(parents=True)
        (managed / "SKILL.md").write_text("old", encoding="utf-8")
        (self.packaging / syncer.DEFAULT_STATE_FILE).write_text(
            json.dumps({"version": 1, "managed_skills": ["old-managed"]}),
            encoding="utf-8",
        )
        previous = sys.argv
        sys.argv = [
            "sync-org-baseline.py",
            "--org-profile",
            str(self.org_profile),
            "--org-skills",
            str(self.org_skills),
            "--https-url",
            "https://security.example.test/baseline.md",
            "--accept-id",
            "acme-sec-1.0.0",
            "--write",
        ]
        try:
            with mock.patch.object(syncer, "_fetch_https_document", return_value=self.doc_bytes):
                self.assertEqual(syncer.main(), 0)
        finally:
            sys.argv = previous
        self.assertFalse(managed.exists())
        state = json.loads(
            (self.packaging / syncer.DEFAULT_STATE_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual(state["managed_skills"], [])
        self.assertEqual(state["source"]["kind"], "https")


if __name__ == "__main__":
    unittest.main()
