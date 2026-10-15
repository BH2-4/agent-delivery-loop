"""Install and update the managed local agent-pull adapter.

The updater only accepts source/ref values saved by the one-time installer. It
does not start an agent, change a host's model configuration, or read model
credentials. A release is built in a separate virtual environment and becomes
active when ``current.json`` is atomically replaced.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import urlsplit

from .errors import AgentDeliveryError

CONFIG_NAME = "config.json"
CURRENT_NAME = "current.json"
CONFIG_SCHEMA = 1
CURRENT_SCHEMA = 1
DEFAULT_SOURCE = "https://github.com/BH2-4/agent-delivery-loop.git"
DEFAULT_REF = "main"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
SKILL_RESOURCE = Path("assets/skills/agent-pull/SKILL.md")
SKILL_TEMPLATE_TOKEN = "{{INSTALL_ROOT}}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_root(value: Path) -> Path:
    root = value.expanduser().resolve()
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise AgentDeliveryError("Choose a dedicated managed install directory, not a filesystem or home root.")
    return root


def _canonical_skill_dir(value: Path) -> Path:
    skill_dir = value.expanduser().absolute()
    if skill_dir.is_symlink():
        raise AgentDeliveryError("The selected Skill directory is a symlink; choose its actual managed directory.")
    return skill_dir


def _validate_source(source: str) -> str:
    parsed = urlsplit(source)
    if (parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise AgentDeliveryError("The update source must be a credential-free HTTPS GitHub repository URL.")
    path = parsed.path.removesuffix("/")
    if not re.fullmatch(r"/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+(?:\.git)?", path):
        raise AgentDeliveryError("The update source must identify one GitHub OWNER/REPO repository.")
    return f"https://github.com{path.removesuffix('.git')}.git"


def _run(args: Sequence[str], *, cwd: Path | None = None, input_data: bytes | None = None,
         timeout: int = 300) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            list(args), cwd=cwd, input=input_data, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentDeliveryError(f"Could not run the local update command ({Path(args[0]).name}).") from exc
    if result.returncode:
        raise AgentDeliveryError(f"The local update command failed ({Path(args[0]).name}, exit {result.returncode}).")
    return result


@contextlib.contextmanager
def _update_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".update.lock"
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            if lock_file.read(1) == b"":
                lock_file.seek(0)
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                                         delete=False) as file:
            temp_path = Path(file.name)
            json.dump(value, file, ensure_ascii=False, indent=2, sort_keys=True)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentDeliveryError(f"The managed {label} is missing or unreadable: {path}.") from exc
    if not isinstance(value, dict):
        raise AgentDeliveryError(f"The managed {label} is invalid: expected a JSON object.")
    return value


def _config_for_root(root: Path) -> dict[str, Any]:
    config = _read_json(root / CONFIG_NAME, "update configuration")
    if config.get("schema_version") != CONFIG_SCHEMA:
        raise AgentDeliveryError("The managed update configuration has an unsupported schema.")
    if config.get("install_root") != str(root):
        raise AgentDeliveryError("The managed update configuration does not match this install directory.")
    source = config.get("source")
    ref = config.get("ref")
    skill_dir_value = config.get("skill_dir")
    if not isinstance(source, str) or _validate_source(source) != source:
        raise AgentDeliveryError("The saved update source is invalid; reinstall through the trusted installer.")
    if not isinstance(ref, str) or not ref or ref.startswith("-") or "\x00" in ref:
        raise AgentDeliveryError("The saved update ref is invalid; reinstall through the trusted installer.")
    if not isinstance(skill_dir_value, str) or not Path(skill_dir_value).is_absolute():
        raise AgentDeliveryError("The saved Skill directory is invalid; reinstall through the trusted installer.")
    return config


def _current_receipt(root: Path, *, validate_skill: bool = True) -> dict[str, Any]:
    record = _read_json(root / CURRENT_NAME, "installation receipt")
    if record.get("schema_version") != CURRENT_SCHEMA:
        raise AgentDeliveryError("The installation receipt has an unsupported schema.")
    if not isinstance(record.get("source_sha"), str) or not SHA_RE.fullmatch(record["source_sha"]):
        raise AgentDeliveryError("The installation receipt has no exact source commit SHA.")
    if not isinstance(record.get("skill_sha256"), str) or not HASH_RE.fullmatch(record["skill_sha256"]):
        raise AgentDeliveryError("The installation receipt has no valid installed Skill SHA-256.")
    for field in ("protocol_version", "package_version", "last_successful_update"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise AgentDeliveryError(f"The installation receipt is missing {field}.")
    config = _config_for_root(root)
    if record.get("source") != config["source"] or record.get("ref") != config["ref"]:
        raise AgentDeliveryError("The installation receipt does not match the fixed update source/ref.")
    release = root / "releases" / record["source_sha"]
    python_path = _venv_python(release)
    if not python_path.is_file():
        raise AgentDeliveryError("The active managed release is missing its Python environment.")
    if validate_skill:
        skill_dir = Path(config["skill_dir"])
        skill_path = skill_dir / "SKILL.md"
        marker_path = skill_dir / ".agent-pull-owned.json"
        if skill_dir.is_symlink() or skill_path.is_symlink() or marker_path.is_symlink():
            raise AgentDeliveryError("The managed Skill path contains a symlink; refusing to follow it.")
        marker = _read_json(marker_path, "Skill ownership marker")
        try:
            actual_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise AgentDeliveryError("The installed agent-pull Skill is missing or unreadable.") from exc
        if (marker.get("owner") != "agent-delivery-loop" or marker.get("skill_sha256") != record["skill_sha256"]
                or actual_hash != record["skill_sha256"]):
            raise AgentDeliveryError("The installed Skill changed outside the managed updater; refusing to overwrite it.")
    release_marker = _read_json(release / ".agent-pull-release.json", "release marker")
    if (release_marker.get("owner") != "agent-delivery-loop" or release_marker.get("state") != "complete"
            or release_marker.get("source_sha") != record["source_sha"]
            or release_marker.get("protocol_version") != record["protocol_version"]
            or release_marker.get("package_version") != record["package_version"]):
        raise AgentDeliveryError("The active package release does not match its installation receipt.")
    return record


def _venv_python(release: Path) -> Path:
    return release / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def load_install_receipt(*, protocol_version: str | None = None) -> dict[str, Any]:
    """Return the verified install metadata for core pull records.

    Managed launchers set ``AGENT_PULL_INSTALL_ROOT``. A source checkout or
    ordinary un-managed package install returns an explicit uninstalled record.
    """
    active_protocol = protocol_version
    if active_protocol is None:
        try:
            from .pull import PULL_PROTOCOL_VERSION

            active_protocol = PULL_PROTOCOL_VERSION
        except (ImportError, AttributeError):
            active_protocol = None
    root_value = os.environ.get("AGENT_PULL_INSTALL_ROOT")
    if not root_value:
        return {
            "installation_status": "uninstalled",
            "install_sha": None,
            "skill_sha256": None,
            "protocol_version": active_protocol,
            "package_version": None,
            "last_successful_update": None,
            "source": "development",
            "ref": None,
        }
    root = _canonical_root(Path(root_value))
    receipt = _current_receipt(root)
    active_release = (root / "releases" / receipt["source_sha"] / "venv").resolve()
    if Path(sys.prefix).resolve() != active_release or not Path(__file__).resolve().is_relative_to(active_release):
        raise AgentDeliveryError("The running CLI is not the active managed release; refusing to label its version.")
    try:
        running_package_version = importlib.metadata.version("agent-delivery-loop")
    except importlib.metadata.PackageNotFoundError as exc:
        raise AgentDeliveryError("The active managed package metadata is missing.") from exc
    if running_package_version != receipt["package_version"]:
        raise AgentDeliveryError("The running package version does not match the active installation receipt.")
    if active_protocol and receipt["protocol_version"] != active_protocol:
        raise AgentDeliveryError("The active pull protocol differs from the installation receipt; refusing this run.")
    return {
        "installation_status": "installed",
        "install_sha": receipt["source_sha"],
        "skill_sha256": receipt["skill_sha256"],
        "protocol_version": receipt["protocol_version"],
        "package_version": receipt["package_version"],
        "last_successful_update": receipt["last_successful_update"],
        "source": receipt["source"],
        "ref": receipt["ref"],
    }


def _ensure_skill_target(config: dict[str, Any], *, allowed_hashes: set[str | None]) -> Path:
    skill_dir = Path(config["skill_dir"])
    if skill_dir.is_symlink():
        raise AgentDeliveryError("The selected Skill directory became a symlink; refusing to follow it.")
    if skill_dir.exists():
        if not skill_dir.is_dir():
            raise AgentDeliveryError("The selected Skill path exists and is not a directory.")
        marker = skill_dir / ".agent-pull-owned.json"
        skill_path = skill_dir / "SKILL.md"
        if skill_path.is_symlink() or marker.is_symlink():
            raise AgentDeliveryError("The selected Skill directory contains a symlink; refusing to follow it.")
        if skill_path.exists() and not skill_path.is_file():
            raise AgentDeliveryError("The selected SKILL.md path exists and is not a regular file.")
        actual_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest() if skill_path.is_file() else None
        owned_marker: dict[str, Any] | None = None
        if marker.is_file():
            prior = _read_json(marker, "Skill ownership marker")
            owned_hashes = {digest for digest in allowed_hashes if digest is not None}
            if prior.get("owner") != "agent-delivery-loop":
                raise AgentDeliveryError("The selected Skill directory is owned by another installer.")
            if prior.get("skill_sha256") not in owned_hashes or actual_hash not in owned_hashes:
                raise AgentDeliveryError("The installed Skill changed locally; refusing to replace it.")
            owned_marker = prior
        contents = {item.name for item in skill_dir.iterdir()}
        allowed_files = {"SKILL.md", ".agent-pull-owned.json"}
        if owned_marker is None:
            # During the first install, an activation intent lets us recognize
            # only the exact candidate file written before an interruption.
            if contents - {"SKILL.md"} or actual_hash not in allowed_hashes:
                raise AgentDeliveryError("The selected Skill directory already contains unowned files; choose an empty path.")
        else:
            if owned_marker.get("skill_sha256") not in allowed_hashes or actual_hash not in allowed_hashes:
                raise AgentDeliveryError("The installed Skill changed locally; refusing to replace it.")
            if contents - allowed_files:
                # Preserve additional resources in an owned Skill directory.
                pass
    else:
        skill_dir.mkdir(parents=True, exist_ok=True)
    return skill_dir


def _skill_bytes(skill_template: bytes, root: Path) -> bytes:
    try:
        template = skill_template.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentDeliveryError("The packaged agent-pull Skill is not valid UTF-8.") from exc
    if SKILL_TEMPLATE_TOKEN not in template:
        raise AgentDeliveryError("The packaged agent-pull Skill has no install-root token.")
    # Adjacent quoted and unquoted shell fragments form one word, so this stays
    # valid for install roots that contain spaces.
    return template.replace(SKILL_TEMPLATE_TOKEN, shlex.quote(str(root))).encode("utf-8")


def _write_managed_skill(skill_dir: Path, content: bytes, *, source_sha: str) -> str:
    skill_path = skill_dir / "SKILL.md"
    marker_path = skill_dir / ".agent-pull-owned.json"
    digest = hashlib.sha256(content).hexdigest()
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=skill_dir, prefix=".SKILL.", delete=False) as file:
            temp_path = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, skill_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    _write_skill_marker(skill_dir, source_sha=source_sha, skill_sha256=digest)
    return digest


def _write_skill_marker(skill_dir: Path, *, source_sha: str, skill_sha256: str) -> None:
    marker_path = skill_dir / ".agent-pull-owned.json"
    marker = {"owner": "agent-delivery-loop", "source_sha": source_sha, "skill_sha256": skill_sha256}
    try:
        if _read_json(marker_path, "Skill ownership marker") == marker:
            return
    except AgentDeliveryError:
        pass
    _atomic_json(marker_path, marker)


def _release_skill_bytes(release: Path) -> bytes:
    path_code = (
        "import importlib.resources; print(importlib.resources.files('agent_delivery_loop')"
        ".joinpath('assets/skills/agent-pull/SKILL.md'))"
    )
    result = _run([str(_venv_python(release)), "-c", path_code], timeout=30)
    skill_path = Path(result.stdout.decode("utf-8").strip())
    try:
        return skill_path.read_bytes()
    except OSError as exc:
        raise AgentDeliveryError("The active package release is missing its agent-pull Skill asset.") from exc


def _ensure_private_grant_dir(root: Path) -> None:
    grant_dir = root / "state" / "grants"
    grant_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = grant_dir.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AgentDeliveryError("The managed grant directory must be user-owned and private (0700 or stricter).")


def _recover_activation(root: Path, config: dict[str, Any]) -> None:
    intent_path = root / "activation.json"
    if not intent_path.exists():
        return
    intent = _read_json(intent_path, "activation intent")
    if intent.get("owner") != "agent-delivery-loop" or intent.get("schema_version") != 1:
        raise AgentDeliveryError("The interrupted activation intent is invalid; refusing to continue.")
    record = intent.get("record")
    if (not isinstance(record, dict) or record.get("source") != config["source"]
            or record.get("ref") != config["ref"] or not isinstance(record.get("source_sha"), str)
            or not SHA_RE.fullmatch(record["source_sha"])):
        raise AgentDeliveryError("The interrupted activation does not match the fixed update source/ref.")
    release = root / "releases" / record["source_sha"]
    meta = _probe_release(_venv_python(release))
    release_marker = _read_json(release / ".agent-pull-release.json", "release marker")
    if (release_marker.get("owner") != "agent-delivery-loop" or release_marker.get("state") != "complete"
            or release_marker.get("source_sha") != record["source_sha"]):
        raise AgentDeliveryError("The interrupted activation release is not complete.")
    expected_content = _skill_bytes(_release_skill_bytes(release), root)
    expected_hash = hashlib.sha256(expected_content).hexdigest()
    if (intent.get("skill_sha256") != expected_hash or record.get("skill_sha256") != expected_hash
            or record.get("protocol_version") != meta.get("protocol_version")
            or record.get("package_version") != meta.get("package_version")
            or release_marker.get("protocol_version") != meta.get("protocol_version")
            or release_marker.get("package_version") != meta.get("package_version")):
        raise AgentDeliveryError("The interrupted activation contents do not match the built release.")

    current: dict[str, Any] | None = None
    try:
        current = _read_json(root / CURRENT_NAME, "installation receipt")
    except AgentDeliveryError:
        if (root / CURRENT_NAME).exists():
            raise
    previous_sha = intent.get("previous_source_sha")
    previous_skill = intent.get("previous_skill_sha256")
    if current is not None and current.get("source_sha") not in {previous_sha, record["source_sha"]}:
        raise AgentDeliveryError("The active release changed after this interrupted activation; refusing recovery.")
    if current is None and previous_sha is not None:
        raise AgentDeliveryError("The previous release receipt is missing; refusing activation recovery.")

    skill_dir = Path(config["skill_dir"])
    candidate_path = skill_dir / "SKILL.md"
    marker_path = skill_dir / ".agent-pull-owned.json"
    actual_hash: str | None = None
    if candidate_path.is_symlink() or marker_path.is_symlink() or skill_dir.is_symlink():
        raise AgentDeliveryError("The interrupted Skill path contains a symlink; refusing recovery.")
    if candidate_path.is_file():
        actual_hash = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    allowed = {expected_hash, previous_skill}
    if current is not None and current.get("source_sha") == record["source_sha"]:
        if current.get("skill_sha256") != expected_hash:
            raise AgentDeliveryError("The active release receipt conflicts with its activation intent.")
        _current_receipt(root)
        _write_launchers(root)
        intent_path.unlink(missing_ok=True)
        return
    elif current is not None and current.get("skill_sha256") != previous_skill:
        raise AgentDeliveryError("The previous receipt conflicts with its activation intent.")
    _ensure_skill_target(config, allowed_hashes=allowed)
    if marker_path.is_file():
        marker = _read_json(marker_path, "Skill ownership marker")
        owned_hashes = {digest for digest in allowed if digest is not None}
        if marker.get("owner") != "agent-delivery-loop" or marker.get("skill_sha256") not in owned_hashes:
            raise AgentDeliveryError("The interrupted Skill is not owned by this installation; refusing recovery.")
    elif previous_sha is not None:
        raise AgentDeliveryError("The previous Skill ownership marker is missing; refusing recovery.")
    if actual_hash not in allowed:
        raise AgentDeliveryError("The interrupted Skill does not match the old or candidate managed content.")
    if actual_hash != expected_hash:
        _write_managed_skill(skill_dir, expected_content, source_sha=record["source_sha"])
    else:
        _write_skill_marker(skill_dir, source_sha=record["source_sha"], skill_sha256=expected_hash)
    _ensure_private_grant_dir(root)
    _atomic_json(root / CURRENT_NAME, record)
    _write_launchers(root)
    intent_path.unlink(missing_ok=True)


def _write_launchers(root: Path) -> None:
    launch_dir = root / "bin"
    launch_dir.mkdir(parents=True, exist_ok=True)
    root_literal = repr(str(root))
    shared = f'''#!/usr/bin/env python3
from pathlib import Path
import json, os, sys
ROOT = Path({root_literal})
def active():
    try:
        rec = json.loads((ROOT / "{CURRENT_NAME}").read_text(encoding="utf-8"))
        sha = rec["source_sha"]
        if len(sha) != 40 or any(ch not in "0123456789abcdef" for ch in sha):
            raise ValueError()
        release = ROOT / "releases" / sha
        if not (release / "venv").is_dir():
            raise ValueError()
        return release
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        print("agent-pull: managed installation receipt is invalid", file=sys.stderr)
        raise SystemExit(2)
'''
    updater = shared + '''release = active()
python = release / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
env = os.environ.copy()
env["AGENT_PULL_INSTALL_ROOT"] = str(ROOT)
os.execve(str(python), [str(python), "-m", "agent_delivery_loop.pull_install", "update", "--root", str(ROOT), *sys.argv[1:]], env)
'''
    pull = shared + '''release = active()
python = release / ("venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python")
env = os.environ.copy()
env["AGENT_PULL_INSTALL_ROOT"] = str(ROOT)
code = "import sys; from agent_delivery_loop.pull import main; raise SystemExit(main(sys.argv[1:]))"
os.execve(str(python), [str(python), "-c", code, *sys.argv[1:]], env)
'''
    for name, body in (("agent-pull-update", updater), ("agent-pull", pull)):
        path = launch_dir / name
        temp = launch_dir / f".{name}.tmp"
        temp.write_text(body, encoding="utf-8")
        os.chmod(temp, 0o755)
        os.replace(temp, path)


def _prepare_root(root: Path, source: str, ref: str, skill_dir: Path) -> dict[str, Any]:
    config_path = root / CONFIG_NAME
    expected = {
        "schema_version": CONFIG_SCHEMA,
        "install_root": str(root),
        "source": source,
        "ref": ref,
        "skill_dir": str(skill_dir),
    }
    if config_path.exists():
        old = _config_for_root(root)
        if any(old.get(key) != expected[key] for key in expected):
            raise AgentDeliveryError("This managed install is already bound to another source, ref, or Skill path.")
        os.chmod(config_path, 0o400)
        return old
    allowed = {".update.lock"}
    unexpected = [item.name for item in root.iterdir() if item.name not in allowed]
    if unexpected:
        raise AgentDeliveryError("The install directory is non-empty and is not managed by this installer.")
    _atomic_json(config_path, expected)
    os.chmod(config_path, 0o400)
    return expected


def _validate_ref(ref: str) -> str:
    if not ref or ref.startswith("-") or "\x00" in ref:
        raise AgentDeliveryError("The configured source ref is invalid.")
    try:
        _run(["git", "check-ref-format", "--branch", ref], timeout=15)
    except AgentDeliveryError as exc:
        raise AgentDeliveryError("The configured source ref must be a valid Git branch name.") from exc
    return ref


def _fetch_source(root: Path, config: dict[str, Any]) -> tuple[Path, str]:
    cache = root / "source"
    if cache.is_symlink():
        raise AgentDeliveryError("The managed source checkout is a symlink; refusing to follow it.")
    if not (cache / ".git").is_dir():
        if cache.exists():
            raise AgentDeliveryError("The managed source path exists but is not an installer-owned Git checkout.")
        cache.mkdir(parents=True)
        _run(["git", "init", "--quiet", str(cache)])
        _run(["git", "-C", str(cache), "remote", "add", "origin", config["source"]])
    else:
        remote = _run(["git", "-C", str(cache), "remote", "get-url", "origin"], timeout=15).stdout.decode().strip()
        if _validate_source(remote) != config["source"]:
            raise AgentDeliveryError("The cached Git remote differs from the fixed update source.")
    ref = _validate_ref(config["ref"])
    _run(["git", "-C", str(cache), "fetch", "--no-tags", "--force", "origin", f"refs/heads/{ref}"])
    sha = _run(["git", "-C", str(cache), "rev-parse", "--verify", "FETCH_HEAD^{commit}"], timeout=15).stdout.decode().strip().lower()
    if not SHA_RE.fullmatch(sha):
        raise AgentDeliveryError("The configured Git ref did not resolve to an exact commit SHA.")
    return cache, sha


def _probe_release(python: Path) -> dict[str, Any]:
    code = (
        "import hashlib,importlib.metadata,importlib.resources,json; "
        "import agent_delivery_loop.pull as p; "
        "d=importlib.resources.files('agent_delivery_loop').joinpath('assets/skills/agent-pull/SKILL.md').read_bytes(); "
        "print(json.dumps({'package_version':importlib.metadata.version('agent-delivery-loop'),"
        "'protocol_version':p.PULL_PROTOCOL_VERSION,'skill_sha256':hashlib.sha256(d).hexdigest()}))"
    )
    result = _run([str(python), "-c", code], timeout=30)
    try:
        record = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AgentDeliveryError("The installed release did not return valid package metadata.") from exc
    if (not isinstance(record, dict) or not isinstance(record.get("package_version"), str)
            or not isinstance(record.get("protocol_version"), str)
            or not HASH_RE.fullmatch(str(record.get("skill_sha256", "")))):
        raise AgentDeliveryError("The candidate release is missing pull protocol or packaged Skill metadata.")
    return record


def _build_release(root: Path, cache: Path, sha: str) -> tuple[Path, dict[str, Any]]:
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / sha
    if target.is_symlink():
        raise AgentDeliveryError("The candidate release path is a symlink; refusing to follow it.")
    if target.exists():
        marker = target / ".agent-pull-release.json"
        if (not marker.is_file() or _read_json(marker, "release marker").get("owner") != "agent-delivery-loop"
                or _read_json(marker, "release marker").get("source_sha") != sha):
            raise AgentDeliveryError("An unowned directory occupies the requested release path.")
        marker_value = _read_json(marker, "release marker")
        if marker_value.get("state") == "building":
            shutil.rmtree(target)
        else:
            if marker_value.get("state") != "complete":
                raise AgentDeliveryError("The release marker is invalid; refusing to replace this release.")
            python = _venv_python(target)
            try:
                meta = _probe_release(python)
            except AgentDeliveryError:
                shutil.rmtree(target)
            else:
                return target, meta

    # Keep the venv at its final path. Python virtual environments are not
    # relocatable because console-script shebangs and sys.prefix are absolute.
    target.mkdir()
    _atomic_json(target / ".agent-pull-release.json", {
        "owner": "agent-delivery-loop", "state": "building", "source_sha": sha,
    })
    temp = Path(tempfile.mkdtemp(prefix=f".{sha}.", dir=root))
    try:
        checkout = temp / "checkout"
        wheelhouse = temp / "wheelhouse"
        wheelhouse.mkdir()
        _run(["git", "clone", "--quiet", "--shared", "--no-checkout", str(cache), str(checkout)])
        _run(["git", "-C", str(checkout), "checkout", "--quiet", "--detach", sha])
        # Require package data in source and install only an immutable wheel built
        # from the fetched commit. The package code itself is the trusted release.
        _run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), str(checkout)])
        wheels = list(wheelhouse.glob("agent_delivery_loop-*.whl"))
        if len(wheels) != 1:
            raise AgentDeliveryError("The fetched source did not build exactly one agent-delivery-loop wheel.")
        venv.EnvBuilder(with_pip=True).create(target / "venv")
        python = _venv_python(target)
        _run([str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])], timeout=600)
        meta = _probe_release(python)
        if meta["skill_sha256"] != hashlib.sha256(
            (checkout / "src/agent_delivery_loop" / SKILL_RESOURCE).read_bytes()
        ).hexdigest():
            raise AgentDeliveryError("The built wheel Skill does not match the fetched source asset.")
        _atomic_json(target / ".agent-pull-release.json", {
            "owner": "agent-delivery-loop", "state": "complete", "source_sha": sha, **meta,
        })
        return target, meta
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _switch_release(root: Path, config: dict[str, Any], sha: str, meta: dict[str, Any],
                    *, updated: bool) -> dict[str, Any]:
    release = root / "releases" / sha
    rendered = _skill_bytes(_release_skill_bytes(release), root)
    skill_sha = hashlib.sha256(rendered).hexdigest()
    previous: dict[str, Any] | None = None
    if (root / CURRENT_NAME).exists():
        previous = _current_receipt(root, validate_skill=False)
    try:
        protocol_version = meta["protocol_version"]
        package_version = meta["package_version"]
    except KeyError as exc:
        raise AgentDeliveryError("The candidate release metadata is incomplete.") from exc
    record = {
        "schema_version": CURRENT_SCHEMA,
        "source": config["source"],
        "ref": config["ref"],
        "source_sha": sha,
        "skill_sha256": skill_sha,
        "protocol_version": protocol_version,
        "package_version": package_version,
        "last_successful_update": _utc_now() if updated else None,
    }
    if not updated and previous is not None:
        record["last_successful_update"] = previous["last_successful_update"]
    intent = {
        "owner": "agent-delivery-loop",
        "schema_version": 1,
        "previous_source_sha": previous.get("source_sha") if previous else None,
        "previous_skill_sha256": previous.get("skill_sha256") if previous else None,
        "skill_sha256": skill_sha,
        "record": record,
    }
    _atomic_json(root / "activation.json", intent)
    skill_dir = _ensure_skill_target(
        config,
        allowed_hashes={skill_sha, previous.get("skill_sha256") if previous else None},
    )
    skill_path = skill_dir / "SKILL.md"
    existing_hash = hashlib.sha256(skill_path.read_bytes()).hexdigest() if skill_path.is_file() else None
    marker_path = skill_dir / ".agent-pull-owned.json"
    if marker_path.is_file():
        marker = _read_json(marker_path, "Skill ownership marker")
        allowed = {skill_sha}
        if previous is not None:
            allowed.add(previous.get("skill_sha256"))
        if marker.get("owner") != "agent-delivery-loop" or marker.get("skill_sha256") not in allowed:
            raise AgentDeliveryError("The selected Skill directory is not owned by this installation.")
    elif previous is not None:
        raise AgentDeliveryError("The previously installed Skill ownership marker is missing.")
    if existing_hash != skill_sha:
        _write_managed_skill(skill_dir, rendered, source_sha=sha)
    else:
        _write_skill_marker(skill_dir, source_sha=sha, skill_sha256=skill_sha)
    _ensure_private_grant_dir(root)
    _atomic_json(root / CURRENT_NAME, record)
    _write_launchers(root)
    (root / "activation.json").unlink(missing_ok=True)
    return record


def _result(status: str, root: Path, record: dict[str, Any]) -> dict[str, Any]:
    if status == "updated":
        summary = "The managed package and Skill were updated to this exact source commit."
        next_actions = ["Stop this activation so the next host activation can load the updated Skill and protocol."]
    elif status == "installed":
        summary = "The managed package, stable launchers, and Skill were installed at the requested local paths."
        next_actions = [
            "Make the selected Skill directory visible to the chosen host.",
            "Configure the host's existing activation path to call updater_launcher before waking this Agent, where supported.",
        ]
    else:
        summary = "The managed package and Skill already match the configured source ref."
        next_actions = ["Continue with the Skill loaded for this activation."]
    return {
        "status": status,
        "summary": summary,
        "install_root": str(root),
        "source": record["source"],
        "ref": record["ref"],
        "source_sha": record["source_sha"],
        "skill_sha256": record["skill_sha256"],
        "protocol_version": record["protocol_version"],
        "package_version": record["package_version"],
        "last_successful_update": record["last_successful_update"],
        "agent_pull_launcher": str(root / "bin" / "agent-pull"),
        "updater_launcher": str(root / "bin" / "agent-pull-update"),
        "artifacts": [],
        "next_actions": next_actions,
    }


def install(*, source: str, ref: str, root_path: Path, skill_path: Path) -> dict[str, Any]:
    source = _validate_source(source)
    root = _canonical_root(root_path)
    skill_dir = _canonical_skill_dir(skill_path)
    if root == skill_dir or root in skill_dir.parents or skill_dir in root.parents:
        raise AgentDeliveryError("Keep the managed install root and Skill directory as separate paths.")
    if sys.version_info < (3, 11):
        raise AgentDeliveryError("The managed agent-pull environment requires Python 3.11 or newer.")
    with _update_lock(root):
        config = _prepare_root(root, source, _validate_ref(ref), skill_dir)
        _recover_activation(root, config)
        cache, sha = _fetch_source(root, config)
        release, meta = _build_release(root, cache, sha)
        current_sha = None
        try:
            current_sha = _current_receipt(root, validate_skill=False)["source_sha"]
        except AgentDeliveryError:
            pass
        record = _switch_release(root, config, sha, meta, updated=(current_sha != sha))
        return _result("current" if current_sha == sha else "installed", root, record)


def update(*, root_path: Path) -> dict[str, Any]:
    root = _canonical_root(root_path)
    with _update_lock(root):
        config = _config_for_root(root)
        _recover_activation(root, config)
        current = _current_receipt(root)
        cache, sha = _fetch_source(root, config)
        if sha == current["source_sha"]:
            return _result("current", root, current)
        _, meta = _build_release(root, cache, sha)
        record = _switch_release(root, config, sha, meta, updated=True)
        return _result("updated", root, record)


def _print_result(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def install_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-pull-install", description="Install the trusted local agent-pull adapter.")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="Fixed GitHub repository, saved for future updates")
    parser.add_argument("--ref", default=DEFAULT_REF, help="Fixed Git branch, saved for future updates")
    parser.add_argument("--root", required=True, type=Path, help="Dedicated managed install directory")
    parser.add_argument("--skill-dir", required=True, type=Path, help="Explicit empty path for the agent-pull Skill")
    args = parser.parse_args(argv)
    try:
        _print_result(install(source=args.source, ref=args.ref, root_path=args.root, skill_path=args.skill_dir))
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, AgentDeliveryError) else "Unexpected installer failure; no agent was started."
        print(f"agent-pull-install: {message}", file=sys.stderr)
        return 2


def update_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-pull-update", description="Update the managed local agent-pull adapter.")
    parser.add_argument("--root", required=True, type=Path, help="Existing managed install directory")
    args = parser.parse_args(argv)
    try:
        _print_result(update(root_path=args.root))
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, AgentDeliveryError) else "Unexpected updater failure; no agent was started."
        print(f"agent-pull-update: {message}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-pull-install")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install_parser = subparsers.add_parser("install", help="Create the fixed managed install")
    install_parser.add_argument("--source", default=DEFAULT_SOURCE)
    install_parser.add_argument("--ref", default=DEFAULT_REF)
    install_parser.add_argument("--root", required=True, type=Path)
    install_parser.add_argument("--skill-dir", required=True, type=Path)
    update_parser = subparsers.add_parser("update", help="Fetch and activate the saved source/ref")
    update_parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = (install(source=args.source, ref=args.ref, root_path=args.root, skill_path=args.skill_dir)
                  if args.command == "install" else update(root_path=args.root))
        _print_result(result)
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, AgentDeliveryError) else "Unexpected updater failure; no agent was started."
        print(f"agent-pull-install: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
