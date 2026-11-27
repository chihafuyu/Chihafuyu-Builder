"""Verify a cached Docker/OCI image tarball against a resolved config digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tarfile
from typing import Any

MAX_METADATA_BYTES = 10 * 1024 * 1024


def _normalize_member(name: str) -> str:
    """Strip leading './' segments from a tar member path."""
    cleaned = name.lstrip("/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _read_member_bytes(
    tar: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    path: str,
) -> bytes:
    """Read a regular file member safely with an upper size bound."""
    info = members.get(_normalize_member(path))
    if info is None or not info.isfile():
        raise RuntimeError(f"Missing tarball member: {path}")
    if info.size > MAX_METADATA_BYTES:
        raise RuntimeError(f"Tarball metadata member too large: {path}")
    handle = tar.extractfile(info)
    if handle is None:
        raise RuntimeError(f"Unreadable tarball member: {path}")
    with handle:
        data = handle.read(MAX_METADATA_BYTES + 1)
    if len(data) > MAX_METADATA_BYTES:
        raise RuntimeError(f"Tarball metadata member exceeded limit: {path}")
    return data


def _sha256_digest(data: bytes) -> str:
    """Return the ``sha256:<hex>`` digest of *data*."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _config_path_from_manifest(
    tar: tarfile.TarFile, members: dict[str, tarfile.TarInfo]
) -> str | None:
    """Extract the config blob path from Docker's ``manifest.json``."""
    if "manifest.json" not in members:
        return None
    raw = _read_member_bytes(tar, members, "manifest.json")
    try:
        entries = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RuntimeError("Invalid manifest.json in image tarball") from err
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        raise RuntimeError("Unexpected manifest.json structure in image tarball")
    cfg_path = entries[0].get("Config")
    if not isinstance(cfg_path, str) or not cfg_path:
        raise RuntimeError("Missing Config path in manifest.json")
    return cfg_path


def _config_path_from_oci_index(
    tar: tarfile.TarFile,
    members: dict[str, tarfile.TarInfo],
    expected_digest: str,
    expected_config_digest: str,
) -> str:
    """Resolve and verify the config blob path via OCI ``index.json``."""
    if "index.json" not in members:
        raise RuntimeError("Image tarball has neither manifest.json nor index.json")
    raw_idx = _read_member_bytes(tar, members, "index.json")
    try:
        index_obj = json.loads(raw_idx.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RuntimeError("Invalid index.json in OCI tarball") from err

    manifests = index_obj.get("manifests") if isinstance(index_obj, dict) else None
    if not isinstance(manifests, list) or not manifests:
        raise RuntimeError("No manifests listed in OCI index.json")

    selected: dict[str, Any] | None = None
    if expected_digest:
        for item in manifests:
            if isinstance(item, dict) and item.get("digest") == expected_digest:
                selected = item
                break
    if selected is None and len(manifests) == 1 and isinstance(manifests[0], dict):
        selected = manifests[0]
    if selected is None:
        raise RuntimeError("Expected manifest digest not found in OCI index.json")

    manifest_digest = str(selected.get("digest") or "")
    if not manifest_digest.startswith("sha256:"):
        raise RuntimeError("Unsupported manifest digest in OCI index.json")
    manifest_bytes = _read_member_bytes(
        tar, members, f"blobs/sha256/{manifest_digest.split(':', 1)[1]}"
    )
    if _sha256_digest(manifest_bytes) != manifest_digest:
        raise RuntimeError("Manifest digest mismatch in OCI tarball")
    if expected_digest and manifest_digest != expected_digest:
        raise RuntimeError("Manifest digest does not match resolved image digest")

    try:
        manifest_obj = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RuntimeError("Invalid manifest JSON in OCI tarball") from err
    cfg_digest = str(
        (manifest_obj.get("config") or {}).get("digest") or ""
        if isinstance(manifest_obj, dict)
        else ""
    )
    if cfg_digest != expected_config_digest:
        raise RuntimeError("OCI manifest config digest mismatch")
    return f"blobs/sha256/{cfg_digest.split(':', 1)[1]}"


def verify_tarball(
    tar_path: str,
    expected_config_digest: str,
    expected_digest: str = "",
) -> list[str]:
    """Verify the tarball config digest and return ``rootfs.diff_ids``."""
    if not expected_config_digest.startswith("sha256:"):
        raise ValueError(f"Invalid config digest: {expected_config_digest!r}")

    with tarfile.open(tar_path, "r:*") as tar:
        members = {
            _normalize_member(member.name): member
            for member in tar.getmembers()
        }
        cfg_path = _config_path_from_manifest(tar, members)
        if cfg_path is None:
            cfg_path = _config_path_from_oci_index(
                tar, members, expected_digest, expected_config_digest
            )

        cfg_bytes = _read_member_bytes(tar, members, cfg_path)
        actual_cfg_digest = _sha256_digest(cfg_bytes)
        if actual_cfg_digest != expected_config_digest:
            raise RuntimeError(
                f"Config digest mismatch: expected {expected_config_digest}, "
                f"got {actual_cfg_digest}"
            )

    try:
        cfg_obj = json.loads(cfg_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RuntimeError("Invalid config JSON in image tarball") from err

    diff_ids = (
        (cfg_obj.get("rootfs") or {}).get("diff_ids")
        if isinstance(cfg_obj, dict)
        else None
    )
    if not isinstance(diff_ids, list) or not diff_ids or not all(
        isinstance(item, str) and item.startswith("sha256:") for item in diff_ids
    ):
        raise RuntimeError("Image config lacks valid rootfs.diff_ids")
    return diff_ids


def main(argv: list[str] | None = None) -> int:
    """Verify the cached image tarball and print compact rootfs diff_ids JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tar", required=True, help="Path to saved image tarball")
    parser.add_argument(
        "--config-digest", required=True, help="Expected sha256 config digest"
    )
    parser.add_argument(
        "--digest", default="", help="Optional expected sha256 manifest digest"
    )
    args = parser.parse_args(argv)
    try:
        diff_ids = verify_tarball(
            args.tar, args.config_digest, expected_digest=args.digest
        )
    except (OSError, RuntimeError, ValueError, tarfile.TarError) as err:
        print(f"[WARN] Cached image verification failed: {err}", file=sys.stderr)
        return 1
    print(json.dumps(diff_ids, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
