"""Shared repository URL parsing, validation, and forge classification."""

import argparse
from dataclasses import dataclass
import re
import sys
from urllib.parse import urlparse

REPO_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")
REPO_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")


@dataclass(frozen=True)
class RepoRef:
    """Normalized components of a GitHub or Gitea/Forgejo repository URL."""

    host: str
    owner: str
    repo: str
    forge: str
    url: str

    @property
    def repo_path(self) -> str:
        """Return the ``owner/repo`` path component."""
        return f"{self.owner}/{self.repo}"


def _validate_owner_repo(owner: str, repo: str, *, strip_git: bool) -> tuple[str, str]:
    """Validate owner and repository name segments."""
    if not REPO_SEGMENT_RE.fullmatch(owner) or not REPO_SEGMENT_RE.fullmatch(repo):
        raise ValueError("repository URL must be 'owner/repo' or 'https://host/owner/repo'")
    if owner in (".", "..") or repo in (".", ".."):
        raise ValueError("repository URL must not contain '.' or '..' segments")
    if strip_git and repo.endswith(".git"):
        repo = repo[:-4]
    if not repo or repo in (".", ".."):
        raise ValueError("repository name cannot be empty")
    return owner, repo


def parse_repo_url(value: str) -> RepoRef:
    """Parse ``owner/repo`` or ``https://host/owner/repo`` into a RepoRef."""
    raw = (value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("repository URL cannot be empty")
    if any(ch.isspace() for ch in raw):
        raise ValueError("repository URL must not contain whitespace")

    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("repository URL must use https://")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "repository URL must not contain credentials, a query, or a fragment"
            )
        if not REPO_HOST_RE.fullmatch(parsed.hostname):
            raise ValueError("repository URL has an invalid host")

        try:
            port = parsed.port
        except ValueError as err:
            raise ValueError("repository URL has an invalid port") from err
        if ":" in parsed.netloc and port is None:
            raise ValueError("repository URL has an empty port")
        if port is not None and port < 1:
            raise ValueError("repository URL has an invalid port")

        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2:
            raise ValueError("repository URL must be https://host/owner/repo")
        owner, repo = _validate_owner_repo(parts[0], parts[1], strip_git=True)

        host = parsed.hostname.lower()
        if host == "www.github.com":
            host = "github.com"
        host_with_port = f"{host}:{port}" if port and port != 443 else host
        forge = "github" if host == "github.com" else "gitea"
        return RepoRef(
            host=host_with_port,
            owner=owner,
            repo=repo,
            forge=forge,
            url=f"https://{host_with_port}/{owner}/{repo}",
        )

    parts = raw.lstrip("/").split("/")
    if len(parts) != 2:
        raise ValueError("repository identifier must be 'owner/repo'")
    owner, repo = _validate_owner_repo(parts[0], parts[1], strip_git=True)
    return RepoRef(
        host="github.com",
        owner=owner,
        repo=repo,
        forge="github",
        url=f"https://github.com/{owner}/{repo}",
    )


def normalize_repo_url(value: str) -> str:
    """Normalize a repository identifier to an HTTPS URL, or return ``""``."""
    cleaned = (value or "").strip()
    if not cleaned:
        return ""
    try:
        return parse_repo_url(cleaned).url
    except ValueError:
        print(f"[WARN] Invalid repo URL ignored: {value!r}")
        return ""


def main(argv: list[str] | None = None) -> int:
    """Resolve a repository identifier and print its normalized fields."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="'owner/repo' or 'https://host/owner/repo'")
    args = parser.parse_args(argv)
    try:
        ref = parse_repo_url(args.repo)
    except ValueError as err:
        print(f"::error::{err}: {args.repo}", file=sys.stderr)
        return 1
    for line in (ref.host, ref.repo_path, ref.forge, ref.url):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
