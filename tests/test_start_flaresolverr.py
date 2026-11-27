"""Unit tests for the FlareSolverr cached image tarball verifier."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "start_flaresolverr_verify",
    REPO_ROOT / ".github/actions/start-flaresolverr/verify_cache_tar.py",
)
VERIFY = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(VERIFY)


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _config_bytes(diff_ids):
    return json.dumps(
        {"architecture": "amd64", "rootfs": {"type": "layers", "diff_ids": diff_ids}}
    ).encode("utf-8")


def test_verify_tarball_accepts_docker_manifest_tar(tmp_path):
    """Classic and containerd docker-save archives with manifest.json verify cleanly."""
    layers = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
    cfg_data = _config_bytes(layers)
    cfg_digest = _sha256(cfg_data)
    cfg_hex = cfg_digest.split(":", 1)[1]

    tar_path = tmp_path / "image.tar"
    with tarfile.open(tar_path, "w") as tar:
        _add_bytes(tar, f"blobs/sha256/{cfg_hex}", cfg_data)
        manifest = [{"Config": f"blobs/sha256/{cfg_hex}", "RepoTags": ["flaresolverr:cache"]}]
        _add_bytes(tar, "manifest.json", json.dumps(manifest).encode("utf-8"))

    assert VERIFY.verify_tarball(str(tar_path), cfg_digest) == layers


def test_verify_tarball_accepts_pure_oci_tar(tmp_path):
    """Pure OCI archives without manifest.json resolve through index.json."""
    layers = ["sha256:" + "c" * 64]
    cfg_data = _config_bytes(layers)
    cfg_digest = _sha256(cfg_data)
    manifest_data = json.dumps({"config": {"digest": cfg_digest}}).encode("utf-8")
    manifest_digest = _sha256(manifest_data)
    index_data = json.dumps({"manifests": [{"digest": manifest_digest}]}).encode("utf-8")

    tar_path = tmp_path / "oci.tar"
    with tarfile.open(tar_path, "w") as tar:
        _add_bytes(tar, f"blobs/sha256/{cfg_digest.split(':', 1)[1]}", cfg_data)
        _add_bytes(tar, f"blobs/sha256/{manifest_digest.split(':', 1)[1]}", manifest_data)
        _add_bytes(tar, "index.json", index_data)

    assert VERIFY.verify_tarball(str(tar_path), cfg_digest, manifest_digest) == layers


def test_verify_tarball_rejects_tampered_config_and_cli_exits_nonzero(tmp_path, capsys):
    """A config blob whose hash mismatches CONFIG_DIGEST is rejected."""
    layers = ["sha256:" + "d" * 64]
    cfg_data = _config_bytes(layers)
    wrong_digest = "sha256:" + "0" * 64

    tar_path = tmp_path / "bad.tar"
    with tarfile.open(tar_path, "w") as tar:
        _add_bytes(tar, "cfg.json", cfg_data)
        _add_bytes(
            tar,
            "manifest.json",
            json.dumps([{"Config": "cfg.json"}]).encode("utf-8"),
        )

    with pytest.raises(RuntimeError):
        VERIFY.verify_tarball(str(tar_path), wrong_digest)

    assert VERIFY.main(["--tar", str(tar_path), "--config-digest", wrong_digest]) == 1
    assert "Config digest mismatch" in capsys.readouterr().err
