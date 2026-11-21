"""Fetch original app artifacts into the Builder HuggingFace source vault.

This is a manual, ecosystem-driven sync job. It reads only apps enabled in
``config/apps-<ecosystem>.toml``, resolves their target version from
``ecosystem/<ecosystem>.json``, downloads the original Play/APKMirror artifact,
verifies the embedded versionName, and uploads it under the exact filename that
Tier 0 expects.

Nothing here patches, signs, renames a mismatched download, or deletes remote
files. The vault is a separate dataset from the patched-output uploads in
``hf_upload.py``.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import tomllib
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from core.apk_manifest import read_version_name
from core.context import Context, RateLimiter, vault_filename
from core.utils import _safe_filename, get_scraper, verify_file_hash

try:
    from huggingface_hub import (
        CommitOperationAdd as _CommitOperationAdd,
        HfApi as _HfApi,
        hf_hub_download as _hf_hub_download,
    )
    HF_AVAILABLE = True
except ImportError:  # pragma: no cover - local download-only installs
    _CommitOperationAdd = None
    _HfApi = None
    _hf_hub_download = None
    HF_AVAILABLE = False


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_HF_USER = "chihafuyu"
DEFAULT_HF_DATASET_ENV = "HF_SOURCE_DATASET_ID"
MANIFEST_FILENAME = "vault_manifest.json"
ARTIFACT_EXTENSIONS = (".apk", ".xapk", ".apkm", ".apks")
ECOSYSTEM_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
PACKAGE_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+\Z")
HF_REPO_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)
UPLOAD_BATCH_FILES = 10
UPLOAD_BATCH_BYTES = 1536 * 1024 * 1024
DEFAULT_MAX_RUNTIME_SECONDS = 19800
FLARESOLVERR_URL = "http://localhost:8191/"
SOURCE_URL_SAFE_QUERY_KEYS = frozenset({"id", "package", "app", "hl", "gl"})


class ConfigError(RuntimeError):
    """Raised when ecosystem or app configuration cannot be used safely."""


class VaultContext(Context):
    """Context that writes scraper output under canonical vault filenames."""

    def get_out_path(self, ext: str) -> str:
        """Returns the canonical vault path for a downloaded artifact."""
        return os.path.join(
            self.out_dir, vault_filename(self.pkg, self.target_ver, ext)
        )


@dataclass
class ArtifactPlan:
    """One unique artifact that should exist in the vault."""

    repo: str
    package: str
    version_raw: str
    version_name: str
    filename_stem: str
    app_key: str
    ecosystem: str
    app_data: dict[str, Any] = field(default_factory=dict)
    related: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """Human-readable ecosystem/app label for logs and summaries."""
        return f"{self.ecosystem}/{self.app_key}"


@dataclass
class FetchOutcome:
    """Result of one scraper attempt."""

    path: str | None = None
    source: str = ""
    source_url: str = ""
    actual_version: str = ""
    reason: str = ""
    transient: bool = False
    scanner_verified: bool = False


@dataclass
class PendingUpload:
    """A staged artifact waiting for a batched HuggingFace commit."""

    path: str
    filename: str
    repo: str
    metadata: dict[str, Any]
    result_index: int


def _utc_now() -> str:
    """Returns an ISO-8601 UTC timestamp without microseconds."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_custom_versions(raw: str) -> dict[str, str]:
    """Parses ``app=version`` or ``_global=version`` pins from a CLI value."""
    values: dict[str, str] = {}
    text = (raw or "").strip()
    if not text:
        return values

    if "=" not in text:
        values["_global"] = text
        return values

    for pair in text.split(","):
        key, separator, value = (part.strip() for part in pair.partition("="))
        if not separator or not key or not value:
            if pair.strip():
                print(f"[WARN] Ignoring malformed custom version: {pair.strip()!r}")
            continue
        values[key] = value
        values[key.casefold()] = value
    return values


def _custom_version_for(
    pins: dict[str, str], ecosystem: str, app_key: str
) -> str:
    """Returns the most specific custom version pin for an app."""
    keys = (
        f"{ecosystem}/{app_key}",
        f"{ecosystem}/{app_key}".casefold(),
        app_key,
        app_key.casefold(),
        "_global",
    )
    for key in keys:
        value = pins.get(key)
        if value:
            return value
    return ""


def _normalize_version(value: str) -> str:
    """Normalizes a version for equality checks, not for filenames."""
    text = value.strip()
    if len(text) > 1 and text[0] in "vV" and text[1].isdigit():
        text = text[1:]
    return text.casefold()


_VERSION_PREFIX_RE = re.compile(r"(\d+(?:\.\d+)+)")
_BUILD_SUFFIX_RE = re.compile(r"^(?P<base>.*\d)-(?P<build>\d{4,})$")


def _clean_version(value: str) -> str:
    """Normalizes a version and drops channel labels such as ``:RELEASE``."""
    return _normalize_version(value).split(":", 1)[0].strip()


def _split_build(value: str) -> tuple[str, str]:
    """Splits a trailing numeric build suffix (``1.2.3-456789``)."""
    match = _BUILD_SUFFIX_RE.fullmatch(value)
    if not match:
        return value, ""
    return match.group("base"), match.group("build")


def _numeric_prefix(value: str) -> str:
    """Returns the leading dotted version number, if the string starts with one."""
    match = _VERSION_PREFIX_RE.match(value)
    return match.group(1) if match else ""


def versions_match(actual: str | None, targets: list[str]) -> bool:
    """Returns True when a manifest version matches any target representation."""
    if not actual:
        return False
    actual_clean = _clean_version(actual)
    if not actual_clean:
        return False

    for target in targets:
        target_clean = _clean_version(target)
        if not target_clean:
            continue
        if target_clean == actual_clean:
            return True
        # A slash version (for example Teams' ``1416/1.0.0...``) must only be
        # accepted as an exact match; its tail alone identifies another build.
        if "/" in target_clean or "\\" in target_clean:
            continue

        target_base, target_build = _split_build(target_clean)
        if target_build:
            # The manifest may omit an explicit build suffix, but it must
            # never accept a different build number.
            if target_base == actual_clean:
                return True
            continue

        # Descriptors such as "- Stable" or "build 25028" are not part of
        # the embedded versionName, so compare its leading numeric run.
        if _numeric_prefix(target_clean) == actual_clean:
            return True
    return False


def _first_version_value(value: Any) -> str | None:
    """Returns the first version in a stable/beta list, mirroring Builder."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return None
    if not values:
        return None
    first = values[0]
    if not isinstance(first, str):
        return None
    return first.strip()


def _resolve_version(
    app_data: dict[str, Any],
    selection: str,
    custom_version: str,
) -> tuple[str, str] | None:
    """Resolves the target version, mirroring Builder's version selection."""
    raw = custom_version.strip() if custom_version else ""

    if not raw and selection == "beta":
        beta = _first_version_value(app_data.get("beta"))
        if beta:
            raw = beta
    if not raw:
        stable = _first_version_value(app_data.get("stable"))
        if stable:
            raw = stable

    if not raw or raw.lower() == "any":
        return None

    # Builder replaces spaces before using the version in filenames/logs.
    version_name = raw.replace(" ", "_")
    if not version_name:
        return None
    return raw, version_name


def _load_enabled_apps(toml_path: Path) -> list[str]:
    """Returns the app keys enabled in one TOML file."""
    try:
        with open(toml_path, "rb") as toml_file:
            data = tomllib.load(toml_file)
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise ConfigError(f"cannot read {toml_path.name}: {err}") from err

    apps = data.get("apps")
    if not isinstance(apps, dict):
        raise ConfigError(f"{toml_path.name} has no [apps] table")

    enabled = []
    for key, value in apps.items():
        if isinstance(value, bool):
            if value:
                enabled.append(str(key))
        else:
            print(f"[WARN] {toml_path.name}: ignoring non-boolean entry {key!r}")
    return sorted(enabled)


def _load_ecosystem(
    repo_root: Path, ecosystem: str
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Loads enabled TOML apps and their ecosystem JSON definitions."""
    toml_path = repo_root / "config" / f"apps-{ecosystem}.toml"
    json_path = repo_root / "ecosystem" / f"{ecosystem}.json"
    if not toml_path.is_file():
        raise ConfigError(f"missing app toggle file: config/{toml_path.name}")
    if not json_path.is_file():
        raise ConfigError(f"missing ecosystem config: ecosystem/{json_path.name}")

    enabled = _load_enabled_apps(toml_path)
    try:
        with open(json_path, "r", encoding="utf-8") as json_file:
            root_data = json.load(json_file)
    except (OSError, json.JSONDecodeError) as err:
        raise ConfigError(f"cannot read ecosystem/{json_path.name}: {err}") from err

    if not isinstance(root_data, dict):
        raise ConfigError(f"ecosystem/{json_path.name} must contain an object")
    ecosystem_data = root_data.get(ecosystem)
    if not isinstance(ecosystem_data, dict):
        raise ConfigError(
            f"root key {ecosystem!r} missing from ecosystem/{json_path.name}"
        )
    apps = ecosystem_data.get("apps")
    if not isinstance(apps, dict):
        raise ConfigError(f"{json_path.name} has no valid 'apps' object")
    return enabled, apps


def _resolve_ecosystems(repo_root: Path, raw: str) -> list[str]:
    """Resolves a comma-separated ecosystem list, including the ``all`` token."""
    available: dict[str, str] = {}
    for path in sorted((repo_root / "config").glob("apps-*.toml")):
        name = path.name[len("apps-") : -len(".toml")]
        if not ECOSYSTEM_NAME_RE.fullmatch(name):
            print(f"[WARN] Ignoring unsafe app config name: {path.name!r}")
            continue
        available[name.casefold()] = name

    requested = [part.strip() for part in (raw or "").split(",") if part.strip()]
    if not requested or any(part.casefold() == "all" for part in requested):
        if not available:
            raise ConfigError("no config/apps-*.toml files were found")
        return sorted(available.values())

    resolved = []
    for name in requested:
        exact = available.get(name.casefold())
        if not exact:
            raise ConfigError(f"unknown ecosystem {name!r}")
        if exact not in resolved:
            resolved.append(exact)
    return resolved


def _matches_requested_app(
    ecosystem: str, app_key: str, requested: list[str]
) -> bool:
    """Returns True when the app is selected by the ``--apps`` filter."""
    for token in requested:
        token = token.strip()
        if token.casefold() == "all":
            return True
        if "/" in token:
            eco_token, app_token = token.split("/", 1)
            if (
                eco_token.casefold() == ecosystem.casefold()
                and app_token.casefold() == app_key.casefold()
            ):
                return True
        elif token.casefold() == app_key.casefold():
            return True
    return False


def _matched_tokens(
    ecosystem: str, app_key: str, requested: list[str]
) -> set[str]:
    """Returns the requested tokens matched by one ecosystem/app pair."""
    matched = set()
    for token in requested:
        token = token.strip()
        if token.casefold() == "all":
            continue
        if "/" in token:
            eco_token, app_token = token.split("/", 1)
            if (
                eco_token.casefold() == ecosystem.casefold()
                and app_token.casefold() == app_key.casefold()
            ):
                matched.add(token.casefold())
        elif token.casefold() == app_key.casefold():
            matched.add(token.casefold())
    return matched


def _resolve_repo(app_data: dict[str, Any], override: str) -> str:
    """Resolves the per-app HuggingFace dataset repository."""
    explicit = override.strip() if override else ""
    if not explicit:
        explicit = str(app_data.get("hf_repo") or "").strip()

    if explicit:
        repo = explicit
    else:
        archive_id = str(app_data.get("archive_id") or "").strip()
        if not archive_id:
            raise ConfigError("missing archive_id/hf_repo")
        hf_user = str(app_data.get("hf_user") or DEFAULT_HF_USER).strip()
        repo = f"{hf_user}/{archive_id}"

    if not HF_REPO_RE.fullmatch(repo):
        raise ConfigError(f"invalid HuggingFace dataset id {repo!r}")
    return repo


def _normalized_app_data(
    app_data: dict[str, Any], package: str
) -> dict[str, Any]:
    """Returns a scraper-safe shallow copy of one ecosystem app entry."""
    cleaned = dict(app_data)
    cleaned["package"] = package
    cleaned["force_bundle"] = bool(app_data.get("force_bundle"))

    for list_key in ("apkm_include", "apkm_exclude"):
        value = cleaned.get(list_key)
        if value is not None and not isinstance(value, list):
            print(f"[WARN] {package}: ignoring non-list {list_key}")
            cleaned.pop(list_key, None)

    for text_key in ("search_term", "force_arch"):
        value = cleaned.get(text_key)
        if value is not None and not isinstance(value, str):
            print(f"[WARN] {package}: ignoring non-string {text_key}")
            cleaned.pop(text_key, None)

    return cleaned


def _build_plans(  # pylint: disable=too-many-positional-arguments
    repo_root: Path,
    ecosystems: list[str],
    requested_apps: list[str],
    selection: str,
    custom_pins: dict[str, str],
    hf_override: str,
) -> tuple[list[ArtifactPlan], list[dict[str, Any]], set[str]]:
    """Builds de-duplicated artifact plans and configuration errors."""
    plans: dict[tuple[str, str, str], ArtifactPlan] = {}
    errors: list[dict[str, Any]] = []
    matched_tokens: set[str] = set()

    for ecosystem in ecosystems:
        try:
            enabled, apps = _load_ecosystem(repo_root, ecosystem)
        except ConfigError as err:
            errors.append({"ecosystem": ecosystem, "reason": str(err)})
            continue

        for app_key in enabled:
            if not _matches_requested_app(ecosystem, app_key, requested_apps):
                continue
            matched_tokens.update(
                _matched_tokens(ecosystem, app_key, requested_apps)
            )

            app_data = apps.get(app_key)
            if not isinstance(app_data, dict):
                errors.append({
                    "ecosystem": ecosystem,
                    "app": app_key,
                    "reason": "app entry is not an object",
                })
                continue

            package = str(app_data.get("package") or "").strip()
            if not PACKAGE_RE.fullmatch(package):
                errors.append({
                    "ecosystem": ecosystem,
                    "app": app_key,
                    "package": package,
                    "reason": "invalid or missing package name",
                })
                continue

            custom_version = _custom_version_for(
                custom_pins, ecosystem, app_key
            )
            version = _resolve_version(app_data, selection, custom_version)
            if version is None:
                errors.append({
                    "ecosystem": ecosystem,
                    "app": app_key,
                    "package": package,
                    "reason": "no concrete target version",
                })
                continue
            version_raw, version_name = version

            try:
                repo = _resolve_repo(app_data, hf_override)
            except ConfigError as err:
                errors.append({
                    "ecosystem": ecosystem,
                    "app": app_key,
                    "package": package,
                    "reason": str(err),
                })
                continue

            cleaned = _normalized_app_data(app_data, package)
            stem = vault_filename(package, version_name, "")
            key = (repo.casefold(), package.casefold(), version_raw.casefold())
            label = f"{ecosystem}/{app_key}"

            existing = plans.get(key)
            if existing is None:
                plans[key] = ArtifactPlan(
                    repo=repo,
                    package=package,
                    version_raw=version_raw,
                    version_name=version_name,
                    filename_stem=stem,
                    app_key=app_key,
                    ecosystem=ecosystem,
                    app_data=cleaned,
                    related=[label],
                )
            else:
                if label not in existing.related:
                    existing.related.append(label)
                if (
                    bool(cleaned.get("force_bundle"))
                    and not bool(existing.app_data.get("force_bundle"))
                ):
                    existing.app_data = cleaned
                    existing.app_key = app_key
                    existing.ecosystem = ecosystem

    ordered = sorted(
        plans.values(),
        key=lambda item: (
            item.repo.casefold(),
            item.package.casefold(),
            item.version_name.casefold(),
            item.app_key.casefold(),
        ),
    )

    unique_plans: list[ArtifactPlan] = []
    seen_stems: dict[tuple[str, str], ArtifactPlan] = {}
    for plan in ordered:
        collision_key = (plan.repo.casefold(), plan.filename_stem.casefold())
        previous = seen_stems.get(collision_key)
        if previous is not None:
            errors.append({
                "ecosystem": plan.ecosystem,
                "app": plan.app_key,
                "package": plan.package,
                "version": plan.version_raw,
                "reason": (
                    "vault filename collision with "
                    f"{previous.ecosystem}/{previous.app_key} "
                    f"(version {previous.version_raw!r})"
                ),
            })
            continue
        seen_stems[collision_key] = plan
        unique_plans.append(plan)

    return unique_plans, errors, matched_tokens


def _sanitize_source_url(url: str) -> str:
    """Drops signed query parameters from provenance URLs before logging."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url.split("?", 1)[0].split("#", 1)[0]

    if not parsed.query:
        return parsed._replace(fragment="").geturl()

    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() in SOURCE_URL_SAFE_QUERY_KEYS
    ]
    return urlunparse(parsed._replace(query=urlencode(kept), fragment=""))


def _remove_quietly(path: str | None) -> None:
    """Removes a file without letting cleanup errors mask the real result."""
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as err:
        print(f"[WARN] Could not remove {path}: {err}")


def _flaresolverr_available() -> bool:
    """Checks that the APKMirror WAF solver is reachable."""
    try:
        response = requests.get(FLARESOLVERR_URL, timeout=5)
        return response.status_code < 500
    except requests.exceptions.RequestException:
        return False


def _download_play(
    plan: ArtifactPlan, work_dir: str, limiter: RateLimiter, arch: str
) -> FetchOutcome:
    """Downloads the current Play artifact and verifies its versionName."""
    email = os.getenv("PLAY_EMAIL", "")
    token = os.getenv("PLAY_AAS_TOKEN", "")
    if not email or not token:
        return FetchOutcome(
            reason="missing PLAY_EMAIL/PLAY_AAS_TOKEN",
            transient=False,
        )

    from scrapers.tier6_google_play import GooglePlayScraper  # pylint: disable=import-outside-toplevel

    session = get_scraper()
    ctx = VaultContext(
        scraper=session,
        app_data=dict(plan.app_data),
        target_ver=plan.version_name,
        arch=arch,
        out_dir=work_dir,
        limiter=limiter,
    )
    try:
        with GooglePlayScraper() as scraper:
            raw_path = scraper.scrape(ctx)
        if not raw_path:
            return FetchOutcome(reason="Play returned no artifact", transient=True)

        if not zipfile.is_zipfile(raw_path):
            _remove_quietly(raw_path)
            return FetchOutcome(
                reason="Play artifact is not a ZIP/APK container",
                transient=True,
            )

        actual = read_version_name(raw_path)
        if not actual:
            _remove_quietly(raw_path)
            return FetchOutcome(
                reason="Play artifact versionName could not be verified",
                transient=False,
            )
        if not versions_match(
            actual, [plan.version_raw, plan.version_name]
        ):
            print(
                f"[WARN] {plan.label}: Play returned {actual!r}, "
                f"expected {plan.version_raw!r}; discarding."
            )
            _remove_quietly(raw_path)
            return FetchOutcome(
                reason=f"Play version mismatch ({actual})",
                transient=False,
                actual_version=actual,
            )

        print(
            f"[INFO] {plan.label}: Play versionName {actual!r} verified."
        )
        return FetchOutcome(
            path=raw_path,
            source="google_play",
            source_url=ctx.source_url,
            actual_version=actual,
        )
    finally:
        session.close()


def _download_apkmirror(
    plan: ArtifactPlan, work_dir: str, limiter: RateLimiter, arch: str
) -> FetchOutcome:
    """Downloads the pinned target version from APKMirror."""
    from scrapers.tier1_apkmirror import ApkmirrorScraper  # pylint: disable=import-outside-toplevel

    session = get_scraper()
    ctx = VaultContext(
        scraper=session,
        app_data=dict(plan.app_data),
        target_ver=plan.version_name,
        arch=arch,
        out_dir=work_dir,
        limiter=limiter,
    )
    try:
        with ApkmirrorScraper() as scraper:
            raw_path = scraper.scrape(ctx)
        if not raw_path:
            return FetchOutcome(
                reason="APKMirror returned no artifact", transient=True
            )

        if not zipfile.is_zipfile(raw_path):
            _remove_quietly(raw_path)
            return FetchOutcome(
                reason="APKMirror artifact is not a ZIP/APK container",
                transient=True,
            )

        actual = read_version_name(raw_path)
        if not actual:
            _remove_quietly(raw_path)
            return FetchOutcome(
                reason="APKMirror artifact versionName could not be verified",
                transient=False,
            )
        if not versions_match(
            actual, [plan.version_raw, plan.version_name]
        ):
            print(
                f"[WARN] {plan.label}: APKMirror returned {actual!r}, "
                f"expected {plan.version_raw!r}; discarding."
            )
            _remove_quietly(raw_path)
            return FetchOutcome(
                reason=f"APKMirror version mismatch ({actual})",
                transient=False,
                actual_version=actual,
            )

        print(
            f"[INFO] {plan.label}: APKMirror versionName {actual!r} verified."
        )
        return FetchOutcome(
            path=raw_path,
            source="apkmirror",
            source_url=ctx.source_url,
            actual_version=actual,
            # download_file_stream already ran the malware cascade before
            # publishing this file; avoid spending scanner quota twice.
            scanner_verified=True,
        )
    finally:
        session.close()


def _attempt_with_retries(  # pylint: disable=too-many-positional-arguments
    downloader: Any,
    plan: ArtifactPlan,
    work_dir: str,
    limiter: RateLimiter,
    arch: str,
    retries: int,
) -> FetchOutcome:
    """Runs one downloader, retrying only transient failures."""
    outcome = FetchOutcome(reason="not attempted")
    for attempt in range(retries + 1):
        outcome = downloader(plan, work_dir, limiter, arch)
        if outcome.path or not outcome.transient:
            return outcome
        if attempt < retries:
            delay = 10 * (attempt + 1)
            print(
                f"[WARN] {plan.label}: {outcome.reason}; "
                f"retrying in {delay}s."
            )
            time.sleep(delay)
    return outcome


def _fetch_artifact(  # pylint: disable=too-many-positional-arguments
    plan: ArtifactPlan,
    work_dir: str,
    limiter: RateLimiter,
    arch: str,
    source: str,
    retries: int,
    apkmirror_available: bool,
) -> FetchOutcome:
    """Runs the configured source chain without ever accepting a mismatch."""
    reasons: list[str] = []

    if source in ("auto", "google_play"):
        outcome = _attempt_with_retries(
            _download_play, plan, work_dir, limiter, arch, retries
        )
        if outcome.path:
            return outcome
        reasons.append(outcome.reason)
        if source == "google_play":
            return FetchOutcome(reason="; ".join(reasons), transient=False)

    if source in ("auto", "apkmirror"):
        if not apkmirror_available:
            reasons.append("FlareSolverr is unavailable")
        else:
            outcome = _attempt_with_retries(
                _download_apkmirror, plan, work_dir, limiter, arch, retries
            )
            if outcome.path:
                return outcome
            reasons.append(outcome.reason)

    return FetchOutcome(
        reason="; ".join(reason for reason in reasons if reason)
        or "no download source available",
        transient=False,
    )


def _delivery_extension(path: str) -> str:
    """Returns the canonical vault extension for a downloaded artifact."""
    extension = os.path.splitext(path)[1].lower()
    if extension == ".zip":
        # apkeep can emit a plain ZIP of split APKs. Builder's vault uses the
        # .apks label for that container; the bytes themselves are unchanged.
        try:
            with zipfile.ZipFile(path, "r") as archive:
                has_apk = any(
                    name.lower().endswith(".apk")
                    for name in archive.namelist()
                )
        except (OSError, zipfile.BadZipFile) as err:
            raise ConfigError(f"unreadable ZIP container: {err}") from err
        if not has_apk:
            raise ConfigError(
                "ZIP artifact contains no APK files; refusing to relabel it"
            )
        return ".apks"
    if extension not in ARTIFACT_EXTENSIONS:
        raise ConfigError(
            f"unsupported artifact extension {extension!r}; "
            f"expected one of {', '.join(ARTIFACT_EXTENSIONS)}"
        )
    return extension


def _stage_artifact(plan: ArtifactPlan, path: str, out_dir: Path) -> str:
    """Moves a verified download to the canonical vault filename."""
    extension = _delivery_extension(path)

    destination = out_dir / f"{plan.filename_stem}{extension}"
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
        os.path.abspath(destination)
    ):
        return str(destination)

    if destination.exists():
        destination.unlink()
    shutil.move(path, destination)
    return str(destination)


def _sha256(path: str) -> str:
    """Returns the lowercase SHA-256 digest of a file."""
    with open(path, "rb") as file_obj:
        return hashlib.file_digest(file_obj, "sha256").hexdigest()


class VaultPublisher:
    """Batched, non-destructive HuggingFace dataset publisher."""

    def __init__(self, token: str, enabled: bool, keep_files: bool, out_dir: Path):
        self.token = token
        self.enabled = enabled and bool(token)
        self.keep_files = keep_files
        self.out_dir = out_dir
        self._api: Any = None
        self._inventory: dict[str, set[str]] = {}
        self._manifests: dict[str, dict[str, Any]] = {}
        self._manifest_loaded: dict[str, bool] = {}
        self._dirty_repos: set[str] = set()
        self._prepared: set[str] = set()

    @property
    def active(self) -> bool:
        """True when remote existence checks and uploads are possible."""
        return self.enabled

    def _get_api(self) -> Any:
        """Lazily creates the HuggingFace API client."""
        if self._api is None:
            if not HF_AVAILABLE:
                raise ConfigError("huggingface_hub is not installed")
            self._api = _HfApi(token=self.token)
        return self._api

    def prepare(self, repo: str) -> bool:
        """Creates the dataset if needed and loads its file inventory."""
        if not self.active or repo in self._prepared:
            return True

        api = self._get_api()
        try:
            if not api.repo_exists(repo_id=repo, repo_type="dataset"):
                print(f"[INFO] Creating HuggingFace dataset: {repo}")
                api.create_repo(
                    repo_id=repo, repo_type="dataset", private=False
                )
            files = set(
                api.list_repo_files(repo_id=repo, repo_type="dataset")
            )
        except Exception as err:
            print(f"[ERROR] Cannot prepare vault dataset {repo}: {err}")
            return False

        self._prepared.add(repo)
        self._inventory[repo] = files
        self._load_manifest(repo, files)
        return True

    def _load_manifest(self, repo: str, files: set[str]) -> None:
        """Loads an existing manifest so this run never drops old entries."""
        manifest: dict[str, Any] = {
            "schema": 1,
            "repo": repo,
            "artifacts": {},
        }
        loaded = True
        if MANIFEST_FILENAME in files:
            try:
                local_path = _hf_hub_download(
                    repo_id=repo,
                    filename=MANIFEST_FILENAME,
                    repo_type="dataset",
                    token=self.token,
                    force_download=True,
                )
                with open(local_path, "r", encoding="utf-8") as manifest_file:
                    existing = json.load(manifest_file)
                if (
                    isinstance(existing, dict)
                    and isinstance(existing.get("artifacts"), dict)
                ):
                    manifest = existing
                    manifest.setdefault("schema", 1)
                    manifest["repo"] = repo
                else:
                    loaded = False
                    print(
                        f"[WARN] {repo}: existing {MANIFEST_FILENAME} "
                        "has an unexpected shape; it will not be overwritten."
                    )
            except Exception as err:
                loaded = False
                print(
                    f"[WARN] {repo}: could not load {MANIFEST_FILENAME}: {err}. "
                    "It will not be overwritten."
                )

        self._manifests[repo] = manifest
        self._manifest_loaded[repo] = loaded

    def existing(self, repo: str, filename_stem: str) -> str | None:
        """Returns the existing vault filename for any accepted extension."""
        if not self.active:
            return None
        files = self._inventory.get(repo, set())
        for extension in ARTIFACT_EXTENSIONS:
            candidate = f"{filename_stem}{extension}"
            if candidate in files:
                return candidate
        return None

    def publish(
        self, repo: str, records: list[PendingUpload]
    ) -> set[str]:
        """Uploads a batch, falling back to per-file commits on batch failure."""
        if not self.active or not records:
            return set()

        names = [record.filename for record in records]
        operations = [
            _CommitOperationAdd(
                path_in_repo=record.filename,
                path_or_fileobj=record.path,
            )
            for record in records
        ]
        api = self._get_api()

        try:
            api.create_commit(
                repo_id=repo,
                operations=operations,
                commit_message=(
                    f"chore(vault): add {len(records)} original artifact(s)"
                ),
                repo_type="dataset",
            )
            uploaded = set(names)
        except Exception as err:
            print(
                f"[WARN] {repo}: batch upload failed ({err}); "
                "retrying each file individually."
            )
            uploaded = set()
            for record in records:
                try:
                    api.upload_file(
                        path_or_fileobj=record.path,
                        path_in_repo=record.filename,
                        repo_id=repo,
                        repo_type="dataset",
                        commit_message=f"chore(vault): add {record.filename}",
                    )
                    uploaded.add(record.filename)
                except Exception as file_err:
                    print(
                        f"[ERROR] {repo}: failed to upload "
                        f"{record.filename}: {file_err}"
                    )

        if uploaded:
            self._inventory.setdefault(repo, set()).update(uploaded)
            manifest = self._manifests.setdefault(
                repo, {"schema": 1, "repo": repo, "artifacts": {}}
            )
            artifacts = manifest.setdefault("artifacts", {})
            for record in records:
                if record.filename in uploaded:
                    artifacts[record.filename] = record.metadata
            self._dirty_repos.add(repo)
        return uploaded

    def flush_manifests(self) -> None:
        """Writes and uploads the merged manifest for every changed repo."""
        if not self.active:
            return

        manifest_dir = self.out_dir / "manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)

        for repo in sorted(self._dirty_repos):
            if not self._manifest_loaded.get(repo, True):
                print(
                    f"[WARN] {repo}: skipping manifest update because the "
                    "remote manifest could not be loaded safely."
                )
                continue

            manifest = self._manifests.get(repo)
            if not manifest:
                continue
            manifest["updated_at"] = _utc_now()
            local_name = f"{_safe_filename(repo.replace('/', '__'))}.json"
            local_path = manifest_dir / local_name
            try:
                with open(local_path, "w", encoding="utf-8") as manifest_file:
                    json.dump(manifest, manifest_file, indent=2, ensure_ascii=False)
                    manifest_file.write("\n")
                self._get_api().upload_file(
                    path_or_fileobj=str(local_path),
                    path_in_repo=MANIFEST_FILENAME,
                    repo_id=repo,
                    repo_type="dataset",
                    commit_message="chore(vault): update manifest",
                )
                print(f"[INFO] Updated {repo}/{MANIFEST_FILENAME}")
            except Exception as err:
                print(f"[ERROR] {repo}: manifest upload failed: {err}")


def _result_for_plan(plan: ArtifactPlan, **extra: Any) -> dict[str, Any]:
    """Builds one summary result entry for an artifact plan."""
    result = {
        "ecosystem": plan.ecosystem,
        "app": plan.app_key,
        "apps": list(plan.related),
        "package": plan.package,
        "version": plan.version_raw,
        "repo": plan.repo,
    }
    result.update(extra)
    return result


def _metadata_for(  # pylint: disable=too-many-positional-arguments
    plan: ArtifactPlan,
    filename: str,
    outcome: FetchOutcome,
    sha256: str,
    size: int,
    arch: str,
) -> dict[str, Any]:
    """Builds the manifest entry stored next to the vault artifact."""
    return {
        "package": plan.package,
        "version": plan.version_raw,
        "version_name": outcome.actual_version,
        "filename": filename,
        "sha256": sha256,
        "size": size,
        "source": outcome.source,
        "source_url": _sanitize_source_url(outcome.source_url),
        "apps": list(plan.related),
        "arch": arch,
        "force_bundle": bool(plan.app_data.get("force_bundle")),
        "downloaded_at": _utc_now(),
    }


def _process_plan(  # pylint: disable=too-many-positional-arguments
    plan: ArtifactPlan,
    args: argparse.Namespace,
    out_dir: Path,
    limiter: RateLimiter,
    publisher: VaultPublisher,
    apkmirror_available: bool,
    result_index: int,
) -> tuple[dict[str, Any], PendingUpload | None]:
    """Downloads, verifies, and stages one plan; upload stays batched."""
    print(
        f"[INFO] {plan.label} -> {plan.repo} "
        f"({plan.package} {plan.version_raw})"
    )
    work_dir = tempfile.mkdtemp(prefix=".work-", dir=str(out_dir))
    try:
        outcome = _fetch_artifact(
            plan=plan,
            work_dir=work_dir,
            limiter=limiter,
            arch=args.arch,
            source=args.download_source,
            retries=args.retries,
            apkmirror_available=apkmirror_available,
        )
        if not outcome.path:
            print(f"[ERROR] {plan.label}: {outcome.reason}")
            return (
                _result_for_plan(
                    plan,
                    status="failed",
                    reason=outcome.reason,
                ),
                None,
            )

        try:
            staged = _stage_artifact(plan, outcome.path, out_dir)
        except (ConfigError, OSError) as err:
            _remove_quietly(outcome.path)
            return (
                _result_for_plan(plan, status="failed", reason=str(err)),
                None,
            )

        if not outcome.scanner_verified and not verify_file_hash(staged):
            _remove_quietly(staged)
            return (
                _result_for_plan(
                    plan,
                    status="failed",
                    reason="rejected by malware scanners",
                ),
                None,
            )

        try:
            size = os.path.getsize(staged)
            sha256 = _sha256(staged)
        except OSError as err:
            _remove_quietly(staged)
            return (
                _result_for_plan(plan, status="failed", reason=str(err)),
                None,
            )

        filename = os.path.basename(staged)
        result = _result_for_plan(
            plan,
            status="downloaded",
            filename=filename,
            source=outcome.source,
            source_url=_sanitize_source_url(outcome.source_url),
            version_name=outcome.actual_version,
            sha256=sha256,
            size=size,
        )

        if not publisher.active:
            result["kept"] = bool(args.keep_files)
            if args.keep_files:
                print(
                    f"[INFO] {plan.label}: staged {filename} (upload disabled, "
                    "kept)"
                )
            else:
                print(
                    f"[INFO] {plan.label}: verified {filename} "
                    "(upload disabled, removed)"
                )
                _remove_quietly(staged)
            return result, None

        pending = PendingUpload(
            path=staged,
            filename=filename,
            repo=plan.repo,
            metadata=_metadata_for(
                plan, filename, outcome, sha256, size, args.arch
            ),
            result_index=result_index,
        )
        return result, pending
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    """Atomically writes the run summary as JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".json.tmp")
    with open(temp_path, "w", encoding="utf-8") as summary_file:
        json.dump(summary, summary_file, indent=2, ensure_ascii=False)
        summary_file.write("\n")
    os.replace(temp_path, path)


def _print_summary(summary: dict[str, Any]) -> None:
    """Prints the final counts in a stable, grep-friendly format."""
    counts = summary.get("counts", {})
    print(
        "[INFO] Vault sync finished: "
        f"planned={counts.get('planned', 0)} "
        f"attempted={counts.get('attempted', 0)} "
        f"uploaded={counts.get('uploaded', 0)} "
        f"already_present={counts.get('already_present', 0)} "
        f"downloaded={counts.get('downloaded', 0)} "
        f"failed={counts.get('failed', 0)} "
        f"skipped={counts.get('skipped', 0)}"
    )


def _config_failure(
    out_dir: Path, message: str, results: list[dict[str, Any]] | None = None
) -> int:
    """Writes a failure summary for an early configuration error."""
    summary = {
        "schema": 1,
        "generated_at": _utc_now(),
        "mode": "config_error",
        "error": message,
        "results": results or [],
        "counts": {"failed": len(results or [])},
    }
    _write_summary(out_dir / "vault-sync-summary.json", summary)
    _print_summary(summary)
    print(f"[ERROR] {message}")
    return 2


def _resolve_runtime(args: argparse.Namespace) -> tuple[str, bool, bool]:
    """Validates credentials and returns token, upload flag, and APKMirror state."""
    token = os.getenv("HF_TOKEN", "")
    upload_enabled = not args.no_upload
    if upload_enabled:
        if not HF_AVAILABLE:
            raise ConfigError(
                "huggingface_hub is not installed; cannot upload"
            )
        if not token:
            raise ConfigError(
                "HF_TOKEN is required unless --no-upload is used"
            )

    if args.download_source in ("auto", "google_play"):
        if not os.getenv("PLAY_EMAIL") or not os.getenv("PLAY_AAS_TOKEN"):
            if args.download_source == "google_play":
                raise ConfigError(
                    "PLAY_EMAIL and PLAY_AAS_TOKEN are required for "
                    "download_source=google_play"
                )
            print(
                "[WARN] Play credentials are missing; auto mode will only "
                "use APKMirror."
            )

    apkmirror_available = False
    if args.download_source in ("auto", "apkmirror"):
        apkmirror_available = _flaresolverr_available()
        if not apkmirror_available:
            if args.download_source == "apkmirror":
                raise ConfigError(
                    "FlareSolverr is required for download_source=apkmirror"
                )
            print(
                "[WARN] FlareSolverr is unavailable; APKMirror fallback is "
                "disabled."
            )
    return token, upload_enabled, apkmirror_available


def run(args: argparse.Namespace) -> int:
    """Executes the vault sync and returns a process exit code."""
    started = time.monotonic()
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    repo_root = Path(args.repo_root).resolve()

    requested_apps = [
        part.strip() for part in (args.apps or "all").split(",") if part.strip()
    ]
    if not requested_apps:
        requested_apps = ["all"]

    try:
        ecosystems = _resolve_ecosystems(repo_root, args.ecosystems)
    except ConfigError as err:
        return _config_failure(out_dir, str(err))

    custom_pins = _parse_custom_versions(args.custom_version)
    hf_override = (args.hf_dataset or "").strip()
    if not hf_override:
        hf_override = os.getenv(DEFAULT_HF_DATASET_ENV, "").strip()
    if hf_override and not HF_REPO_RE.fullmatch(hf_override):
        return _config_failure(
            out_dir, f"invalid --hf-dataset value {hf_override!r}"
        )

    plans, plan_errors, matched_tokens = _build_plans(
        repo_root=repo_root,
        ecosystems=ecosystems,
        requested_apps=requested_apps,
        selection=args.version_selection,
        custom_pins=custom_pins,
        hf_override=hf_override,
    )
    for error in plan_errors:
        error.setdefault("status", "failed")

    if "all" not in {token.casefold() for token in requested_apps}:
        missing = [
            token
            for token in requested_apps
            if token.casefold() not in matched_tokens
        ]
        if missing:
            return _config_failure(
                out_dir,
                "no enabled app matched: " + ", ".join(missing),
                plan_errors,
            )

    summary: dict[str, Any] = {
        "schema": 1,
        "generated_at": _utc_now(),
        "mode": "dry_run" if args.dry_run else (
            "download_only" if args.no_upload else "upload"
        ),
        "requested": {
            "ecosystems": ecosystems,
            "apps": requested_apps,
            "version_selection": args.version_selection,
            "download_source": args.download_source,
            "limit": args.limit,
        },
        "results": list(plan_errors),
        "counts": {},
    }

    if args.dry_run:
        if args.limit > 0:
            plans = plans[: args.limit]
        for plan in plans:
            summary["results"].append(
                _result_for_plan(
                    plan,
                    status="planned",
                    expected_filenames=[
                        f"{plan.filename_stem}{ext}"
                        for ext in ARTIFACT_EXTENSIONS
                    ],
                )
            )
            print(
                f"[PLAN] {plan.label} -> {plan.repo} "
                f"{plan.filename_stem}{{.apk,.xapk,.apkm,.apks}} "
                f"(bundle={bool(plan.app_data.get('force_bundle'))})"
            )
        summary["counts"] = {
            "planned": len(plans),
            "attempted": len(plans),
            "failed": len(plan_errors),
            "skipped": 0,
        }
        summary["duration_seconds"] = round(time.monotonic() - started, 1)
        _write_summary(out_dir / "vault-sync-summary.json", summary)
        _print_summary(summary)
        return 0 if plans else 1

    try:
        token, upload_enabled, apkmirror_available = _resolve_runtime(args)
    except ConfigError as err:
        return _config_failure(out_dir, str(err), plan_errors)

    publisher = VaultPublisher(
        token=token,
        enabled=upload_enabled,
        keep_files=args.keep_files,
        out_dir=out_dir,
    )

    if publisher.active:
        for repo in sorted({plan.repo for plan in plans}):
            if not publisher.prepare(repo):
                return _config_failure(
                    out_dir,
                    f"cannot access vault dataset {repo}",
                    plan_errors,
                )

    results = list(plan_errors)
    attempts = plans
    if publisher.active and not args.force:
        # Skip artifacts that are already vaulted before applying --limit so
        # repeated bounded runs keep advancing through the remaining apps.
        attempts = []
        for plan in plans:
            existing = publisher.existing(plan.repo, plan.filename_stem)
            if existing:
                print(f"[INFO] {plan.label}: already in vault as {existing}")
                results.append(
                    _result_for_plan(
                        plan,
                        status="already_present",
                        filename=existing,
                        source="vault",
                    )
                )
            else:
                attempts.append(plan)

    if args.limit > 0:
        attempts = attempts[: args.limit]

    pending: dict[str, list[PendingUpload]] = {}
    pending_bytes: dict[str, int] = {}
    limiter = RateLimiter(delay=0.0)
    interrupted = False

    def flush_repo(repo: str) -> None:
        """Uploads the current batch for one repository."""
        records = pending.get(repo, [])
        if not records:
            return
        uploaded = publisher.publish(repo, records)
        for record in records:
            if record.filename in uploaded:
                results[record.result_index]["status"] = "uploaded"
                if not publisher.keep_files:
                    _remove_quietly(record.path)
            else:
                results[record.result_index]["status"] = "failed"
                results[record.result_index]["reason"] = "HuggingFace upload failed"
        pending[repo] = []
        pending_bytes[repo] = 0

    for index, plan in enumerate(attempts):
        elapsed = time.monotonic() - started
        if 0 < args.max_runtime_seconds <= elapsed:
            interrupted = True
            for remaining in attempts[index:]:
                results.append(
                    _result_for_plan(
                        remaining,
                        status="skipped",
                        reason="max runtime reached",
                    )
                )
            break

        result_index = len(results)
        result, queued = _process_plan(
            plan=plan,
            args=args,
            out_dir=out_dir,
            limiter=limiter,
            publisher=publisher,
            apkmirror_available=apkmirror_available,
            result_index=result_index,
        )
        results.append(result)

        if queued is not None:
            repo = queued.repo
            pending.setdefault(repo, []).append(queued)
            pending_bytes[repo] = pending_bytes.get(repo, 0) + result.get(
                "size", 0
            )
            if (
                len(pending[repo]) >= UPLOAD_BATCH_FILES
                or pending_bytes[repo] >= UPLOAD_BATCH_BYTES
            ):
                flush_repo(repo)

        if args.delay_seconds > 0 and index < len(attempts) - 1:
            time.sleep(args.delay_seconds)

    for repo in list(pending):
        flush_repo(repo)
    publisher.flush_manifests()

    counts = Counter(result.get("status", "failed") for result in results)
    summary["results"] = results
    summary["counts"] = {
        "planned": len(plans),
        "attempted": len(attempts),
        "uploaded": counts.get("uploaded", 0),
        "already_present": counts.get("already_present", 0),
        "downloaded": counts.get("downloaded", 0),
        "failed": counts.get("failed", 0),
        "skipped": counts.get("skipped", 0),
    }
    summary["interrupted"] = interrupted
    summary["duration_seconds"] = round(time.monotonic() - started, 1)
    _write_summary(out_dir / "vault-sync-summary.json", summary)
    _print_summary(summary)

    if counts.get("failed", 0) and not (
        counts.get("uploaded", 0)
        or counts.get("already_present", 0)
        or counts.get("downloaded", 0)
    ):
        return 1
    if args.strict and counts.get("failed", 0):
        return 1
    return 0


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses vault sync CLI arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Fetch original APK/APKS/XAPK/APKM files into the HuggingFace "
            "source vault without patching or relabeling."
        )
    )
    parser.add_argument("--ecosystems", default="all")
    parser.add_argument("--apps", default="all")
    parser.add_argument(
        "--version-selection", choices=("stable", "beta"), default="stable"
    )
    parser.add_argument(
        "--arch",
        default="arm64-v8a",
        choices=("arm64-v8a", "armeabi-v7a", "x86", "x86_64", "universal"),
    )
    parser.add_argument(
        "--download-source",
        choices=("auto", "google_play", "apkmirror"),
        default="auto",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--custom-version", default="")
    parser.add_argument("--hf-dataset", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--delay-seconds", type=float, default=5.0)
    parser.add_argument(
        "--max-runtime-seconds", type=int, default=DEFAULT_MAX_RUNTIME_SECONDS
    )
    parser.add_argument("--output-dir", default="vault-downloads")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    args = parser.parse_args(argv)

    if args.limit < 0:
        parser.error("--limit must be zero or positive")
    if args.retries < 0 or args.retries > 5:
        parser.error("--retries must be between 0 and 5")
    if args.delay_seconds < 0:
        parser.error("--delay-seconds must be zero or positive")
    if args.max_runtime_seconds < 0:
        parser.error("--max-runtime-seconds must be zero or positive")
    return args


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    return run(parse_arguments(argv))


if __name__ == "__main__":
    sys.exit(main())
