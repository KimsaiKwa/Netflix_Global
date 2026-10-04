"""Offline regressions for both GitHub Actions writers (stdlib + local Git only)."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import publish_data

ROOT = Path(__file__).resolve().parents[1]


def record(value, timestamp=1, version=2):
    return {"saved_at": timestamp, "match_version": version, "meta": {"cn_title": value}}


def cache(items):
    return {"version": 1, "updated_at": "2026-10-04T00:00:00+00:00", "items": items}


class CacheMergeTests(unittest.TestCase):
    def test_changed_records_merge_without_losing_newer_remote_work(self):
        base = cache({"same": record("base"), "untouched": record("old")})
        local = cache({"same": record("local", 2), "untouched": record("old"),
                       "new-local": record("local", 3)})
        remote = cache({"same": record("remote", 4), "untouched": record("new", 5),
                        "new-remote": record("remote", 6)})
        result = publish_data.merge_cache(base, local, remote)
        self.assertEqual(result["items"], {**remote["items"], "new-local": record("local", 3)})
        self.assertNotIn("new-local", remote["items"], "merge must not mutate inputs")

    def test_newer_match_version_wins_before_timestamp(self):
        base = cache({"film": record("legacy", 10, 1)})
        local = cache({"film": record("strict", 11, 2)})
        remote = cache({"film": record("fuzzy", 99, 1)})
        self.assertEqual(publish_data.merge_cache(base, local, remote)["items"], local["items"])
        self.assertEqual(publish_data.merge_cache(base, remote, local)["items"], local["items"])

    def test_deletions_and_timestamp_ties_preserve_remote_intent(self):
        base = cache({"deleted": record("base"), "tie": record("base")})
        local = cache({"deleted": record("updated", 20), "tie": record("local", 2)})
        remote = cache({"tie": record("remote", 2)})
        self.assertEqual(publish_data.merge_cache(base, local, remote), remote)
        self.assertEqual(publish_data.merge_cache(base, cache({}), base)["items"], {})

    def test_unchanged_local_entry_never_reverts_remote_edit(self):
        base = cache({"film": record("base", 500)})
        remote = cache({"film": record("corrected", 1)})
        self.assertEqual(publish_data.merge_cache(base, base, remote), remote)

    def test_current_identity_beats_newer_obsolete_remote_record(self):
        base = cache({"film": record("old identity", 1)})
        local = cache({"film": {**record("correct identity", 2), "identity_revision": "current"}})
        remote = cache({"film": {**record("wrong identity", 99), "identity_revision": "obsolete"}})
        result = publish_data.merge_cache(base, local, remote, {"film": "current"})
        self.assertEqual(result["items"], local["items"])

    def test_obsolete_local_identity_never_replaces_current_remote(self):
        base = cache({"film": record("base", 1)})
        local = cache({"film": {**record("obsolete", 999), "identity_revision": "old"}})
        remote = cache({"film": {**record("current", 2), "identity_revision": "new"}})
        self.assertEqual(publish_data.merge_cache(base, local, remote, {"film": "new"}), remote)
        # Even if no concurrent record edit occurred, obsolete local work is skipped.
        self.assertEqual(publish_data.merge_cache(base, local, base, {"film": "new"}), base)

    def test_removed_identity_accepts_revalidation_without_old_revision(self):
        base = cache({"film": {**record("removed", 1), "identity_revision": "old"}})
        local = cache({"film": record("revalidated", 2)})
        remote = cache({"film": {**record("obsolete", 999), "identity_revision": "old"}})
        self.assertEqual(publish_data.merge_cache(base, local, remote, {})["items"], local["items"])

    def test_version_one_cache_migrates_to_two_with_local_progress(self):
        base = cache({"film": record("v1", 1, 1)})
        local = cache({"film": record("v2", 2, 2)})
        local["version"] = 2
        result = publish_data.merge_cache(base, local, base)
        self.assertEqual(result["version"], 2)
        self.assertEqual(result["items"], local["items"])

    def test_concurrent_migration_preserves_both_writers_and_never_downgrades(self):
        base = cache({"film": record("old", 1, 1)})
        local = cache({"film": record("old", 1, 1), "local": record("strict", 2)})
        local["version"] = 2
        remote = cache({"film": record("remote strict", 3), "remote": record("strict", 3)})
        remote["version"] = 2
        result = publish_data.merge_cache(base, local, remote)
        self.assertEqual(result["version"], 2)
        self.assertEqual(set(result["items"]), {"film", "local", "remote"})
        self.assertEqual(result["items"]["film"], remote["items"]["film"])
        self.assertEqual(publish_data.merge_cache(base, base, remote)["version"], 2)

    def test_schema_conflict_is_explicit(self):
        remote = cache({})
        remote["version"] = 3
        with self.assertRaisesRegex(ValueError, "cache schema changed"):
            publish_data.merge_cache(cache({}), cache({}), remote)


class PublicationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.runner = self.root / "runner"
        self.other = self.root / "other"
        self.command("git", "init", "--bare", "--initial-branch=main", str(self.origin))
        self.command("git", "init", "--initial-branch=main", str(self.seed))
        self.configure(self.seed)
        self.write(self.seed, "global.json", {"week": "2026-09-27", "healthy": True})
        self.write(self.seed, "countries/us.json", {"week": "2026-09-27", "title": "original"})
        self.write(self.seed, "metadata_cache.json", cache({"film": record("base")}))
        self.write(self.seed, "README.md", "original documentation\n")
        self.write(self.seed, "health_check.py", "import json\nimport sys\nsys.exit(0 if json.load(open('global.json')).get('healthy') else 1)\n")
        shutil.copy2(ROOT / "publish_data.py", self.seed / "publish_data.py")
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Initial snapshot")
        self.git(self.seed, "remote", "add", "origin", str(self.origin))
        self.git(self.seed, "push", "-u", "origin", "main")
        for dest in (self.runner, self.other):
            self.command("git", "clone", str(self.origin), str(dest))
            self.configure(dest)

    @staticmethod
    def command(*args, check=True):
        return subprocess.run(args, text=True, capture_output=True, check=check)

    def git(self, repo, *args, check=True):
        return self.command("git", "-C", str(repo), *args, check=check)

    def configure(self, repo):
        self.git(repo, "config", "user.name", "Offline Test")
        self.git(repo, "config", "user.email", "test@example.invalid")

    @staticmethod
    def write(repo, name, value):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2) + "\n")

    def remote_json(self, path):
        return json.loads(self.git(self.origin, "show", f"main:{path}").stdout)

    def remote_text(self, path):
        return self.git(self.origin, "show", f"main:{path}").stdout

    def update_remote(self, changes):
        for path, value in changes.items():
            self.write(self.other, path, value)
        self.git(self.other, "add", ".")
        self.git(self.other, "commit", "-m", "Concurrent remote changes")
        self.git(self.other, "push", "origin", "main")

    def publish(self, mode="cache", **kwargs):
        return publish_data.publish(self.runner, mode=mode, message="Publish test [skip ci]", **kwargs)

    def test_unhealthy_cache_only_publishes_from_dirty_worktree(self):
        original_global = self.remote_text("global.json")
        original_country = self.remote_text("countries/us.json")
        self.write(self.runner, "global.json", {"healthy": False})
        self.write(self.runner, "countries/us.json", {"title": "incomplete metadata"})
        self.write(self.runner, "metadata_cache.json", cache({"film": record("progress", 3)}))
        self.write(self.runner, "unrelated.txt", "leave untouched\n")
        self.write(self.runner, "diagnostics.txt", "coverage below threshold\n")
        # Even an existing staged generator change must never leak into publication.
        self.git(self.runner, "add", "global.json")
        before_status = self.git(self.runner, "status", "--porcelain").stdout
        result = self.publish(diagnostics="publish")
        self.assertIsNotNone(result.commit)
        self.assertEqual(self.remote_text("global.json"), original_global)
        self.assertEqual(self.remote_text("countries/us.json"), original_country)
        self.assertEqual(self.remote_json("metadata_cache.json")["items"]["film"], record("progress", 3))
        self.assertEqual(self.remote_text("diagnostics.txt"), "coverage below threshold\n")
        self.assertEqual(self.git(self.runner, "status", "--porcelain").stdout, before_status)
        self.assertEqual((self.runner / "unrelated.txt").read_text(), "leave untouched\n")

    def test_first_real_cache_publication_migrates_v1_to_v2(self):
        updated = cache({"film": record("strict progress", 2)})
        updated["version"] = 2
        self.write(self.runner, "metadata_cache.json", updated)
        self.write(self.runner, "global.json", {"healthy": False})
        self.publish()
        self.assertEqual(self.remote_json("metadata_cache.json"), updated)
        self.assertTrue(self.remote_json("global.json")["healthy"])

    def test_cache_only_keeps_concurrent_remote_edits_and_fresh_cache(self):
        self.write(self.runner, "metadata_cache.json", cache({"film": record("local", 3), "local": record("new", 2)}))
        self.update_remote({"README.md": "remote edit\n", "metadata_cache.json": cache({
            "film": record("remote fresher", 5), "remote": record("also new", 4),
        })})
        self.publish()
        self.assertEqual(self.remote_text("README.md"), "remote edit\n")
        items = self.remote_json("metadata_cache.json")["items"]
        self.assertEqual(items["film"], record("remote fresher", 5))
        self.assertEqual(set(items), {"film", "local", "remote"})

    def test_real_remote_identity_manifest_controls_cache_merge(self):
        # Exercise the real local-only identity loader, not a second hashing algorithm.
        for name in ("enrich_metadata.py", "verified_title_aliases.json"):
            shutil.copy2(ROOT / name, self.seed / name)
        self.git(self.seed, "add", ".")
        self.git(self.seed, "commit", "-m", "Add reviewed identities")
        self.git(self.seed, "push", "origin", "main")
        for checkout in (self.runner, self.other):
            self.git(checkout, "pull", "--ff-only")
        revisions = publish_data.current_identity_revisions(self.runner)
        self.assertTrue(revisions)
        key, revision = next(iter(revisions.items()))
        local = cache({"film": record("base"), key: {
            **record("verified identity", 2), "identity_revision": revision,
        }})
        self.write(self.runner, "metadata_cache.json", local)
        self.update_remote({"metadata_cache.json": cache({"film": record("base"), key: {
            **record("obsolete identity", 999), "identity_revision": "obsolete",
        }})})
        self.publish()
        self.assertEqual(self.remote_json("metadata_cache.json")["items"][key], local["items"][key])

    def test_full_publish_keeps_unrelated_remote_changes(self):
        self.write(self.runner, "global.json", {"healthy": True, "title": "new validated mirror"})
        self.write(self.runner, "countries/us.json", {"title": "new validated country"})
        self.update_remote({"README.md": "remote documentation\n"})
        result = self.publish(mode="full")
        self.assertIsNone(result.cache_only_reason)
        self.assertEqual(self.remote_json("global.json")["title"], "new validated mirror")
        self.assertEqual(self.remote_json("countries/us.json")["title"], "new validated country")
        self.assertEqual(self.remote_text("README.md"), "remote documentation\n")

    def test_stale_mirror_preserves_remote_rankings_and_local_cache(self):
        self.write(self.runner, "global.json", {"healthy": True, "title": "old run"})
        self.write(self.runner, "metadata_cache.json", cache({"film": record("useful local work", 2)}))
        self.update_remote({"global.json": {"healthy": True, "week": "2026-10-04", "title": "newer run"}})
        result = self.publish(mode="full")
        self.assertIn("global.json", result.cache_only_reason)
        self.assertEqual(self.remote_json("global.json")["title"], "newer run")
        self.assertEqual(self.remote_json("metadata_cache.json")["items"]["film"], record("useful local work", 2))

    def test_newer_remote_cache_prevents_publishing_older_metadata(self):
        self.write(self.runner, "global.json", {"healthy": True, "title": "old metadata"})
        self.update_remote({"metadata_cache.json": cache({"film": record("newer metadata", 50)})})
        result = self.publish(mode="full")
        self.assertIn("metadata_cache.json", result.cache_only_reason)
        self.assertNotIn("title", self.remote_json("global.json"))
        self.assertEqual(self.remote_json("metadata_cache.json")["items"]["film"], record("newer metadata", 50))

    def test_revalidation_failure_preserves_cache_but_not_unhealthy_mirror(self):
        self.write(self.runner, "global.json", {"healthy": False})
        self.write(self.runner, "metadata_cache.json", cache({"film": record("progress", 2)}))
        result = self.publish(mode="full")
        self.assertIn("health check failed", result.cache_only_reason)
        self.assertTrue(self.remote_json("global.json")["healthy"])
        self.assertEqual(self.remote_json("metadata_cache.json")["items"]["film"], record("progress", 2))

    def test_non_fast_forward_push_rebuilds_from_new_remote(self):
        self.write(self.runner, "metadata_cache.json", cache({"film": record("local", 2)}))
        original_git = publish_data.git
        raced = False

        def race(repo, *args, **kwargs):
            nonlocal raced
            if args and args[0] == "push" and not raced:
                raced = True
                self.update_remote({"README.md": "late remote edit\n", "metadata_cache.json": cache({
                    "film": record("base"), "remote": record("late remote progress", 3),
                })})
            return original_git(repo, *args, **kwargs)

        with patch.object(publish_data, "git", side_effect=race):
            result = self.publish()
        self.assertTrue(raced)
        self.assertIsNotNone(result.commit)
        self.assertEqual(self.remote_text("README.md"), "late remote edit\n")
        self.assertEqual(self.remote_json("metadata_cache.json")["items"], {
            "film": record("local", 2), "remote": record("late remote progress", 3),
        })
        # The runner's source checkout has not been reset, stashed or rebased.
        self.assertEqual(self.git(self.runner, "rev-parse", "HEAD").stdout,
                         self.git(self.seed, "rev-parse", "HEAD").stdout)

    def test_full_publish_race_downgrades_safely_if_rankings_advance(self):
        self.write(self.runner, "global.json", {"healthy": True, "title": "stale local run"})
        self.write(self.runner, "metadata_cache.json", cache({"film": record("progress", 2)}))
        original_git = publish_data.git
        raced = False

        def race(repo, *args, **kwargs):
            nonlocal raced
            if args and args[0] == "push" and not raced:
                raced = True
                self.update_remote({"global.json": {"healthy": True, "title": "newer remote run"}})
            return original_git(repo, *args, **kwargs)

        with patch.object(publish_data, "git", side_effect=race):
            result = self.publish(mode="full")
        self.assertIn("global.json", result.cache_only_reason)
        self.assertEqual(self.remote_json("global.json")["title"], "newer remote run")
        self.assertEqual(self.remote_json("metadata_cache.json")["items"]["film"], record("progress", 2))

    def test_push_permission_failure_is_reported_without_retries_or_dirty_rebase(self):
        self.write(self.runner, "metadata_cache.json", cache({"film": record("progress", 2)}))
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'push deliberately rejected for offline test' >&2\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "Publication push failed"):
            self.publish()
        self.assertEqual(self.remote_json("metadata_cache.json")["items"]["film"], record("base"))
        self.assertEqual(len(self.git(self.runner, "worktree", "list", "--porcelain").stdout.split("worktree ")) - 1, 1)

    def test_shallow_actions_checkout_publishes_after_remote_advance(self):
        shallow = self.root / "shallow-runner"
        self.command("git", "clone", "--depth=1", self.origin.as_uri(), str(shallow))
        self.runner = shallow
        self.configure(self.runner)
        self.assertEqual(self.git(self.runner, "rev-parse", "--is-shallow-repository").stdout.strip(), "true")
        self.write(self.runner, "metadata_cache.json", cache({"film": record("progress", 2)}))
        self.write(self.runner, "global.json", {"healthy": False})
        self.update_remote({"README.md": "edit after shallow checkout\n"})
        self.publish()
        self.assertEqual(self.remote_text("README.md"), "edit after shallow checkout\n")
        self.assertEqual(self.remote_json("metadata_cache.json")["items"]["film"], record("progress", 2))
        self.assertTrue(self.remote_json("global.json")["healthy"])

    def test_noop_does_not_create_commit_or_erase_remote_diagnostics(self):
        self.update_remote({"diagnostics.txt": "newer failure\n"})
        before = self.git(self.origin, "rev-parse", "main").stdout
        self.write(self.runner, "diagnostics.txt", "older failure\n")
        result = self.publish(diagnostics="publish")
        self.assertIsNone(result.commit)
        self.assertEqual(self.git(self.origin, "rev-parse", "main").stdout, before)
        self.assertEqual(self.remote_text("diagnostics.txt"), "newer failure\n")

    def test_cli_reports_stale_full_publication_as_failure(self):
        self.update_remote({"global.json": {"healthy": True, "title": "remote"}})
        result = subprocess.run(
            [sys.executable, str(ROOT / "publish_data.py"), "--mode", "full", "--message", "test"],
            cwd=self.runner, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Rerun on latest main", result.stdout)


class WorkflowContractTests(unittest.TestCase):
    def test_writers_share_non_cancelling_group_and_latest_checkout(self):
        for name in ("netflix-global.yml", "netflix-metadata.yml"):
            text = (ROOT / ".github" / "workflows" / name).read_text()
            self.assertIn("group: netflix-data-writer\n  cancel-in-progress: false", text)
            self.assertIn("ref: main", text)
            self.assertNotIn("git pull --rebase", text)
            self.assertIn("publish_data.py --mode full", text)
            self.assertIn("publish_data.py --mode cache", text)
            self.assertIn('"verified_title_aliases.json"', text)
            self.assertIn("actions/upload-artifact@v4", text)

    def test_issue_and_final_failure_are_not_skipped_by_publication_failure(self):
        text = (ROOT / ".github/workflows/netflix-metadata.yml").read_text()
        for heading in ("Open or refresh metadata issue", "Fail when metadata refresh is unhealthy"):
            block = text.split(f"      - name: {heading}\n", 1)[1].split("      - name:", 1)[0]
            self.assertIn("always() && !cancelled()", block)
            self.assertIn("steps.publish.outcome != 'success'", block)
        recovered = text.split("      - name: Close recovered metadata issue\n", 1)[1].split("      - name:", 1)[0]
        self.assertIn("steps.publish.outcome == 'success'", recovered)


if __name__ == "__main__":
    unittest.main()
