#!/usr/bin/env python3
"""Incremental local-file inbox. Discovery stays pending until business recording is acknowledged.

No model, network, scheduler, or business-file writes. JSON state is external to the
watched tree; initialize once, scan repeatedly, acknowledge only processed IDs.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

@contextmanager
def _state_lock(state_path):
    # Local macOS/POSIX locking without a development-venv-only dependency.
    import fcntl
    with open(str(state_path) + ".lock", "ab") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield  # Closing the descriptor releases the lock, including after a crash.


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ignored(name):
    return (name.startswith((".", "~$")) or name.endswith("~")
            or name.lower().endswith((".tmp", ".part", ".crdownload", ".swp")))


def _fingerprint(path):
    before = path.stat()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Not a regular non-symlink file")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    attrs = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, key) != getattr(after, key) for key in attrs):
        raise ValueError("File changed during scan; retry next time")
    return {"sha256": digest, "size": after.st_size, "mtime_ns": after.st_mtime_ns,
            "device": after.st_dev, "inode": after.st_ino}


def _snapshot(root):
    files, errors = {}, []

    def failed(error):
        errors.append({"path": str(error.filename or root), "error": str(error)})

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=failed):
        dirs[:] = sorted(d for d in dirs if not _ignored(d) and not (Path(directory) / d).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if _ignored(name) or path.is_symlink():
                continue
            try:
                if not path.resolve().is_relative_to(root):
                    raise ValueError("File resolves outside watched root")
                files[path.relative_to(root).as_posix()] = _fingerprint(path)
            except (OSError, ValueError) as exc:
                errors.append({"path": str(path), "error": str(exc)})
    return files, errors


def _paths(root, state_path):
    root, state_path = Path(root).resolve(strict=True), Path(state_path).resolve()
    if not root.is_dir():
        raise ValueError("Watched root must be a directory")
    if state_path.is_relative_to(root):
        raise ValueError("State must be stored outside the watched directory")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return root, state_path


def _load(root, state_path):
    data = json.loads(state_path.read_text(encoding="utf-8"))
    if (data.get("schema_version") != 1 or data.get("root") != str(root)
            or not all(isinstance(data.get(key), dict) for key in ("seen", "pending", "acknowledged"))):
        raise ValueError("Invalid state or wrong watched root; refusing to reset the baseline")
    return data


def _save(state_path, data):
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=state_path.parent,
                                         prefix=state_path.name + ".", suffix=".tmp", delete=False) as stream:
            name = stream.name
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, state_path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def initialize(root, state_path):
    root, state_path = _paths(root, state_path)
    with _state_lock(state_path):
        if state_path.exists():
            data = _load(root, state_path)
            return {"initialized": False, "baseline_at": data["baseline_at"],
                    "baseline_files": data["baseline_files"], "pending_count": len(data["pending"])}
        files, errors = _snapshot(root)
        if errors:
            raise ValueError(f"Baseline scan incomplete; nothing initialized: {errors}")
        data = {"schema_version": 1, "root": str(root), "baseline_at": _now(),
                "baseline_files": len(files), "seen": files, "pending": {}, "acknowledged": {}}
        _save(state_path, data)
        return {"initialized": True, "baseline_at": data["baseline_at"],
                "baseline_files": len(files), "pending_count": 0}


def scan(root, state_path):
    root, state_path = _paths(root, state_path)
    with _state_lock(state_path):
        # Missing/corrupt state is an error, never a silent new baseline that eats new files.
        data = _load(root, state_path)
        files, errors = _snapshot(root)
        pending = data["pending"]
        # A local move changes the name, not the inbox item. Require inode AND
        # content equality, and require the old name to be absent (copies stay new).
        absent = {(info["device"], info["inode"], info["sha256"]): relative
                  for relative, info in data["seen"].items() if relative not in files}
        for relative, info in files.items():
            if relative in data["seen"]:
                continue
            previous = absent.pop((info["device"], info["inode"], info["sha256"]), None)
            if previous is not None:
                data["seen"][relative] = info
                if previous in pending:
                    item = pending.pop(previous)
                    item.update(path=str(root / relative), relative_path=relative)
                    pending[relative] = item
        new_count = 0
        for relative, info in files.items():
            old = pending.get(relative)
            if relative not in data["seen"] or (old and info["sha256"] != old["sha256"]):
                identifier = hashlib.sha256((relative + "\0" + info["sha256"]).encode()).hexdigest()
                pending[relative] = {"id": identifier, "path": str(root / relative),
                                     "relative_path": relative, "sha256": info["sha256"],
                                     "size": info["size"], "discovered_at": _now()}
                new_count += 1
            data["seen"][relative] = info
        for relative, item in pending.items():
            item["available"] = relative in files
        _save(state_path, data)
        return {"ok": not errors, "root": str(root), "new_count": new_count,
                "pending_count": len(pending), "pending": list(pending.values()), "errors": errors}


def acknowledge(root, state_path, identifiers, record, *, allow_missing=False):
    if not record.strip():
        raise ValueError("A durable business-record reference is required; discovery is not processing")
    if not identifiers:
        raise ValueError("At least one pending ID is required")
    root, state_path = _paths(root, state_path)
    with _state_lock(state_path):
        data = _load(root, state_path)
        indexed = {value["id"]: key for key, value in data["pending"].items()}
        for identifier in identifiers:
            if identifier in data["acknowledged"]:
                continue
            if identifier not in indexed:
                raise ValueError(f"Unknown or superseded pending ID: {identifier}")
            item = data["pending"][indexed[identifier]]
            path = Path(item["path"])
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("Pending file resolves outside watched root")
            try:
                info = _fingerprint(path)
            except FileNotFoundError:
                if allow_missing:
                    continue  # Explicitly record disappearance; never infer business completion.
                raise
            if info["sha256"] != item["sha256"]:
                raise ValueError("File changed after discovery; scan and analyze the new version first")
        for identifier in set(identifiers):
            if identifier in data["acknowledged"]:
                continue
            item = data["pending"].pop(indexed[identifier])
            data["acknowledged"][identifier] = {**item, "record": record, "acknowledged_at": _now()}
        _save(state_path, data)
        return {"ok": True, "acknowledged": sorted(set(identifiers)), "pending_count": len(data["pending"])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "scan", "ack"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--record", default="")
    parser.add_argument("--allow-missing", action="store_true",
                        help="Acknowledge a removed file only after recording its unresolved disposition")
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            result = initialize(args.root, args.state)
        elif args.action == "scan":
            result = scan(args.root, args.state)
        else:
            result = acknowledge(args.root, args.state, args.id, args.record, allow_missing=args.allow_missing)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok", True) else 1
    except (OSError, ValueError, TimeoutError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
