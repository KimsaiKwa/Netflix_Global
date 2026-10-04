#!/usr/bin/env python3
"""Publish validated output without rebasing a dirty generator checkout.

Both Actions writers use this helper after acquiring the same concurrency group.
A separate, clean worktree starts at the latest remote branch for each attempt.
Only selected outputs are copied; cache records are merged against the checkout
base, so unrelated remote commits and concurrent cache progress survive. If a
remote writer changed the mirror or its generation inputs, keep its mirror and
publish only cache progress. The caller must then rerun against the new inputs.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CACHE = "metadata_cache.json"
DIAGNOSTICS = "diagnostics.txt"
# Publishing a result produced by old matching rules can reintroduce bad matches.
GENERATION_INPUTS = (
    "update.py", "update_countries.py", "enrich_metadata.py", "health_check.py",
    "verified_title_aliases.json", "publish_data.py", CACHE,
)
MISSING = object()


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result


def blob(repo: Path, revision: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{path}"], capture_output=True,
    )
    if result.returncode:
        return None
    return result.stdout


def read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def mirror_paths(repo: Path, revision: str) -> set[str]:
    paths = git(repo, "ls-tree", "-r", "--name-only", revision).stdout.splitlines()
    return {path for path in paths if path == "global.json" or (
        path.startswith("countries/") and path.endswith(".json")
    )}


def decode_cache(raw: bytes | None) -> dict:
    if raw is None:
        return {"version": 1, "updated_at": None, "items": {}}
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("items"), dict):
        raise ValueError("metadata_cache.json must contain an items object")
    return value


def record_freshness(record: object) -> tuple[float, float]:
    """Newer strict-match versions win first, then the lookup timestamp."""
    if not isinstance(record, dict):
        return (0, 0)

    def number(value: object) -> float:
        try:
            result = float(value)
            return result if math.isfinite(result) else 0
        except (TypeError, ValueError):
            return 0

    return number(record.get("match_version")), number(record.get("saved_at"))


def iso_timestamp(value: object) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def current_identity_revisions(repo: Path) -> dict[str, str]:
    """Ask the current remote matching code for its reviewed-identity revisions.

    These functions only read the local manifest; no provider lookup is made.
    Keeping normalization and hashing in one place prevents cache-key drift.
    """
    if not (repo / "verified_title_aliases.json").exists():
        return {}
    result = subprocess.run(
        [sys.executable, "-c", (
            "import json, enrich_metadata as e; "
            "print(json.dumps({key: e.identity_revision(item['netflix_title'], "
            "item['media_type']) for key, item in e.verified_identities().items()}))"
        )], cwd=repo, capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError("Cannot validate current cache identities: " + result.stderr.strip())
    return json.loads(result.stdout)


def merge_cache(
    base: dict, local: dict, remote: dict,
    expected_revisions: dict[str, str] | None = None,
) -> dict:
    """Apply only local record changes; preserve remote deletions and fresher data.

    Cache entries are indivisible: merging fields from two candidates could mix
    different films. Ties and explicit remote deletions favor the remote writer.
    """
    versions = [value.get("version", 1) for value in (base, local, remote)]
    # The existing v1 -> v2 migration keeps the same items/record shape and
    # invalidates old matches per record. Merge progress across this migration,
    # but never roll a remote v2 cache back or guess at a future schema.
    if not all(type(version) is int and version in (1, 2) for version in versions):
        raise ValueError("Unsupported cache schema changed during this run; rerun on latest main")
    result = copy.deepcopy(remote)
    result["version"] = max(versions)
    merged = result["items"]
    for key in base["items"].keys() | local["items"].keys():
        before = base["items"].get(key, MISSING)
        ours = local["items"].get(key, MISSING)
        theirs = remote["items"].get(key, MISSING)
        if ours == before:
            continue
        expected = expected_revisions.get(key, "") if expected_revisions is not None else None
        if expected is not None and ours is not MISSING:
            if not isinstance(ours, dict) or (ours.get("identity_revision") or "") != expected:
                # A lookup from a superseded alias manifest is never progress.
                continue
        if theirs == before:
            if ours is MISSING:
                merged.pop(key, None)
            else:
                merged[key] = copy.deepcopy(ours)
        elif ours is not MISSING and theirs is not MISSING:
            remote_identity_old = expected is not None and (
                not isinstance(theirs, dict) or (theirs.get("identity_revision") or "") != expected
            )
            if remote_identity_old or record_freshness(ours) > record_freshness(theirs):
                merged[key] = copy.deepcopy(ours)
    if merged != remote["items"]:
        result["updated_at"] = max(
            (remote.get("updated_at"), local.get("updated_at")), key=iso_timestamp,
        )
    return result


def put(repo: Path, path: str, value: bytes | None) -> None:
    target = repo / path
    if value is None:
        target.unlink(missing_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)


@dataclass
class Publication:
    commit: str | None
    cache_only_reason: str | None = None


def publish(
    repo: Path, *, mode: str, message: str, diagnostics: str = "keep",
    remote: str = "origin", branch: str = "main", max_attempts: int = 3,
) -> Publication:
    repo = repo.resolve()
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    base_paths = mirror_paths(repo, base)
    local_paths = {"global.json"} | {
        str(path.relative_to(repo)) for path in (repo / "countries").glob("*.json")
    }
    paths = base_paths | local_paths
    snapshots = {path: read(repo / path) for path in paths} if mode == "full" else {}
    base_cache = decode_cache(blob(repo, base, CACHE))
    local_cache_raw = read(repo / CACHE)
    # If enrichment never ran, there is no cache progress to publish.
    local_cache = decode_cache(local_cache_raw) if local_cache_raw is not None else base_cache
    local_diagnostics = read(repo / DIAGNOSTICS)
    remote_ref = f"refs/remotes/{remote}/{branch}"

    for attempt in range(1, max_attempts + 1):
        git(repo, "fetch", "--no-tags", remote, f"+refs/heads/{branch}:{remote_ref}")
        latest = git(repo, "rev-parse", remote_ref).stdout.strip()
        reason = None
        if mode == "full":
            guarded = paths | mirror_paths(repo, latest) | set(GENERATION_INPUTS)
            changed = [path for path in sorted(guarded)
                       if blob(repo, base, path) != blob(repo, latest, path)]
            if changed:
                reason = "Remote mirror or generation inputs changed: " + ", ".join(changed)

        remote_cache_raw = blob(repo, latest, CACHE)
        remote_cache = decode_cache(remote_cache_raw)
        with tempfile.TemporaryDirectory(prefix="netflix-publish-") as temp:
            worktree = Path(temp) / "checkout"
            git(repo, "worktree", "add", "--detach", str(worktree), latest)
            try:
                merged_cache = merge_cache(
                    base_cache, local_cache, remote_cache,
                    current_identity_revisions(worktree),
                )
                staged = set()
                if mode == "full" and reason is None:
                    for path, value in snapshots.items():
                        put(worktree, path, value)
                    validation = subprocess.run(
                        [sys.executable, "health_check.py"], cwd=worktree,
                        capture_output=True, text=True,
                    )
                    print(validation.stdout, end="", flush=True)
                    print(validation.stderr, end="", file=sys.stderr, flush=True)
                    if validation.returncode:
                        reason = "Final publication health check failed; browser JSON was not published"
                        for path in snapshots:
                            put(worktree, path, blob(repo, latest, path))
                    else:
                        staged.update(snapshots)
                if merged_cache != remote_cache:
                    put(worktree, CACHE, (json.dumps(
                        merged_cache, ensure_ascii=False, indent=2,
                    ) + "\n").encode("utf-8"))
                    staged.add(CACHE)
                # Do not replace a diagnostic from a newer remote run.
                if blob(repo, base, DIAGNOSTICS) == blob(repo, latest, DIAGNOSTICS):
                    if diagnostics == "publish" and local_diagnostics is not None:
                        put(worktree, DIAGNOSTICS, local_diagnostics)
                        staged.add(DIAGNOSTICS)
                    elif diagnostics == "remove" and reason is None:
                        if (worktree / DIAGNOSTICS).exists():
                            put(worktree, DIAGNOSTICS, None)
                            staged.add(DIAGNOSTICS)
                if reason:
                    print(reason + "; preserving cache progress only. Rerun on latest main.", flush=True)
                if staged:
                    # Missing, untracked paths cannot be passed to git add.
                    stageable = [path for path in sorted(staged) if
                                 (worktree / path).exists() or blob(repo, latest, path) is not None]
                    if stageable:
                        git(worktree, "add", "-A", "--", *stageable)
                if git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
                    print("No publishable changes.", flush=True)
                    return Publication(None, reason)
                commit_message = message if reason is None else "Preserve Netflix metadata cache progress [skip ci]"
                git(worktree, "commit", "-m", commit_message)
                commit = git(worktree, "rev-parse", "HEAD").stdout.strip()
                pushed = git(worktree, "push", remote, f"HEAD:refs/heads/{branch}", check=False)
                if pushed.returncode == 0:
                    print(f"Published {commit} on {branch}.", flush=True)
                    return Publication(commit, reason)
                # Retry only a genuine race, never force-push or rebase dirty files.
                git(repo, "fetch", "--no-tags", remote, f"+refs/heads/{branch}:{remote_ref}")
                if git(repo, "merge-base", "--is-ancestor", commit, remote_ref, check=False).returncode == 0:
                    print(f"Verified publication {commit} on {branch}.", flush=True)
                    return Publication(commit, reason)
                if git(repo, "rev-parse", remote_ref).stdout.strip() == latest:
                    raise RuntimeError(f"Publication push failed:\n{pushed.stderr.strip()}")
                print(f"Remote advanced during publication; retrying ({attempt}/{max_attempts}).", flush=True)
            finally:
                git(repo, "worktree", "remove", "--force", str(worktree))
    raise RuntimeError(f"Remote kept advancing; publication failed after {max_attempts} attempts")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "cache"), required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--diagnostics", choices=("keep", "publish", "remove"), default="keep")
    args = parser.parse_args()
    try:
        result = publish(Path.cwd(), mode=args.mode, message=args.message, diagnostics=args.diagnostics)
        return 1 if result.cache_only_reason else 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Publication failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
