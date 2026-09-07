#!/usr/bin/env python3
"""Synchronize a composed organization baseline and optional skills.

The organization repository owns composition of the generic baseline and its
organization overlay. Sources may be an existing local checkout, a temporary
fetch from a Git URL/ref, or one HTTPS document. Git mode materializes only the
configured document and skill blobs; it never checks out or executes repository
content. HTTPS mode accepts one document and no skills.

The default is a read-only drift check (0 current, 1 drift, 2 error). ``--write``
applies same-id changes. A new baseline id requires
``--accept-id <published-id>`` and updates the profile and document together;
without that acknowledgement the write exits 3.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterator
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import yaml


DEFAULT_STATE_FILE = ".org-baseline-sync-state.json"
MAX_BASELINE_BYTES = 1_048_576
MAX_SKILL_FILE_BYTES = 10 * 1_048_576
MAX_SKILL_FILES = 2_000
MAX_SKILL_TOTAL_BYTES = 100 * 1_048_576
MAX_GIT_LIST_BYTES = 2 * 1_048_576
FETCH_TIMEOUT_SECONDS = 60
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
MARKER_PATTERN = re.compile(
    r"(?m)^\s*`?baseline-id:\s*`?"
    r"([A-Za-z0-9](?:[A-Za-z0-9._+-]{0,78}[A-Za-z0-9])?)`?"
    r"(?:[ \t.,;:\u2014-].*)?$"
)
# A cheap, application-agnostic signal that a fetch landed on a login page, an
# error page, or a web UI wrapper instead of the baseline document it asked
# for \u2014 the case the marker check alone reports only as "found none", which
# reads as a defect in the document rather than in what was fetched.
_HTML_SNIFF_PATTERN = re.compile(r"^\s*<(!doctype\s+html|html\b)", re.IGNORECASE)


class SyncError(ValueError):
    """The sync cannot proceed or complete safely."""


@dataclass(frozen=True)
class SyncPlan:
    changes: tuple[str, ...]
    published_id: str
    configured_id: str | None
    profile_path: Path
    doc_source: Path
    doc_target: Path
    skill_sources: tuple[Path, ...]
    removed_skills: tuple[str, ...]
    state_path: Path
    write_state: bool
    unallowlisted_skills: tuple[str, ...]
    source_metadata: dict[str, str]


@dataclass(frozen=True)
class ResolvedSource:
    checkout: Path
    doc: str
    skills_dir: str | None
    metadata: dict[str, str]
    manage_skills: bool


def _marker(document: bytes, origin: str) -> str:
    if len(document) > MAX_BASELINE_BYTES:
        raise SyncError(
            f"baseline document exceeds {MAX_BASELINE_BYTES} bytes: {origin}"
        )
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SyncError(f"baseline document is not UTF-8: {origin}") from error
    ids = MARKER_PATTERN.findall(text)
    if len(ids) != 1:
        if not ids and _HTML_SNIFF_PATTERN.match(text[:512]):
            raise SyncError(
                f"document looks like an HTML page, not the baseline document "
                f"(likely a login page or redirect): {origin}"
            )
        detail = "none" if not ids else ", ".join(ids)
        raise SyncError(
            f"document must contain exactly one baseline-id marker; found {detail}: {origin}"
        )
    return ids[0]


def _valid_git_ref(value: str) -> bool:
    return bool(
        GIT_REF_PATTERN.fullmatch(value)
        and ".." not in value
        and "//" not in value
        and "/." not in value
        and not value.endswith(("/", ".", ".lock"))
    )


def _validate_git_url(value: str) -> str:
    if len(value) > 2048 or any(
        character.isspace() or not character.isprintable()
        for character in value
    ):
        raise SyncError("Git URL contains whitespace, control characters, or is too long")
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise SyncError(
                "Git HTTPS URL must have a host and no credentials, query, or fragment"
            )
        return value
    if value.startswith("ssh://"):
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.password is not None or parsed.query or parsed.fragment:
            raise SyncError(
                "Git SSH URL must have a host and no password, query, or fragment"
            )
        return value
    if re.fullmatch(
        r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[A-Za-z0-9._~/-]+", value
    ):
        return value
    raise SyncError(
        "Git URL must use HTTPS without credentials or an SSH Git URL"
    )


def _validate_https_url(value: str) -> str:
    if len(value) > 2048 or any(
        character.isspace() or not character.isprintable()
        for character in value
    ):
        raise SyncError("HTTPS baseline URL contains whitespace, control characters, or is too long")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise SyncError(f"invalid HTTPS baseline URL: {error}") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SyncError(
            "baseline URL must use HTTPS and contain no credentials, query, or fragment"
        )
    return value


class _HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: N802
        try:
            _validate_https_url(newurl)
        except SyncError as error:
            raise urllib.error.HTTPError(
                newurl, code, f"redirect rejected: {error}", headers, fp
            ) from error
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def _fetch_https_document(url: str) -> bytes:
    url = _validate_https_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/markdown, text/plain",
            "User-Agent": "appsec-advisor-org-baseline-sync/1",
        },
    )
    opener = urllib.request.build_opener(_HTTPSOnlyRedirectHandler())
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:  # noqa: S310
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_BASELINE_BYTES:
                raise SyncError(
                    f"baseline document exceeds {MAX_BASELINE_BYTES} bytes: {url}"
                )
            document = response.read(MAX_BASELINE_BYTES + 1)
    except SyncError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise SyncError(f"cannot fetch HTTPS baseline {url}: {error}") from error
    _marker(document, url)
    return document


def _git_command(git_dir: Path, arguments: list[str], *, limit: int = 64 * 1024) -> bytes:
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.file.allow=never",
        "--git-dir",
        str(git_dir),
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyncError(f"Git command could not complete: {error}") from error
    if len(result.stdout) > limit:
        raise SyncError(f"Git command output exceeds {limit} bytes")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise SyncError(f"Git command failed: {detail or 'no diagnostic'}")
    return result.stdout


def _init_bare_repository(git_dir: Path) -> None:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "init.templateDir=",
                "-c",
                "core.hooksPath=/dev/null",
                "init",
                "--bare",
                "--quiet",
                str(git_dir),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=FETCH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SyncError(f"could not initialize temporary Git repository: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise SyncError(f"could not initialize temporary Git repository: {detail}")


def _git_tree_entries(git_dir: Path, commit: str, path: str) -> list[tuple[str, str, str, str]]:
    output = _git_command(
        git_dir,
        ["ls-tree", "-rz", "-r", "--full-tree", commit, "--", path],
        limit=MAX_GIT_LIST_BYTES,
    )
    entries: list[tuple[str, str, str, str]] = []
    for raw_entry in output.rstrip(b"\0").split(b"\0") if output else []:
        try:
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
            entry_path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise SyncError("Git source contains an invalid tree entry") from error
        entries.append((mode, object_type, object_id, entry_path))
    return entries


def _git_blob(git_dir: Path, object_id: str, maximum: int) -> bytes:
    raw_size = _git_command(git_dir, ["cat-file", "-s", object_id], limit=64)
    try:
        size = int(raw_size.strip())
    except ValueError as error:
        raise SyncError("Git returned an invalid blob size") from error
    if size > maximum:
        raise SyncError(f"Git source file exceeds {maximum} bytes")
    return _git_command(git_dir, ["cat-file", "blob", object_id], limit=maximum)


def _materialize_git_source(
    root: Path, url: str, ref: str, doc: str, skills_dir: str | None, *, skills_optional: bool = False
) -> ResolvedSource:
    url = _validate_git_url(url)
    if not _valid_git_ref(ref):
        raise SyncError(f"Git ref is not a safe branch, tag, or commit: {ref!r}")
    doc_relative = _relative_path(doc, "baseline document")
    if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/" for character in doc):
        raise SyncError("Git baseline document path contains unsupported characters")
    skills_relative = None
    if skills_dir:
        skills_relative = _relative_path(skills_dir, "skills directory")
        if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/" for character in skills_dir):
            raise SyncError("Git skills path contains unsupported characters")

    git_dir = root / "source.git"
    source_root = root / "materialized"
    source_root.mkdir()
    _init_bare_repository(git_dir)
    _git_command(
        git_dir,
        ["fetch", "--quiet", "--depth", "1", "--no-tags", "--", url, ref],
    )
    commit = _git_command(
        git_dir, ["rev-parse", "--verify", "FETCH_HEAD^{commit}"], limit=128
    ).decode("ascii").strip()

    doc_entries = _git_tree_entries(git_dir, commit, doc_relative.as_posix())
    if len(doc_entries) != 1 or doc_entries[0][3] != doc_relative.as_posix():
        raise SyncError(f"baseline document not found as one Git blob: {doc}")
    mode, object_type, object_id, _ = doc_entries[0]
    if object_type != "blob" or mode not in ("100644", "100755"):
        raise SyncError(f"baseline document must be a regular Git file: {doc}")
    document = _git_blob(git_dir, object_id, MAX_BASELINE_BYTES)
    _marker(document, f"{url}@{commit}:{doc}")
    _atomic_write(source_root / doc_relative, document)

    if skills_relative is not None:
        entries = _git_tree_entries(git_dir, commit, skills_relative.as_posix())
        if len(entries) > MAX_SKILL_FILES:
            raise SyncError(f"org skill tree exceeds {MAX_SKILL_FILES} files")
        prefix = skills_relative.as_posix() + "/"
        if not entries:
            if skills_optional:
                skills_relative = None
            else:
                raise SyncError(f"skills directory not found in Git source: {skills_dir}")
        else:
            total_size = 0
            for mode, object_type, object_id, entry_path in entries:
                if not entry_path.startswith(prefix):
                    raise SyncError("Git skills path escaped its configured directory")
                relative = _relative_path(entry_path, "Git skill file")
                if object_type != "blob" or mode not in ("100644", "100755"):
                    raise SyncError(f"Git skills may contain only regular files: {entry_path}")
                data = _git_blob(git_dir, object_id, MAX_SKILL_FILE_BYTES)
                total_size += len(data)
                if total_size > MAX_SKILL_TOTAL_BYTES:
                    raise SyncError(
                        f"org skill tree exceeds {MAX_SKILL_TOTAL_BYTES} total bytes"
                    )
                _atomic_write(
                    source_root / relative, data, 0o755 if mode == "100755" else 0o644
                )

    return ResolvedSource(
        checkout=source_root,
        doc=doc_relative.as_posix(),
        skills_dir=skills_relative.as_posix() if skills_relative else None,
        metadata={"kind": "git", "url": url, "ref": ref, "commit": commit},
        manage_skills=True,
    )


@contextmanager
def _resolved_source(args: argparse.Namespace) -> Iterator[ResolvedSource]:
    if args.checkout is not None:
        if not args.doc:
            raise SyncError("--doc is required with --checkout")
        yield ResolvedSource(
            checkout=Path(args.checkout),
            doc=args.doc,
            skills_dir=args.skills_dir,
            metadata={"kind": "local"},
            manage_skills=bool(args.manage_skills or args.skills_dir),
        )
        return
    with tempfile.TemporaryDirectory(prefix="appsec-org-baseline-") as temporary:
        root = Path(temporary)
        if args.git_url is not None:
            if not args.doc:
                raise SyncError("--doc is required with --git-url")
            yield _materialize_git_source(
                root, args.git_url, args.git_ref, args.doc, args.skills_dir,
                skills_optional=args.skills_dir_optional,
            )
            return
        if args.https_url is not None:
            if args.doc or args.skills_dir:
                raise SyncError("HTTPS mode accepts exactly one document and no --doc or --skills-dir")
            document = _fetch_https_document(args.https_url)
            source_root = root / "materialized"
            _atomic_write(source_root / "baseline.md", document)
            yield ResolvedSource(
                checkout=source_root,
                doc="baseline.md",
                skills_dir=None,
                metadata={
                    "kind": "https",
                    "url": _validate_https_url(args.https_url),
                    "sha256": hashlib.sha256(document).hexdigest(),
                },
                manage_skills=True,
            )
            return
    raise SyncError("one baseline source is required")


def _read_yaml_mapping(path: Path, description: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SyncError(f"cannot read {description} {path}: {error}") from error
    except (UnicodeError, yaml.YAMLError) as error:
        raise SyncError(f"{description} is not valid UTF-8 YAML: {path}: {error}") from error
    if not isinstance(value, dict):
        raise SyncError(f"{description} must be a YAML mapping: {path}")
    return value


def _configured_baseline(org_profile_dir: Path) -> tuple[str | None, str, Path]:
    profile_path = org_profile_dir / "org-profile.yaml"
    data = _read_yaml_mapping(profile_path, "organization profile")
    baseline: Any = data.get("baseline")
    baseline = baseline if isinstance(baseline, dict) else {}
    if baseline.get("enabled", True) is not True:
        raise SyncError("organization baseline sync requires baseline.enabled: true")
    if "url" in baseline or "git" in baseline:
        raise SyncError(
            "organization baseline mode must use the reviewed baseline.file only; remove baseline.url and baseline.git"
        )
    configured_id = baseline.get("id") if isinstance(baseline.get("id"), str) else None
    baseline_file = baseline.get("file")
    if not isinstance(baseline_file, str) or not baseline_file.strip():
        raise SyncError("organization baseline mode requires a non-empty baseline.file")
    return configured_id, baseline_file, profile_path


def _relative_path(value: str, description: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise SyncError(f"{description} must be a non-empty relative path without '.' or '..': {value!r}")
    return path


def _reject_symlink_components(root: Path, relative: Path, description: str) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise SyncError(f"{description} must not contain symlinks: {current}")


def _source_path(root: Path, value: str, description: str, *, directory: bool) -> Path:
    relative = _relative_path(value, description)
    _reject_symlink_components(root, relative, description)
    candidate = root / relative
    if directory and not candidate.is_dir():
        raise SyncError(f"{description} not found: {candidate}")
    if not directory and not candidate.is_file():
        raise SyncError(f"{description} not found: {candidate}")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise SyncError(f"{description} escapes its source repository: {candidate}") from error
    return candidate


def _target_path(root: Path, value: str, description: str) -> Path:
    relative = _relative_path(value, description)
    candidate = root / relative
    _reject_symlink_components(root, relative, description)
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise SyncError(f"{description} escapes {root}: {candidate}") from error
    return candidate


def _validate_skill_dir(skill_dir: Path) -> None:
    name = skill_dir.name
    if not SKILL_NAME_PATTERN.fullmatch(name):
        raise SyncError(
            "org skill name must start with a lowercase letter or digit and contain only "
            f"lowercase letters, digits, '.', '_' and '-': {name}"
        )
    if not (skill_dir / "SKILL.md").is_file():
        raise SyncError(f"org skill {skill_dir} must contain SKILL.md")


def _tree_digest(root: Path) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    file_count = 0
    total_size = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SyncError(f"org skill content must not contain symlinks: {path}")
        if path.is_dir():
            entries.append((f"d:{relative}", ""))
            continue
        if not path.is_file():
            raise SyncError(f"org skill content must contain only files and directories: {path}")
        file_count += 1
        if file_count > MAX_SKILL_FILES:
            raise SyncError(f"org skill tree exceeds {MAX_SKILL_FILES} files: {root}")
        size = path.stat().st_size
        if size > MAX_SKILL_FILE_BYTES:
            raise SyncError(
                f"org skill file exceeds {MAX_SKILL_FILE_BYTES} bytes: {path}"
            )
        total_size += size
        if total_size > MAX_SKILL_TOTAL_BYTES:
            raise SyncError(
                f"org skill tree exceeds {MAX_SKILL_TOTAL_BYTES} total bytes: {root}"
            )
        entries.append((f"f:{relative}", hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(entries)


def _read_state(path: Path) -> set[str]:
    if not path.exists():
        return set()
    if path.is_symlink():
        raise SyncError(f"sync state must not be a symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SyncError(f"cannot read organization baseline sync state {path}: {error}") from error
    skills = value.get("managed_skills") if isinstance(value, dict) else None
    if not isinstance(skills, list) or any(
        not isinstance(item, str) or not SKILL_NAME_PATTERN.fullmatch(item)
        for item in skills
    ):
        raise SyncError(f"organization baseline sync state is invalid: {path}")
    return set(skills)


def _unallowlisted(org_profile_dir: Path, skill_names: set[str]) -> tuple[str, ...]:
    if not skill_names:
        return ()
    policy_path = org_profile_dir / "package-policy.yaml"
    if not policy_path.is_file():
        return tuple(sorted(skill_names))
    policy = _read_yaml_mapping(policy_path, "package policy")
    surface = policy.get("plugin_surface")
    skills = surface.get("skills") if isinstance(surface, dict) else None
    include = skills.get("include") if isinstance(skills, dict) else None
    allowed = {item for item in include if isinstance(item, str)} if isinstance(include, list) else set()
    return tuple(sorted(skill_names - allowed))


def plan(args: argparse.Namespace) -> SyncPlan:
    source_root = Path(args.checkout)
    if source_root.is_symlink():
        raise SyncError(f"organization baseline source must not be a symlink: {source_root}")
    if not source_root.is_dir():
        raise SyncError(f"organization baseline source not found: {source_root}")
    source_root = source_root.resolve(strict=True)

    doc_source = _source_path(
        source_root, args.doc, "baseline document", directory=False
    )
    doc_bytes = doc_source.read_bytes()
    published_id = _marker(doc_bytes, str(doc_source))

    org_profile_dir = Path(args.org_profile)
    if org_profile_dir.is_symlink() or not org_profile_dir.is_dir():
        raise SyncError(f"organization profile directory not found or symlinked: {org_profile_dir}")
    org_profile_dir = org_profile_dir.resolve(strict=True)
    configured_id, baseline_file, profile_path = _configured_baseline(org_profile_dir)
    doc_target = _target_path(org_profile_dir, baseline_file, "baseline.file")
    repository_root = org_profile_dir.parent
    if source_root == repository_root or source_root in repository_root.parents or repository_root in source_root.parents:
        raise SyncError(
            "organization baseline source and packaging repository must be separate, non-overlapping trees"
        )

    org_skills_dir = Path(args.org_skills)
    if org_skills_dir.is_symlink():
        raise SyncError(f"organization skills directory must not be a symlink: {org_skills_dir}")
    if org_skills_dir.exists() and not org_skills_dir.is_dir():
        raise SyncError(f"organization skills path is not a directory: {org_skills_dir}")
    org_skills_dir = org_skills_dir.resolve(strict=False)
    try:
        org_skills_dir.relative_to(repository_root)
    except ValueError as error:
        raise SyncError(
            f"organization skills directory escapes the packaging repository: {org_skills_dir}"
        ) from error

    changes: list[str] = []
    if configured_id != published_id:
        changes.append(
            f"id: org-profile.yaml has '{configured_id}', source publishes '{published_id}'"
        )
    if not doc_target.is_file():
        changes.append(f"new file: {doc_target} does not exist yet")
    elif doc_target.read_bytes() != doc_bytes:
        changes.append(f"text: {doc_target} differs from the source")

    state_path = org_profile_dir.parent / DEFAULT_STATE_FILE
    skill_sources: list[Path] = []
    removed_skills: tuple[str, ...] = ()
    manage_skills = bool(getattr(args, "manage_skills", False) or args.skills_dir)
    source_metadata = dict(getattr(args, "source_metadata", {}))
    write_state = manage_skills or bool(source_metadata)
    current_names: set[str] = set()
    if manage_skills:
        if args.skills_dir:
            skills_src = _source_path(
                source_root, args.skills_dir, "skills directory", directory=True
            )
            for item in sorted(skills_src.iterdir()):
                if item.is_symlink():
                    raise SyncError(f"org skill directory must not be a symlink: {item}")
                if not item.is_dir():
                    raise SyncError(
                        f"skills directory may contain only skill directories: {item}"
                    )
                _validate_skill_dir(item)
                source_digest = _tree_digest(item)
                skill_sources.append(item)
                current_names.add(item.name)
                destination = org_skills_dir / item.name
                if destination.is_symlink():
                    raise SyncError(f"refusing to replace symlinked org skill: {destination}")
                if not destination.is_dir():
                    changes.append(f"new skill: {item.name}")
                elif _tree_digest(destination) != source_digest:
                    changes.append(f"changed skill: {item.name}")
        previous_names = _read_state(state_path)
        removed_skills = tuple(sorted(previous_names - current_names))
        for name in removed_skills:
            destination = org_skills_dir / name
            if destination.is_symlink():
                raise SyncError(f"refusing to remove symlinked org skill: {destination}")
            if destination.exists() and not destination.is_dir():
                raise SyncError(f"managed org skill path is not a directory: {destination}")
            changes.append(f"removed skill: {name}")
        if previous_names != current_names:
            changes.append("skill inventory: managed skill set changed")

    return SyncPlan(
        changes=tuple(dict.fromkeys(changes)),
        published_id=published_id,
        configured_id=configured_id,
        profile_path=profile_path,
        doc_source=doc_source,
        doc_target=doc_target,
        skill_sources=tuple(skill_sources),
        removed_skills=removed_skills,
        state_path=state_path,
        write_state=write_state,
        unallowlisted_skills=_unallowlisted(org_profile_dir, current_names),
        source_metadata=source_metadata,
    )


def _updated_profile(profile_path: Path, published_id: str) -> str:
    try:
        text = profile_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SyncError(f"cannot read organization profile {profile_path}: {error}") from error
    block = re.search(r"(?m)^baseline:\s*(?:#.*)?\n(?P<body>(?:^[ \t].*(?:\n|$))*)", text)
    if not block:
        raise SyncError("organization profile has no top-level baseline block")
    body = block.group("body")
    updated_body, count = re.subn(
        r"(?m)^  id:\s*[^#\n]*(?:#.*)?$", f"  id: {published_id}", body
    )
    if count != 1:
        raise SyncError("organization profile baseline block must contain exactly one id field")
    return text[: block.start("body")] + updated_body + text[block.end("body") :]


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SyncError(f"refusing to replace symlinked file: {path}")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def apply(plan_: SyncPlan, org_skills_dir: Path, *, update_id: bool) -> None:
    document_existed = plan_.doc_target.exists()
    previous_document = plan_.doc_target.read_bytes() if document_existed else None
    previous_profile = plan_.profile_path.read_bytes()
    updated_profile = (
        _updated_profile(plan_.profile_path, plan_.published_id).encode("utf-8")
        if update_id
        else None
    )
    try:
        _atomic_write(plan_.doc_target, plan_.doc_source.read_bytes())
        if updated_profile is not None:
            _atomic_write(plan_.profile_path, updated_profile)
    except (OSError, SyncError):
        if document_existed and previous_document is not None:
            _atomic_write(plan_.doc_target, previous_document)
        elif plan_.doc_target.exists() and not plan_.doc_target.is_symlink():
            plan_.doc_target.unlink()
        if plan_.profile_path.read_bytes() != previous_profile:
            _atomic_write(plan_.profile_path, previous_profile)
        raise

    org_skills_dir.mkdir(parents=True, exist_ok=True)
    for name in plan_.removed_skills:
        destination = org_skills_dir / name
        if destination.exists():
            shutil.rmtree(destination)
    for skill_source in plan_.skill_sources:
        destination = org_skills_dir / skill_source.name
        staged = Path(tempfile.mkdtemp(prefix=f".{skill_source.name}.", dir=org_skills_dir))
        shutil.rmtree(staged)
        backup: Path | None = None
        try:
            shutil.copytree(skill_source, staged, symlinks=True)
            if destination.exists():
                backup = Path(
                    tempfile.mkdtemp(prefix=f".{skill_source.name}.backup.", dir=org_skills_dir)
                )
                shutil.rmtree(backup)
                os.replace(destination, backup)
            os.replace(staged, destination)
            if backup is not None:
                shutil.rmtree(backup)
                backup = None
        except OSError:
            if backup is not None and backup.exists() and not destination.exists():
                os.replace(backup, destination)
                backup = None
            raise
        finally:
            if staged.exists():
                shutil.rmtree(staged)
            if backup is not None and backup.exists():
                shutil.rmtree(backup)

    if plan_.write_state:
        state = {
            "version": 1,
            "managed_skills": sorted(path.name for path in plan_.skill_sources),
        }
        if plan_.source_metadata:
            state["source"] = plan_.source_metadata
        _atomic_write(
            plan_.state_path,
            (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--org-profile", type=Path, default=Path("org-profile"), help="org-profile directory"
    )
    parser.add_argument(
        "--org-skills", type=Path, default=Path("org-skills"), help="org-skills directory"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--checkout", type=Path, help="existing local organization-baseline checkout"
    )
    source.add_argument(
        "--git-url", help="organization-baseline repository HTTPS or SSH Git URL"
    )
    source.add_argument(
        "--https-url", help="one composed organization-baseline HTTPS document"
    )
    parser.add_argument(
        "--git-ref", default="main", help="branch, tag, or commit fetched with --git-url"
    )
    parser.add_argument(
        "--doc", help="composed baseline document relative to a checkout or Git source"
    )
    parser.add_argument(
        "--skills-dir", help="optional skill-pack directory relative to --checkout"
    )
    parser.add_argument(
        "--skills-dir-optional",
        action="store_true",
        help="with --git-url, treat --skills-dir missing from the Git source as no skill pack instead of an error",
    )
    parser.add_argument(
        "--manage-skills",
        action="store_true",
        help="treat an omitted skills directory as an empty managed skill set",
    )
    parser.add_argument(
        "--accept-id", help="accept and persist this new baseline id during --write"
    )
    parser.add_argument(
        "--print-id",
        action="store_true",
        help="validate source document and skills, print the source id, and exit",
    )
    parser.add_argument("--write", action="store_true", help="apply pending changes")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.skills_dir_optional and not args.git_url:
        print("ERROR: --skills-dir-optional is only supported with --git-url", file=sys.stderr)
        return 2
    if args.skills_dir_optional and not args.skills_dir:
        print("ERROR: --skills-dir-optional requires --skills-dir", file=sys.stderr)
        return 2
    try:
        with _resolved_source(args) as resolved:
            source_root = Path(resolved.checkout)
            if source_root.is_symlink() or not source_root.is_dir():
                raise SyncError(f"organization baseline source not found or symlinked: {source_root}")
            source_root = source_root.resolve(strict=True)
            doc_source = _source_path(
                source_root, resolved.doc, "baseline document", directory=False
            )
            published_id = _marker(doc_source.read_bytes(), str(doc_source))
            if resolved.skills_dir:
                skills_root = _source_path(
                    source_root, resolved.skills_dir, "skills directory", directory=True
                )
                for item in sorted(skills_root.iterdir()):
                    if item.is_symlink() or not item.is_dir():
                        raise SyncError(f"skills directory contains an invalid entry: {item}")
                    _validate_skill_dir(item)
                    _tree_digest(item)
            if args.print_id:
                print(published_id)
                if args.skills_dir_optional:
                    print("skills: found" if resolved.skills_dir else "skills: missing")
                return 0

            resolved_args = argparse.Namespace(**vars(args))
            resolved_args.checkout = source_root
            resolved_args.doc = resolved.doc
            resolved_args.skills_dir = resolved.skills_dir
            resolved_args.manage_skills = resolved.manage_skills
            resolved_args.source_metadata = resolved.metadata
            plan_ = plan(resolved_args)

            for change in plan_.changes:
                print(change)
            for name in plan_.unallowlisted_skills:
                print(
                    f"NOTE: org skill '{name}' is synced but not allowlisted in "
                    "org-profile/package-policy.yaml; it will not ship until explicitly included."
                )

            if not plan_.changes:
                print("OK: organization baseline and skills are current")
                return 0
            if not args.write:
                print("NOTE: re-run with --write to apply the pending changes.")
                return 1

            update_id = plan_.configured_id != plan_.published_id
            if update_id and not args.accept_id:
                print(
                    "NOTE: a new baseline id is a decision; re-run with "
                    f"--accept-id {plan_.published_id} to update document and profile together."
                )
                return 3
            if args.accept_id and args.accept_id != plan_.published_id:
                print(
                    f"ERROR: --accept-id is '{args.accept_id}', but the source publishes "
                    f"'{plan_.published_id}'",
                    file=sys.stderr,
                )
                return 2

            try:
                apply(
                    plan_, Path(args.org_skills).resolve(strict=False), update_id=update_id
                )
            except (OSError, SyncError) as error:
                print(f"ERROR: cannot apply organization baseline sync: {error}", file=sys.stderr)
                return 2

            print(f"==> Wrote {plan_.doc_target}")
            if update_id:
                print(f"==> Updated baseline.id to {plan_.published_id}")
            for skill_source in plan_.skill_sources:
                print(f"==> Wrote {Path(args.org_skills) / skill_source.name}")
            for name in plan_.removed_skills:
                print(f"==> Removed {Path(args.org_skills) / name}")
            return 0
    except (OSError, SyncError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
