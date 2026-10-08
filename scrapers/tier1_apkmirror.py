"""Tier 1 Scraper: APKMirror utilizing FlareSolverr Microservice to Bypass WAF."""

import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
import requests

from core.context import Context
from core.utils import download_file_stream
from .base import BaseScraper


EDITION_SLUG_REGEX = re.compile(
    r"\b(amazon|fire-tablet|fire-tv|androidtv|wear|go-edition|"
    r"lite|enterprise|kids|headunit|auto)\b",
    re.IGNORECASE,
)

# Maximum attempts for a single FlareSolverr fetch before giving up.
FETCH_ATTEMPTS = 3
# Per-attempt timeout when handing the request to FlareSolverr.
FETCH_TIMEOUT_SECONDS = 45
# URL prefix used to shorten log lines that would otherwise be very long.
URL_LOG_WIDTH = 90


def _short_url(url: str) -> str:
    """Truncates a URL for log output while keeping the scheme and host visible."""
    if len(url) <= URL_LOG_WIDTH:
        return url
    return url[: URL_LOG_WIDTH - 3] + "..."


@dataclass
class _DummyResponse:
    """Mock requests.Response object for FlareSolverr HTML returns."""

    status_code: int
    text: str
    url: str

    def raise_for_status(self) -> None:
        """Raises stored HTTP error, if one occurred."""
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"Error: {self.status_code}"
            )

    def __enter__(self) -> _DummyResponse:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


class _FlareSolverrSession:
    """Routes HTML via FlareSolverr and binary streams via native requests."""

    def __init__(self) -> None:
        self.proxy_url = "http://localhost:8191/v1"
        self.session = requests.Session()
        self.proxy_session_id = self._create_session()

    def _create_session(self) -> str:
        """Initializes a persistent browser session in FlareSolverr."""
        try:
            res = requests.post(
                self.proxy_url,
                json={"cmd": "sessions.create"},
                timeout=15,
            )
            return res.json().get("session", "")
        except (requests.exceptions.RequestException, ValueError):
            return ""

    def get(self, *args: Any, **kwargs: Any) -> Any:
        """Executes GET requests using the appropriate transport layer."""
        if kwargs.get("stream"):
            return self.session.get(*args, **kwargs)

        url = args[0] if args else kwargs.get("url")
        if not url:
            return _DummyResponse(status_code=400, text="", url="")

        raw_timeout = kwargs.get("timeout") or FETCH_TIMEOUT_SECONDS
        if isinstance(raw_timeout, (tuple, list)):
            raw_timeout = raw_timeout[0] if raw_timeout else FETCH_TIMEOUT_SECONDS
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = float(FETCH_TIMEOUT_SECONDS)

        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": int(timeout * 1000),
        }
        if self.proxy_session_id:
            payload["session"] = self.proxy_session_id

        try:
            res = requests.post(
                self.proxy_url,
                json=payload,
                timeout=timeout + 15,
            )
            data = res.json()

            if data.get("status") == "ok":
                solution = data.get("solution", {})
                html = solution.get("response", "")
                solved_url = solution.get("url", url)

                for cookie in solution.get("cookies", []):
                    self.session.cookies.set(
                        cookie["name"],
                        cookie["value"],
                        domain=cookie.get("domain") or None,
                    )

                if "userAgent" in solution:
                    self.session.headers.update(
                        {"User-Agent": solution["userAgent"]}
                    )

                return _DummyResponse(
                    status_code=200, text=html, url=solved_url
                )

            err_msg = data.get("message", "Unknown FlareSolverr error")
            print(
                f"[WARN] FlareSolverr returned error status: {err_msg}",
                flush=True,
            )
            return _DummyResponse(status_code=403, text="", url=url)

        except (requests.exceptions.RequestException, ValueError) as err:
            print(f"[WARN] FlareSolverr connection failed: {err}", flush=True)
            return _DummyResponse(status_code=500, text="", url=url)

    def close(self) -> None:
        """Destroys proxy session and closes the native requests session."""
        if self.proxy_session_id:
            try:
                requests.post(
                    self.proxy_url,
                    json={
                        "cmd": "sessions.destroy",
                        "session": self.proxy_session_id,
                    },
                    timeout=10,
                )
            except (requests.exceptions.RequestException, ValueError):
                pass
        self.session.close()


class ApkmirrorScraper(BaseScraper):
    """Scrapes APKs from APKMirror handling WAF via local FlareSolverr."""

    def __init__(self) -> None:
        super().__init__()
        self._session = _FlareSolverrSession()

    @classmethod
    def tier_name(cls) -> str:
        """Returns the tier identifier."""
        return "apkmirror"

    def close(self) -> None:
        """Cleans up the FlareSolverr session to prevent memory leaks."""
        self._session.close()

    def _safe_get(self, ctx: Context, url: str) -> Any | None:
        """Fetches page source ensuring FlareSolverr resolves WAF challenges.

        Logs each attempt with elapsed seconds so long-running Cloudflare
        challenges are visible in CI instead of appearing as a hang.
        """
        ctx.limiter.wait()
        time.sleep(random.uniform(2.5, 4.5))

        for attempt in range(1, FETCH_ATTEMPTS + 1):
            print(
                f"[INFO] Fetch attempt {attempt}/{FETCH_ATTEMPTS}: "
                f"{_short_url(url)}",
                flush=True,
            )
            start = time.monotonic()
            resp = self._session.get(url, timeout=FETCH_TIMEOUT_SECONDS)
            elapsed = time.monotonic() - start

            if resp.status_code == 200 and "apkmirror.com" in str(resp.url):
                print(
                    f"[INFO] Fetch succeeded in {elapsed:.1f}s.",
                    flush=True,
                )
                return resp

            print(
                f"[WARN] FlareSolverr WAF resolution failed "
                f"(attempt {attempt}/{FETCH_ATTEMPTS}, "
                f"{elapsed:.1f}s, status={resp.status_code}).",
                flush=True,
            )

            if attempt < FETCH_ATTEMPTS:
                delay = random.uniform(5.0, 10.0)
                print(
                    f"[INFO] Retrying in {delay:.1f}s...",
                    flush=True,
                )
                time.sleep(delay)

        return None

    @staticmethod
    def _is_valid_release_link(
        link: Any, base_ver: str, exc_kws: list[str], inc_kws: list[str]
    ) -> str | None:
        href = link.get("href", "")
        if not href or EDITION_SLUG_REGEX.search(href):
            return None

        text = link.text.lower()

        has_ver_text = base_ver.lower() in text
        has_ver_href = base_ver.replace(".", "-").lower() in href.lower()

        is_smart_match = False
        if not (has_ver_text or has_ver_href):
            base_parts = base_ver.split(".")
            pattern = (
                r"\b"
                + r"-".join(map(re.escape, base_parts))
                + r"(?:-[a-zA-Z0-9]+)*\b"
            )
            if re.search(pattern, href, re.IGNORECASE):
                is_smart_match = True

        if not (has_ver_text or has_ver_href or is_smart_match):
            return None

        if any(k in text for k in exc_kws):
            return None

        if inc_kws and not all(k in text for k in inc_kws):
            return None

        return urljoin("https://www.apkmirror.com", href)

    @staticmethod
    def _get_filter_kws(ctx: Context) -> tuple[list[str], list[str]]:
        exc_kws = ["secondary"] + [
            k.lower()
            for k in ctx.app_data.get("apkm_exclude", [])
            if k.strip()
        ]
        inc_kws = [
            k.lower()
            for k in ctx.app_data.get("apkm_include", [])
            if k.strip()
        ]
        return exc_kws, inc_kws

    @staticmethod
    def _get_search_queries(ctx: Context, base_ver: str) -> list[str]:
        search_term = ctx.app_data.get("search_term", ctx.pkg)

        short_term = (
            search_term.replace(" Browser", "")
            .replace(" App", "")
            .strip()
        )
        if "." in short_term and " " not in short_term:
            parts = short_term.split(".")
            if len(parts) >= 2:
                short_term = (
                    parts[-1] if len(parts[-1]) > 3 else parts[-2]
                )

        short_term = short_term.split("-")[0].strip()

        return list(dict.fromkeys([
            ctx.pkg,
            search_term,
            f"{search_term} {base_ver}",
            f"{short_term} {base_ver}",
            short_term,
        ]))

    def _find_release(self, ctx: Context) -> str | None:
        base_ver = (
            ctx.target_ver.split("-")[0]
            if "-" in ctx.target_ver and ctx.target_ver[:1].isdigit()
            else ctx.target_ver
        )

        queries = self._get_search_queries(ctx, base_ver)
        exc_kws, inc_kws = self._get_filter_kws(ctx)
        total = len(queries)

        for idx, query in enumerate(queries, start=1):
            print(
                f"[INFO] Search query {idx}/{total}: {query!r}",
                flush=True,
            )
            url = (
                "https://www.apkmirror.com/"
                f"?post_type=app_release&s={quote_plus(query)}"
            )
            resp = self._safe_get(ctx, url)

            if not resp:
                continue

            if (
                "?post_type=app_release" not in resp.url
                and "-release/" in resp.url
            ):
                print("[INFO] Auto-redirected to release page.", flush=True)
                return str(resp.url)

            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.find_all("a", class_="fontBlack"):
                valid_url = self._is_valid_release_link(
                    link, base_ver, exc_kws, inc_kws
                )
                if valid_url:
                    print(
                        f"[INFO] Matched release: {_short_url(valid_url)}",
                        flush=True,
                    )
                    return valid_url

        return None

    @staticmethod
    def _log_expected_sha256(soup: BeautifulSoup) -> None:
        modal = soup.select_one(
            "#safeDownload .modal-body, .safeDownload .modal-body"
        )
        if not modal:
            return
        block_text = modal.text
        if (
            "APK file hashes" in block_text
            and "APK certificate fingerprints" in block_text
        ):
            file_section = block_text.split("APK file hashes")[1]
            file_section = file_section.split(
                "APK certificate fingerprints"
            )[0]
            hash_match = re.search(r"[0-9a-fA-F]{64}", file_section)
            if hash_match:
                print(
                    f"[INFO] Expected SHA-256 extracted: "
                    f"{hash_match.group(0)}",
                    flush=True,
                )

    @staticmethod
    def _get_download_buttons(soup: BeautifulSoup) -> list[Any]:
        btns = []
        for btn in soup.find_all("a", href=True):
            href = btn["href"]
            classes = btn.get("class", [])
            if isinstance(classes, str):
                classes = [classes]

            if "variantsButton" in classes or href.startswith("#"):
                continue

            if "downloadButton" in classes or "/download/?key=" in href:
                btns.append(btn)
        return btns

    @staticmethod
    def _pick_variant_button(btns: list[Any], is_bundle: bool) -> Any:
        for btn in btns:
            has_force = "forcebaseapk" in btn["href"].lower()
            if (is_bundle and not has_force) or (
                not is_bundle and has_force
            ):
                return btn
        return btns[0]

    @staticmethod
    def _get_final_download_link(d_soup: BeautifulSoup) -> Any | None:
        dl_btn = d_soup.find("a", id="download-link")
        if dl_btn and dl_btn.has_attr("href"):
            return dl_btn

        return d_soup.find(
            lambda tag: tag.name == "a"
            and tag.has_attr("href")
            and (
                "download.php" in tag["href"]
                or "/download/?key=" in tag["href"]
            )
        )

    def _process_variant_page(
        self, ctx: Context, var_url: str, is_bundle: bool
    ) -> str | None:
        """Resolves and downloads from a variant page, logging each phase."""
        print(
            f"[INFO] Loading variant page: {_short_url(var_url)}",
            flush=True,
        )
        v_resp = self._safe_get(ctx, var_url)
        if not v_resp:
            print("[WARN] APKMirror variant page failed.", flush=True)
            time.sleep(random.uniform(4.0, 7.0))
            return None

        v_soup = BeautifulSoup(v_resp.text, "html.parser")

        btns = self._get_download_buttons(v_soup)
        if not btns:
            print(
                "[WARN] Download button not found on variant page.",
                flush=True,
            )
            return None

        btn = self._pick_variant_button(btns, is_bundle)

        if not is_bundle:
            self._log_expected_sha256(v_soup)

        p_type = "APKM Bundle" if is_bundle else "Raw APK"
        print(f"[INFO] Preparing to extract: {p_type}", flush=True)

        dl_page = urljoin("https://www.apkmirror.com", btn["href"])
        print(
            f"[INFO] Resolving download page: {_short_url(dl_page)}",
            flush=True,
        )
        d_resp = self._safe_get(ctx, dl_page)

        if not d_resp:
            print("[WARN] APKMirror download page failed.", flush=True)
            return None

        print("[INFO] Extracting final download link...", flush=True)
        dl_btn = self._get_final_download_link(
            BeautifulSoup(d_resp.text, "html.parser")
        )

        out_path = None
        if dl_btn and dl_btn.has_attr("href"):
            out_path = ctx.get_out_path(".apkm" if is_bundle else ".apk")
            print(
                f"[INFO] Downloading {p_type} from APKMirror...",
                flush=True,
            )
            time.sleep(random.uniform(3.0, 5.0))
            start = time.monotonic()
            if not download_file_stream(
                self._session,
                urljoin("https://www.apkmirror.com", dl_btn["href"]),
                out_path,
                dl_page,
            ):
                print(
                    f"[WARN] APKMirror download failed after "
                    f"{time.monotonic() - start:.1f}s.",
                    flush=True,
                )
                out_path = None
            else:
                print(
                    f"[INFO] APKMirror download completed in "
                    f"{time.monotonic() - start:.1f}s.",
                    flush=True,
                )
        else:
            print(
                "[WARN] Final download link not found on APKMirror.",
                flush=True,
            )

        return out_path

    @staticmethod
    def _is_arch_match(text: str, target_arch: str, pass_idx: int) -> bool:
        text = text.lower()
        target_arch = target_arch.lower()

        if target_arch in text or "universal" in text or "noarch" in text:
            return True

        arch_list = (
            "arm64-v8a", "armeabi-v7a", "x86", "x86_64", "armeabi",
        )
        if not any(a in text for a in arch_list):
            return True

        is_multi_arm = "arm64-v8a" in text and "armeabi-v7a" in text
        if is_multi_arm and target_arch in (
            "arm64-v8a", "armeabi-v7a", "universal",
        ):
            return True

        if pass_idx >= 3 and target_arch == "universal" and (
            "arm64-v8a" in text or "armeabi-v7a" in text
        ):
            return True

        return False

    @staticmethod
    def _is_bundle_row(row: Any) -> bool:
        badges = row.find_all(
            "span", class_=re.compile(r"badge", re.IGNORECASE)
        )
        for badge in badges:
            if "BUNDLE" in badge.text.upper() or "APKM" in badge.text.upper():
                return True
        return "bundle" in row.text.lower()

    def _extract_row(
        self, ctx: Context, row: Any, opts: dict
    ) -> str | None:
        text = row.text.lower()
        pass_idx = opts.get("pass_idx", 1)
        force_b = opts.get("force_b", False)
        ver_code = str(opts.get("ver_code") or "").lower()

        is_bundle = self._is_bundle_row(row)

        is_mismatch = not is_bundle if force_b else is_bundle
        if pass_idx in (1, 2) and is_mismatch:
            return None
        if pass_idx in (3, 4) and not is_mismatch:
            return None

        invalid_ver = (
            pass_idx in (1, 3) and ver_code and ver_code not in text
        )
        if invalid_ver or not self._is_arch_match(
            text, ctx.arch.lower(), pass_idx
        ):
            return None

        link = row.find("a", class_="accent_color")
        if not link:
            return None

        return self._process_variant_page(
            ctx,
            urljoin("https://www.apkmirror.com", link["href"]),
            is_bundle,
        )

    def _find_variant_in_rows(
        self,
        ctx: Context,
        rows: list[Any],
        ver_code: str,
        force_b: bool,
    ) -> str | None:
        for pass_idx in (1, 2, 3, 4):
            print(
                f"[INFO] Variant pass {pass_idx}/4 "
                f"(bundle={force_b}).",
                flush=True,
            )
            opts = {
                "ver_code": ver_code,
                "pass_idx": pass_idx,
                "force_b": force_b,
            }
            for row in rows:
                link = row.find("a", class_="accent_color")
                if not link:
                    continue

                href = link.get("href", "")
                if href.endswith("-release/") or href.endswith("-release"):
                    continue

                out = self._extract_row(ctx, row, opts)
                if out:
                    return out
        return None

    def _download_variant(
        self, ctx: Context, rel_url: str, ver_code: str, force_b: bool
    ) -> str | None:
        print(
            f"[INFO] Loading release page: {_short_url(rel_url)}",
            flush=True,
        )
        resp = self._safe_get(ctx, rel_url)
        if not resp:
            print("[WARN] APKMirror release page failed.", flush=True)
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        v_table = soup.find(
            "div", class_=re.compile(r"variants-table", re.IGNORECASE)
        )
        rows = (
            v_table.find_all("div", class_="table-row")
            if v_table
            else soup.find_all("div", class_="table-row")
        )
        print(
            f"[INFO] Parsed {len(rows)} variant row(s) from release table.",
            flush=True,
        )

        if rows:
            out = self._find_variant_in_rows(ctx, rows, ver_code, force_b)
            if out:
                return out

            print(
                "[WARN] No matching variants found in release table.",
                flush=True,
            )
            return None

        dl_btn = soup.find(
            lambda tag: tag.name == "a"
            and "downloadButton" in tag.get("class", [])
            and "variantsButton" not in tag.get("class", [])
        )

        if dl_btn:
            return self._process_variant_page(
                ctx, rel_url, "bundle" in dl_btn.text.lower()
            )

        print(
            "[WARN] Release table and fallback download button both missing.",
            flush=True,
        )
        return None

    def scrape(self, ctx: Context) -> str | None:
        """Executes the scraping process from APKMirror."""
        print(f"[TIER 1] APKMirror: v{ctx.target_ver}", flush=True)
        ver_code = ctx.app_data.get("version_codes", {}).get(ctx.arch)
        try:
            rel_url = self._find_release(ctx)
            if not rel_url:
                print("[WARN] Release not found.", flush=True)
                return None
            return self._download_variant(
                ctx,
                rel_url,
                ver_code,
                ctx.app_data.get("force_bundle", False),
            )
        except Exception as err:  # pylint: disable=broad-except
            print(f"[ERROR] Tier 1 failed: {err}", flush=True)
        return None
