"""Tier 1 Scraper: APKMirror utilizing FlareSolverr Microservice to Bypass WAF."""

import random
import re
import time
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

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

EXCLUDED_RELEASE_TAGS = ("extended", "secondary")
PLAIN_RELEASE_TAGS = ("", "release", "stable")
RELEASE_TAG_PRIORITIES = {"beta": 1}
UNKNOWN_RELEASE_TAG_PRIORITY = 2

FETCH_ATTEMPTS = 3
FETCH_TIMEOUT_SECONDS = 45
URL_LOG_WIDTH = 90


def _short_url(url: str) -> str:
    """Truncates a URL for log output while keeping the scheme and host visible."""
    if len(url) <= URL_LOG_WIDTH:
        return url
    return url[: URL_LOG_WIDTH - 3] + "..."


def _resolve_ver_code(app_data: dict, arch: str) -> str | None:
    """Returns the target version code, tolerating separator variants.

    Patches-list sources use mixed separators (X86_64 vs x86-64) while
    the workflow inputs and scraper lookups stay on the underscore form.
    """
    version_codes = app_data.get("version_codes") or {}
    if not isinstance(version_codes, dict):
        return None
    candidates = (arch, arch.replace("_", "-"), arch.replace("-", "_"))
    for key in candidates:
        value = version_codes.get(key)
        if value is not None:
            return str(value)
    return None


def _make_dummy_response(status_code: int, text: str, url: str) -> requests.Response:
    """Constructs a real requests.Response object for FlareSolverr HTML returns."""
    res = requests.Response()
    res.status_code = status_code
    res._content = text.encode("utf-8")  # pylint: disable=protected-access
    res.url = url
    return res


def _is_arch_token(text: str, token: str) -> bool:
    """Matches *token* in *text* only as a standalone architecture word.

    Hyphens and underscores bind tokens together, so "x86" must not match
    "x86_64" or "x86-64" (a different architecture), while "arm64-v8a"
    still matches its exact hyphenated form. Matching is case-insensitive
    so mixed-case callers cannot produce false negatives.
    """
    pattern = rf"(?<![a-z0-9_-]){re.escape(token)}(?![a-z0-9_-])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _has_version_token(value: str, version: str) -> bool:
    """Matches *version* as a complete token, not as a numeric prefix.

    Without the digit boundaries a target like ``21.46.52`` would also
    match ``21.46.524``, which is a different APKMirror release.
    """
    pattern = rf"(?<![\d.]){re.escape(version)}(?![\d.])"
    return re.search(pattern, value, re.IGNORECASE) is not None


def _extract_release_tag(href: str, text: str, base_ver: str) -> str:
    """Returns the upload tag that follows the target version.

    APKMirror encodes alternate tracks in the slug and title, for example
    ``youtube-21-47-483-beta-release`` / ``YouTube 21.47.483-BETA``.
    A plain release returns an empty string or ``release``.
    """
    candidates = (
        (text.lower(), base_ver.lower()),
        (href.lower(), base_ver.replace(".", "-").lower()),
    )
    for value, version in candidates:
        pos = value.rfind(version)
        if pos < 0:
            continue
        suffix = value[pos + len(version):]
        match = re.match(r"[-_\s]+([a-z0-9]+)", suffix)
        if match:
            return match.group(1)
    return ""


def _release_priority(href: str, text: str, base_ver: str) -> int:
    """Ranks release uploads: plain stable first, beta as fallback."""
    tag = _extract_release_tag(href, text, base_ver)
    if tag in PLAIN_RELEASE_TAGS:
        return 0
    return RELEASE_TAG_PRIORITIES.get(tag, UNKNOWN_RELEASE_TAG_PRIORITY)


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

    def get(self, *args: Any, **kwargs: Any) -> requests.Response:
        """Executes GET requests using the appropriate transport layer."""
        if kwargs.get("stream"):
            return self.session.get(*args, **kwargs)

        url = args[0] if args else kwargs.get("url")
        if not url:
            return _make_dummy_response(status_code=400, text="", url="")

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

                return _make_dummy_response(
                    status_code=200, text=html, url=solved_url
                )

            err_msg = data.get("message", "Unknown FlareSolverr error")
            print(
                f"[WARN] FlareSolverr returned error status: {err_msg}",
                flush=True,
            )
            return _make_dummy_response(status_code=403, text="", url=url)

        except (requests.exceptions.RequestException, ValueError) as err:
            print(f"[WARN] FlareSolverr connection failed: {err}", flush=True)
            return _make_dummy_response(status_code=500, text="", url=url)

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

    @staticmethod
    def _extract_version_codes(text: str) -> list[str]:
        """Extracts candidate version codes (6+ digit numbers) from text.

        APKMirror variant rows embed the version code as a standalone
        numeric token, e.g. 'arm64-v8a 475417104 nodpi BUNDLE'.
        """
        return list(dict.fromkeys(re.findall(r"\b\d{6,}\b", text)))

    def _safe_get(self, ctx: Context, url: str) -> requests.Response | None:
        """Fetches page source ensuring FlareSolverr resolves WAF challenges."""
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

            if resp.status_code == 200 and urlparse(
                str(resp.url)
            ).hostname in ("apkmirror.com", "www.apkmirror.com"):
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

        has_ver_text = _has_version_token(text, base_ver)
        has_ver_href = _has_version_token(
            href, base_ver.replace(".", "-")
        )

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

        if _extract_release_tag(href, text, base_ver) in EXCLUDED_RELEASE_TAGS:
            return None

        if inc_kws and not all(k in text for k in inc_kws):
            return None

        return urljoin("https://www.apkmirror.com", href)

    @staticmethod
    def _get_filter_kws(ctx: Context) -> tuple[list[str], list[str]]:
        exc_kws = list(EXCLUDED_RELEASE_TAGS) + [
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

    @staticmethod
    def _release_url_priority(
        url: str, base_ver: str, exc_kws: list[str]
    ) -> int | None:
        """Ranks an auto-redirected release URL, or rejects it."""
        lowered = url.lower()
        if any(k in lowered for k in exc_kws):
            return None

        if not _has_version_token(
            lowered, base_ver.replace(".", "-")
        ):
            return None

        return _release_priority(url, "", base_ver)

    def _find_release(self, ctx: Context) -> str | None:
        ver_match = re.search(r"(v?\d+(?:\.\d+)+)", ctx.target_ver, re.IGNORECASE)
        base_ver = ver_match.group(1) if ver_match else ctx.target_ver

        queries = self._get_search_queries(ctx, base_ver)
        exc_kws, inc_kws = self._get_filter_kws(ctx)
        total = len(queries)
        beta_fallback = None

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
                redirect_url = str(resp.url)
                priority = self._release_url_priority(
                    redirect_url, base_ver, exc_kws
                )
                if priority is None:
                    print(
                        "[WARN] Auto-redirect pointed to an excluded "
                        "release; ignoring.",
                        flush=True,
                    )
                    continue

                print("[INFO] Auto-redirected to release page.", flush=True)
                if priority == 0:
                    return redirect_url
                if beta_fallback is None:
                    beta_fallback = redirect_url
                continue

            soup = BeautifulSoup(resp.text, "html.parser")
            candidates: list[tuple[int, str]] = []
            for link in soup.find_all("a", class_="fontBlack"):
                valid_url = self._is_valid_release_link(
                    link, base_ver, exc_kws, inc_kws
                )
                if not valid_url:
                    continue

                priority = _release_priority(
                    link.get("href", ""), link.text, base_ver
                )
                candidates.append((priority, valid_url))

            if not candidates:
                continue

            # Stable, plain uploads always win over beta uploads. The site
            # order is only a tiebreaker, so a newer beta upload cannot
            # shadow an existing plain release.
            candidates.sort(key=lambda item: item[0])
            priority, best_url = candidates[0]
            print(
                f"[INFO] Matched release: {_short_url(best_url)}",
                flush=True,
            )
            if priority == 0:
                return best_url
            if beta_fallback is None:
                beta_fallback = best_url

        if beta_fallback:
            print(
                f"[INFO] Falling back to upload: "
                f"{_short_url(beta_fallback)}",
                flush=True,
            )
        return beta_fallback

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
        ctx.source_url = dl_page
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
                ctx=ctx,
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

        target_forms = {
            target_arch,
            target_arch.replace("_", "-"),
            target_arch.replace("-", "_"),
        }
        if any(_is_arch_token(text, form) for form in target_forms):
            return True
        if _is_arch_token(text, "universal") or _is_arch_token(text, "noarch"):
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
    def _variant_preference_index(
        text: str, preferences: Any
    ) -> int | None:
        """Returns the first configured variant preference matching *text*.

        Preferences are ordered most- to least-preferred. Each entry may
        constrain ``arch``, ``android``, and/or ``dpi``; every supplied
        field must match. Malformed entries are ignored.
        """
        if not isinstance(preferences, list):
            return None

        lowered = text.lower()
        for index, pref in enumerate(preferences):
            if not isinstance(pref, dict):
                continue

            arch = str(pref.get("arch") or "").strip()
            android = str(pref.get("android") or "").strip().lower()
            dpi = str(pref.get("dpi") or "").strip().lower()
            if not (arch or android or dpi):
                continue
            if arch and not _is_arch_token(lowered, arch):
                continue
            if android and android not in lowered:
                continue
            if dpi and dpi not in lowered:
                continue
            return index
        return None

    @classmethod
    def _order_rows_by_preference(
        cls, rows: list[Any], preferences: Any
    ) -> list[Any]:
        """Orders variant rows by configured preference, then site order.

        Rows that match no preference keep their original relative order
        after every preferred row, so an unconfigured or partially
        available list still behaves like the previous DOM-order scan.
        """
        if not isinstance(preferences, list) or not preferences:
            return rows

        def sort_key(item: tuple[int, Any]) -> tuple[bool, int, int]:
            index, row = item
            priority = cls._variant_preference_index(
                row.get_text(" ", strip=True), preferences
            )
            return (
                priority is None,
                priority if priority is not None else 0,
                index,
            )

        ordered = sorted(enumerate(rows), key=sort_key)
        return [row for _, row in ordered]

    @staticmethod
    def _is_bundle_row(row: Any) -> bool:
        """Identifies APKM bundles strictly via APKMirror's badge elements."""
        badges = row.find_all(
            "span", class_=re.compile(r"badge", re.IGNORECASE)
        )
        for badge in badges:
            if "BUNDLE" in badge.text.upper() or "APKM" in badge.text.upper():
                return True
        return False

    @staticmethod
    def _row_matches(
        ctx: Context,
        text: str,
        opts: dict,
        is_bundle: bool,
        ver_code: str,
    ) -> bool:
        """Returns True when the row satisfies pass, arch, and version filters."""
        pass_idx = opts.get("pass_idx", 1)
        force_b = opts.get("force_b", False)
        is_mismatch = not is_bundle if force_b else is_bundle

        if pass_idx in (1, 2) and is_mismatch:
            return False
        if pass_idx in (3, 4) and not is_mismatch:
            return False
        if pass_idx in (1, 3) and ver_code and ver_code not in text:
            return False

        target_android = str(ctx.app_data.get("apkm_android", "")).lower()
        if target_android and target_android not in text:
            return False

        target_dpi = str(ctx.app_data.get("apkm_dpi", "")).lower()
        if target_dpi and target_dpi not in text:
            return False

        return ApkmirrorScraper._is_arch_match(
            text, ctx.arch.lower(), pass_idx
        )

    @staticmethod
    def _log_variant_codes(
        ctx: Context, row: Any, ver_code: str, is_bundle: bool
    ) -> None:
        """Logs expected vs detected version codes for the matched row."""
        row_text = row.get_text(" ", strip=True)
        detected_codes = ApkmirrorScraper._extract_version_codes(row_text)
        expected = ver_code or "<not specified>"

        if ver_code and ver_code in row_text.lower():
            status = "MATCH"
        elif ver_code:
            status = "MISMATCH"
        else:
            status = "candidate"

        print(
            f"[INFO] Variant {status}: "
            f"expected_code={expected} "
            f"detected_codes={detected_codes or ['?']} "
            f"arch={ctx.arch} bundle={is_bundle}",
            flush=True,
        )

        if status == "MISMATCH":
            print(
                f"[WARN] Row detected codes {detected_codes} do NOT "
                f"contain expected {ver_code}. The APK may not match "
                f"your JSON version code exactly.",
                flush=True,
            )

    def _extract_row(
        self, ctx: Context, row: Any, opts: dict
    ) -> str | None:
        text = row.get_text(" ", strip=True).lower()
        ver_code = str(opts.get("ver_code") or "").lower()
        is_bundle = self._is_bundle_row(row)

        if not self._row_matches(ctx, text, opts, is_bundle, ver_code):
            return None

        link = row.find("a", class_="accent_color")
        if not link:
            link = row.find("a", href=re.compile(r"-release/"))
        if not link:
            return None

        self._log_variant_codes(ctx, row, ver_code, is_bundle)

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
        preferences = ctx.app_data.get("apkm_variants")
        rows = self._order_rows_by_preference(rows, preferences)
        if isinstance(preferences, list) and preferences:
            print(
                "[INFO] Ordered variants by configured preference "
                f"({len(preferences)} rule(s)).",
                flush=True,
            )

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
                    link = row.find("a", href=re.compile(r"-release/"))
                if not link:
                    continue

                href = link.get("href", "")
                if href.endswith("-release/") or href.endswith("-release"):
                    continue

                out = self._extract_row(ctx, row, opts)
                if out:
                    if ver_code:
                        print(
                            f"[INFO] Download complete for version "
                            f"code {ver_code}.",
                            flush=True,
                        )
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
        ver_code = _resolve_ver_code(ctx.app_data, ctx.arch)

        self._log_target_info(ctx, ver_code)

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

    @staticmethod
    def _log_target_info(ctx: Context, ver_code: str | None) -> None:
        """Logs the target package, version, arch, and expected version code."""
        print(f"[INFO] Target package: {ctx.pkg}", flush=True)
        print(f"[INFO] Target version: {ctx.target_ver}", flush=True)
        print(f"[INFO] Target arch: {ctx.arch}", flush=True)
        print(
            f"[INFO] Expected version code (from JSON): "
            f"{ver_code or '<missing>'}",
            flush=True,
        )

        if not ver_code:
            print(
                "[WARN] No version code found in app_data.version_codes "
                f"for arch '{ctx.arch}'. Cannot verify exact build.",
                flush=True,
            )
