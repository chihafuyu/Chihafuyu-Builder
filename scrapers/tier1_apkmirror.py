"""Tier 1 Scraper: APKMirror with Dynamic Stealth Rotation to Bypass WAF."""

import random
import re
import time
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from core.context import Context
from core.utils import _is_waf_blocked, download_file_stream
from .base import BaseScraper


EDITION_SLUG_REGEX = re.compile(
    r"\b(amazon|fire-tablet|fire-tv|androidtv|wear|go-edition|"
    r"lite|enterprise|kids|headunit|auto)\b",
    re.IGNORECASE
)


class _CffiResponseContext:
    """Context manager wrapper for curl_cffi Response to support 'with' statements."""

    def __init__(self, resp: Any) -> None:
        self.resp = resp

    def __enter__(self) -> Any:
        original_iter = getattr(self.resp, "iter_content", None)
        if original_iter:
            def safe_iter_content(*args: Any, **kwargs: Any) -> Any:
                try:
                    return original_iter(*args, **kwargs)
                except TypeError:
                    return original_iter()
            self.resp.iter_content = safe_iter_content
        return self.resp

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if hasattr(self.resp, "close"):
            self.resp.close()


class _CffiSessionWrapper:
    """Wraps curl_cffi Session to ensure compatibility with generic requests downloaders."""

    def __init__(self, session: Any) -> None:
        self.session = session

    def get(self, *args: Any, **kwargs: Any) -> Any:
        """Executes GET request while intercepting incompatible kwargs and fake streams."""
        if "timeout" in kwargs and isinstance(kwargs["timeout"], tuple):
            kwargs["timeout"] = kwargs["timeout"][-1] if kwargs["timeout"] else None

        resp = self.session.get(*args, **kwargs)

        if kwargs.get("stream"):
            c_type = resp.headers.get("Content-Type", "").lower()
            if "text/html" in c_type:
                print("[WARN] Stream returned HTML. WAF trap or expired token detected.")
                resp.status_code = 403

        # Cloudscraper returns requests.Response which doesn't need context wrapper
        mod_name = getattr(self.session, "__module__", "")
        if hasattr(resp, "iter_content") and not mod_name.startswith("curl_cffi"):
            return resp

        return _CffiResponseContext(resp)

    def close(self) -> None:
        """Closes the underlying curl_cffi session gracefully."""
        self.session.close()

    def __getattr__(self, item: str) -> Any:
        """Delegates unknown attribute lookups to the underlying session."""
        return getattr(self.session, item)


class ApkmirrorScraper(BaseScraper):
    """Scrapes APKs from APKMirror handling WAF, variants, and dynamic rate limits."""

    def __init__(self) -> None:
        super().__init__()
        self._current_profile_idx = 0
        # High success TLS profiles
        self._profiles = ["chrome", "safari_ios", "safari"]

        self.session = cffi_requests.Session(
            impersonate=self._profiles[self._current_profile_idx]
        )
        self._last_url = "https://www.apkmirror.com/"
        self._use_cloudscraper = False

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "apkmirror"

    def _rotate_session(self) -> None:
        """Closes current session and rotates the impersonation profile to evade WAF."""
        self.session.close()

        if self._use_cloudscraper:
            import cloudscraper  # pylint: disable=import-outside-toplevel
            self.session = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True}
            )
        else:
            self._current_profile_idx = (self._current_profile_idx + 1) % len(self._profiles)
            self.session = cffi_requests.Session(
                impersonate=self._profiles[self._current_profile_idx]
            )
        self._last_url = "https://www.apkmirror.com/"

    def _safe_get(self, ctx: Context, url: str) -> Optional[Any]:
        ctx.limiter.wait()
        time.sleep(random.uniform(2.5, 4.5))

        for attempt in range(4):
            try:
                # Fallback to cloudscraper on final attempt if curl_cffi fails completely
                if attempt == 3 and not self._use_cloudscraper:
                    print("[INFO] curl_cffi failed to bypass. Falling back to cloudscraper...")
                    self._use_cloudscraper = True
                    self._rotate_session()

                # Referer chaining
                headers = {
                    "Referer": getattr(self, "_last_url", "https://www.apkmirror.com/")
                }

                resp = self.session.get(url, timeout=30, headers=headers)
                text = resp.text.lower()

                # Robust WAF detection via title tag
                t_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
                title = t_match.group(1).strip() if t_match else ""

                is_blocked = (
                    resp.status_code in (403, 429, 503) or
                    "apkmirror" not in title or
                    "too many requests" in text or
                    "ad blocker" in text or
                    "verify you are human" in text or
                    "ray id" in text or
                    "attention required" in text or
                    "security check" in text or
                    "just a moment" in text or
                    "challenges.cloudflare.com" in text or
                    "cf-turnstile" in text or
                    "checking your browser" in text or
                    ("cloudflare" in text and "enable javascript" in text)
                )

                if is_blocked or _is_waf_blocked(resp.status_code, text):
                    print(
                        f"[WARN] WAF/Block (HTTP {resp.status_code}). "
                        f"Rotating profile and backing off (attempt {attempt + 1}/4)..."
                    )
                    self._rotate_session()
                    time.sleep(random.uniform(8.0, 12.0))
                    continue

                if resp.status_code == 200:
                    self._last_url = str(resp.url)
                    return resp

            except Exception as err:  # pylint: disable=broad-except
                print(f"[WARN] Request failed: {err}")
                self._rotate_session()
                time.sleep(random.uniform(4.0, 7.0))

        return None

    @staticmethod
    def _is_valid_release_link(
        link: Any, base_ver: str, exc_kws: list[str], inc_kws: list[str]
    ) -> Optional[str]:
        href = link.get("href", "")
        if not href or EDITION_SLUG_REGEX.search(href):
            return None

        text = link.text.lower()
        href_ver = base_ver.replace(".", "-")

        has_ver_text = base_ver.lower() in text
        has_ver_href = href_ver.lower() in href.lower()

        if not has_ver_text and not has_ver_href:
            major_ver = base_ver.split(".")[0]
            if not major_ver:
                return None

            # Ensure word boundaries so e.g. '1' doesn't blindly match '12' or '21'
            major_pat = rf"\b{re.escape(major_ver)}\b"
            if not (re.search(major_pat, text) or re.search(major_pat, href)):
                return None

        if any(k in text for k in exc_kws):
            return None

        if inc_kws and not all(k in text for k in inc_kws):
            return None

        return urljoin("https://www.apkmirror.com", href)

    @staticmethod
    def _get_filter_kws(ctx: Context) -> tuple[list[str], list[str]]:
        exc_kws = ["secondary"] + [
            k.lower() for k in ctx.app_data.get("apkm_exclude", []) if k.strip()
        ]
        inc_kws = [k.lower() for k in ctx.app_data.get("apkm_include", []) if k.strip()]
        return exc_kws, inc_kws

    @staticmethod
    def _get_search_queries(ctx: Context, base_ver: str) -> list[str]:
        search_term = ctx.app_data.get("search_term", ctx.pkg)

        short_term = search_term.replace(" Browser", "").replace(" App", "").strip()
        if "." in short_term and " " not in short_term:
            parts = short_term.split(".")
            short_term = parts[-1] if len(parts[-1]) > 3 else parts[-2]

        short_term = short_term.split("-")[0].strip()

        return list(dict.fromkeys([
            ctx.pkg,
            search_term,
            f"{search_term} {base_ver}",
            f"{short_term} {base_ver}",
            short_term
        ]))

    def _find_release(self, ctx: Context) -> Optional[str]:
        base_ver = (
            ctx.target_ver.split("-")[0]
            if "-" in ctx.target_ver and ctx.target_ver[:1].isdigit()
            else ctx.target_ver
        )

        queries = self._get_search_queries(ctx, base_ver)
        exc_kws, inc_kws = self._get_filter_kws(ctx)

        for query in queries:
            url = f"https://www.apkmirror.com/?post_type=app_release&s={quote_plus(query)}"
            resp = self._safe_get(ctx, url)

            if not resp:
                continue

            if "?post_type=app_release" not in resp.url and "-release/" in resp.url:
                print("[INFO] Auto-redirected to release page.")
                return str(resp.url)

            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.find_all("a", class_="fontBlack"):
                valid_url = self._is_valid_release_link(link, base_ver, exc_kws, inc_kws)
                if valid_url:
                    return valid_url

        return None

    @staticmethod
    def _log_expected_sha256(soup: BeautifulSoup) -> None:
        modal = soup.select_one("#safeDownload .modal-body, .safeDownload .modal-body")
        if not modal:
            return
        block_text = modal.text
        if "APK file hashes" in block_text and "APK certificate fingerprints" in block_text:
            file_section = block_text.split("APK file hashes")[1]
            file_section = file_section.split("APK certificate fingerprints")[0]
            hash_match = re.search(r"[0-9a-fA-F]{64}", file_section)
            if hash_match:
                print(f"[INFO] Expected SHA-256 extracted: {hash_match.group(0)}")

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
            if (is_bundle and not has_force) or (not is_bundle and has_force):
                return btn
        return btns[0]

    @staticmethod
    def _get_final_download_link(d_soup: BeautifulSoup) -> Optional[Any]:
        dl_btn = d_soup.find("a", id="download-link")
        if dl_btn and dl_btn.has_attr("href"):
            return dl_btn

        return d_soup.find(
            lambda tag: tag.name == "a" and tag.has_attr("href") and (
                "download.php" in tag["href"] or "/download/?key=" in tag["href"]
            )
        )

    def _process_variant_page(
        self, ctx: Context, var_url: str, is_bundle: bool
    ) -> Optional[str]:
        v_resp = self._safe_get(ctx, var_url)
        if not v_resp:
            print("[WARN] APKMirror variant page failed.")
            time.sleep(random.uniform(4.0, 7.0))
            return None

        v_soup = BeautifulSoup(v_resp.text, "html.parser")

        # Secondary Turnstile check
        if v_soup.find("div", id="turnstile-wrapper") or "challenges.cloudflare.com" in v_resp.text:
            print("[WARN] Turnstile challenge detected on variant page!")
            return None

        btns = self._get_download_buttons(v_soup)
        if not btns:
            print("[WARN] Download button not found on variant page. Possible WAF block.")
            return None

        btn = self._pick_variant_button(btns, is_bundle)

        if not is_bundle:
            self._log_expected_sha256(v_soup)

        p_type = 'APKM Bundle' if is_bundle else 'Raw APK'
        print(f"[INFO] Preparing to extract: {p_type}")

        dl_page = urljoin("https://www.apkmirror.com", btn["href"])
        d_resp = self._safe_get(ctx, dl_page)

        if not d_resp:
            print("[WARN] APKMirror download page failed.")
            return None

        dl_btn = self._get_final_download_link(BeautifulSoup(d_resp.text, "html.parser"))

        out_path = None
        if dl_btn and dl_btn.has_attr("href"):
            out_path = ctx.get_out_path(".apkm" if is_bundle else ".apk")
            print(f"[INFO] Downloading {p_type} from APKMirror...")
            time.sleep(random.uniform(3.0, 5.0))
            if not download_file_stream(
                _CffiSessionWrapper(self.session),
                urljoin("https://www.apkmirror.com", dl_btn["href"]),
                out_path,
                dl_page
            ):
                out_path = None
        else:
            print("[WARN] Final download link not found on APKMirror.")

        return out_path

    @staticmethod
    def _is_arch_match(text: str, target_arch: str, pass_idx: int) -> bool:
        if target_arch in text or "universal" in text or "noarch" in text:
            return True

        arch_list = ("arm64-v8a", "armeabi-v7a", "x86", "x86_64", "armeabi")
        if not any(a in text for a in arch_list):
            return True

        is_multi_arm = "arm64-v8a" in text and "armeabi-v7a" in text
        if is_multi_arm and target_arch in ("arm64-v8a", "armeabi-v7a", "universal"):
            return True

        if pass_idx == 3 and target_arch == "universal" and (
            "arm64-v8a" in text or "armeabi-v7a" in text
        ):
            return True

        return False

    @staticmethod
    def _is_bundle_row(row: Any) -> bool:
        badges = row.find_all("span", class_=re.compile(r"badge", re.IGNORECASE))
        for badge in badges:
            if "BUNDLE" in badge.text.upper() or "APKM" in badge.text.upper():
                return True
        return "bundle" in row.text.lower()

    def _extract_row(
        self, ctx: Context, row: Any, opts: dict
    ) -> Optional[str]:
        text = row.text.lower()
        pass_idx = opts.get("pass_idx", 1)
        force_b = opts.get("force_b", False)
        ver_code = opts.get("ver_code", "")

        is_bundle = self._is_bundle_row(row)
        if pass_idx in (1, 2) and force_b != is_bundle:
            return None

        if pass_idx == 1 and ver_code and str(ver_code).lower() not in text:
            return None

        if not self._is_arch_match(text, ctx.arch.lower(), pass_idx):
            return None

        link = row.find("a", class_="accent_color")
        if not link:
            return None

        return self._process_variant_page(
            ctx, urljoin("https://www.apkmirror.com", link["href"]), is_bundle
        )

    def _download_variant(
        self, ctx: Context, rel_url: str, ver_code: str, force_b: bool
    ) -> Optional[str]:
        resp = self._safe_get(ctx, rel_url)
        if not resp:
            print("[WARN] APKMirror release page failed.")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Isolate variants table to avoid parsing 'Related Releases'
        v_table = soup.find("div", class_=re.compile(r"variants-table", re.IGNORECASE))
        rows = v_table.find_all("div", class_="table-row") if v_table else soup.find_all(
            "div", class_="table-row"
        )

        if rows:
            for pass_idx in (1, 2, 3):
                opts = {"force_b": force_b, "ver_code": ver_code, "pass_idx": pass_idx}
                for row in rows:
                    # Safeguard against false positive release links
                    link = row.find("a", class_="accent_color")
                    if link and "-release/" in link.get("href", ""):
                        continue

                    out = self._extract_row(ctx, row, opts)
                    if out:
                        return out

            print("[WARN] No matching variants found in release table.")
            return None

        dl_btn = soup.find(
            lambda tag: tag.name == "a"
            and "downloadButton" in tag.get("class", [])
            and "variantsButton" not in tag.get("class", [])
        )

        if dl_btn:
            return self._process_variant_page(ctx, rel_url, "bundle" in dl_btn.text.lower())

        print("[WARN] Release table and fallback download button both missing.")
        return None

    def scrape(self, ctx: Context) -> Optional[str]:
        """Executes the scraping process from APKMirror."""
        print(f"[TIER 1] APKMirror: v{ctx.target_ver}")
        ver_code = ctx.app_data.get("version_codes", {}).get(ctx.arch)
        try:
            rel_url = self._find_release(ctx)
            if not rel_url:
                print("[WARN] Release not found.")
                return None
            return self._download_variant(
                ctx, rel_url, ver_code, ctx.app_data.get("force_bundle", False)
            )
        except Exception as err:  # pylint: disable=broad-except
            print(f"[ERROR] Tier 1 failed: {err}")
        return None
