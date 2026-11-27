"""Download a Morphe patch bundle from a Gitea/Forgejo release."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from core.repo_url import parse_repo_url
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    sys.path.insert(
        0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    )
    from core.repo_url import parse_repo_url

API_PAGE_LIMIT = 50
MAX_PAGES = 20
API_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BACKOFF_SECONDS = 2.0
VERSION_HEAD_RE = re.compile(r"^(\d+(?:\.\d+)*)(.*)$")
VERSION_SPLIT_RE = re.compile(r"[._+-]+")
PRE_STAGE_RANKS = {
    "dev": 0,
    "alpha": 1,
    "pre": 2,
    "preview": 2,
    "beta": 3,
    "experimental": 3,
    "exp": 3,
    "rc": 4,
}


def _log(message: str) -> None:
    """Write diagnostics to stderr so stdout stays machine-readable."""
    print(message, file=sys.stderr, flush=True)


def _warn(message: str) -> None:
    """Emit an Actions warning annotation in CI, otherwise a log line."""
    prefix = "::warning::" if os.environ.get("GITHUB_ACTIONS") else "[WARN] "
    _log(f"{prefix}{message}")


def _error(message: str) -> None:
    """Emit an Actions annotation when running in CI, otherwise a log line."""
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") else "[ERROR] "
    _log(f"{prefix}{message}")


def _version_key(value: str) -> tuple[Any, ...]:
    """Return a SemVer-aware sortable key for tags such as v2.13.0-experimental.6."""
    cleaned = value.strip().lstrip("vV").split("+", 1)[0]
    match = VERSION_HEAD_RE.match(cleaned)
    if match:
        base_str, suffix = match.group(1), match.group(2)
        base_nums = tuple(int(part) for part in base_str.split("."))
    else:
        base_nums = ()
        suffix = cleaned

    suffix_clean = suffix.lstrip("._-")
    if not suffix_clean:
        return (base_nums, 1, ())

    tokens: list[tuple[int, int, str]] = []
    has_prerelease_stage = False
    for part in VERSION_SPLIT_RE.split(suffix_clean):
        if not part:
            continue
        if part.isdigit():
            tokens.append((0, int(part), ""))
            continue
        lower = part.lower()
        if lower in PRE_STAGE_RANKS:
            has_prerelease_stage = True
            tokens.append((1, PRE_STAGE_RANKS[lower], lower))
        else:
            tokens.append((1, 5, lower))

    is_stable = 0 if has_prerelease_stage else 1
    return (base_nums, is_stable, tuple(tokens))


def _parse_repo_url(value: str) -> tuple[str, str, str]:
    """Split an https://host/owner/repo URL into ``(host, owner, repo)``."""
    if "://" not in (value or "").strip():
        raise ValueError("repository URL must use https://")
    ref = parse_repo_url(value)
    return ref.host, ref.owner, ref.repo


def _build_session() -> requests.Session:
    """Create a session with bounded retries for transient failures."""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "Chihafuyu-Builder/1.0"})
    return session


def _get_json(session: requests.Session, url: str, **kwargs: Any) -> Any:
    """Fetch JSON from an API endpoint with a clear failure message."""
    response = session.get(
        url,
        timeout=API_TIMEOUT,
        headers={"Accept": "application/json"},
        **kwargs,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as err:
        raise RuntimeError(f"GET {url} failed: {err}") from err
    try:
        return response.json()
    except ValueError as err:
        raise RuntimeError(f"GET {url} did not return JSON") from err


def _list_releases(
    session: requests.Session, api_base: str
) -> list[dict[str, Any]]:
    """Return every release, following Gitea's page-based pagination."""
    releases: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        batch = _get_json(
            session,
            f"{api_base}/releases",
            params={"limit": API_PAGE_LIMIT, "page": page},
        )
        if not isinstance(batch, list):
            raise RuntimeError("Gitea release list is not an array")
        releases.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < API_PAGE_LIMIT:
            return releases
    raise RuntimeError(f"Release list exceeded {MAX_PAGES} pages")


def _is_valid_mpp_asset(asset: Any) -> bool:
    """Return True when an asset entry is a downloadable HTTPS .mpp bundle."""
    if not isinstance(asset, dict):
        return False
    name = str(asset.get("name") or "")
    if not name.lower().endswith(".mpp"):
        return False
    url = str(asset.get("browser_download_url") or "")
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname)


def _select_release(
    releases: list[dict[str, Any]],
    track: str,
    *,
    allow_stable_fallback: bool = True,
) -> tuple[dict[str, Any], str]:
    """Pick the highest release with an .mpp asset and return ``(release, track)``."""
    if track not in ("stable", "experimental"):
        raise ValueError(f"Unsupported track: {track}")

    published = [
        release
        for release in releases
        if not release.get("draft")
        and release.get("tag_name")
        and any(_is_valid_mpp_asset(asset) for asset in release.get("assets") or [])
    ]
    stable = [rel for rel in published if not rel.get("prerelease")]
    experimental = [rel for rel in published if rel.get("prerelease")]

    if track == "experimental":
        if experimental:
            best = max(experimental, key=lambda item: _version_key(str(item["tag_name"])))
            return best, "experimental"
        if allow_stable_fallback and stable:
            best = max(stable, key=lambda item: _version_key(str(item["tag_name"])))
            _warn(
                "No experimental release with an .mpp asset found; "
                f"falling back to stable release {best['tag_name']}."
            )
            return best, "stable"
        raise RuntimeError("No experimental release with an .mpp asset found")

    if not stable:
        raise RuntimeError("No stable release with an .mpp asset found")
    best = max(stable, key=lambda item: _version_key(str(item["tag_name"])))
    return best, "stable"


def _select_asset(release: dict[str, Any]) -> dict[str, Any]:
    """Choose the best .mpp asset from a release."""
    assets = [
        asset
        for asset in release.get("assets") or []
        if _is_valid_mpp_asset(asset)
    ]
    if not assets:
        raise RuntimeError(
            f"Release {release.get('tag_name')} has no .mpp asset"
        )

    tag = str(release.get("tag_name") or "")
    tag_variants = sorted({tag, tag.lstrip("vV")}, key=len, reverse=True)

    def _matches_tag(asset_name: str) -> bool:
        stem = re.sub(r"\.mpp$", "", asset_name, flags=re.IGNORECASE)
        return any(
            variant
            and re.search(
                rf"(?:^|[^A-Za-z0-9]){re.escape(variant)}$",
                stem,
            )
            for variant in tag_variants
        )

    matching = [
        asset for asset in assets if _matches_tag(str(asset.get("name", "")))
    ]
    if not matching:
        matching = [
            asset
            for asset in assets
            if any(
                variant and variant in str(asset.get("name", ""))
                for variant in tag_variants
            )
        ]
    pool = matching or assets

    def asset_key(asset: dict[str, Any]) -> tuple[Any, ...]:
        name = str(asset.get("name", ""))
        stem = re.sub(r"\.mpp$", "", name, flags=re.IGNORECASE)
        stem = re.sub(r"^patches[-_.]?", "", stem, flags=re.IGNORECASE)
        return _version_key(stem)

    return max(pool, key=asset_key)


def _validate_https_url(url: str) -> None:
    """Reject non-HTTPS URLs before downloading executable patch bundles."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"Refusing non-HTTPS asset URL: {url!r}")


def _download_asset_once(
    session: requests.Session,
    url: str,
    expected_size: int,
    output: str,
) -> None:
    """Perform one streaming download attempt into an atomic temp file."""
    output_dir = os.path.dirname(output) or "."
    response = session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
    try:
        try:
            response.raise_for_status()
        except requests.HTTPError as err:
            raise RuntimeError(f"GET {url} failed: {err}") from err
        final_url = str(getattr(response, "url", "") or url)
        _validate_https_url(final_url)

        fd, tmp_path = tempfile.mkstemp(
            prefix=".patches-", suffix=".mpp", dir=output_dir
        )
        try:
            total = 0
            with os.fdopen(fd, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
                        total += len(chunk)
            if total == 0:
                raise RuntimeError("Downloaded patch bundle is empty")
            if expected_size and total != expected_size:
                raise RuntimeError(
                    f"Downloaded {total} bytes, expected {expected_size}"
                )
            with open(tmp_path, "rb") as handle:
                if handle.read(2) != b"PK":
                    raise RuntimeError(
                        "Downloaded file is not a ZIP/Morphe patch bundle"
                    )
            os.replace(tmp_path, output)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    finally:
        response.close()


def _download_asset(
    session: requests.Session, asset: dict[str, Any], output: str
) -> None:
    """Stream an asset to output with retries, rejecting empty/truncated payloads."""
    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise RuntimeError("Release asset has no download URL")
    _validate_https_url(url)

    expected_size = asset.get("size")
    try:
        expected_size = int(expected_size) if expected_size is not None else 0
    except (TypeError, ValueError):
        expected_size = 0

    output = os.path.abspath(output)
    output_dir = os.path.dirname(output) or "."
    os.makedirs(output_dir, exist_ok=True)

    last_err: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            _download_asset_once(session, url, expected_size, output)
            return
        except (requests.RequestException, RuntimeError) as err:
            last_err = err
            if attempt >= DOWNLOAD_ATTEMPTS:
                break
            _log(
                f"[WARN] Asset download attempt {attempt}/{DOWNLOAD_ATTEMPTS} "
                f"failed ({err}); retrying..."
            )
            time.sleep(DOWNLOAD_BACKOFF_SECONDS * attempt)
    assert last_err is not None
    raise last_err


def resolve_and_fetch_release(
    repo_url: str,
    track: str,
    output: str,
    session: requests.Session | None = None,
    *,
    allow_stable_fallback: bool = True,
) -> tuple[str, str]:
    """Download the selected release asset and return ``(tag, resolved_track)``."""
    host, owner, repo = _parse_repo_url(repo_url)
    if host == "github.com":
        raise ValueError("GitHub repositories use the core engine's gh path")
    if track not in ("stable", "experimental"):
        raise ValueError(f"Unsupported track: {track}")

    owns_session = session is None
    session = session or _build_session()
    try:
        api_base = f"https://{host}/api/v1/repos/{owner}/{repo}"
        _log(f"[INFO] Resolving {track} release from {api_base}...")
        releases = _list_releases(session, api_base)
        release, resolved_track = _select_release(
            releases, track, allow_stable_fallback=allow_stable_fallback
        )
        tag = str(release.get("tag_name") or "")
        if not tag or "\n" in tag or "\r" in tag:
            raise RuntimeError("Release has an invalid tag name")
        asset = _select_asset(release)
        _log(f"[INFO] Downloading {asset.get('name')} from {tag}...")
        _download_asset(session, asset, output)
        return tag, resolved_track
    finally:
        if owns_session:
            session.close()


def fetch_release(
    repo_url: str,
    track: str,
    output: str,
    session: requests.Session | None = None,
) -> str:
    """Download the selected release asset and return its tag."""
    tag, _ = resolve_and_fetch_release(
        repo_url, track, output, session=session
    )
    return tag


def _write_github_output(path: str, tag: str, resolved_track: str) -> None:
    """Append resolved tag and effective track to GITHUB_OUTPUT safely."""
    for label, value in (("tag", tag), ("resolved_track", resolved_track)):
        if "\n" in value or "\r" in value:
            raise RuntimeError(f"Refusing multiline value for {label}")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"tag={tag}\n")
        handle.write(f"resolved_track={resolved_track}\n")


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, fetch the release, and print its tag."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="https://host/owner/repo")
    parser.add_argument(
        "--track",
        required=True,
        type=str.lower,
        choices=("stable", "experimental", "pre-release", "prerelease"),
        help="Release channel to fetch",
    )
    parser.add_argument("--output", required=True, help="Path for the .mpp file")
    parser.add_argument(
        "--github-output",
        default="",
        help="Optional GITHUB_OUTPUT file to append tag and resolved_track",
    )
    args = parser.parse_args(argv)
    track = (
        "experimental"
        if args.track in ("pre-release", "prerelease")
        else args.track
    )
    try:
        tag, resolved_track = resolve_and_fetch_release(
            args.repo, track, args.output
        )
        if args.github_output:
            _write_github_output(args.github_output, tag, resolved_track)
    except (
        OSError,
        RuntimeError,
        ValueError,
        requests.RequestException,
    ) as err:
        _error(str(err))
        return 1
    print(tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
