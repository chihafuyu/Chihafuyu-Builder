"""Tier 1 Scraper: APKMirror."""

import re
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
import requests

from core.context import Context
from core.utils import _is_waf_blocked, download_file_stream
from .base import BaseScraper

EDITION_SLUG_REGEX = re.compile(
    r"\b(amazon|fire-tablet|fire-tv|androidtv|wear|go-edition|"
    r"lite|enterprise|kids|headunit|auto)\b",
    re.IGNORECASE
)

class ApkmirrorScraper(BaseScraper):
    """Scrapes APKs from APKMirror handling WAF and variants."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "apkmirror"

    def _is_valid_release_link(self, link: Any, base_ver: str, ctx: Context) -> Optional[str]:
        """Validates if a release link matches the required criteria."""
        href = link.get("href", "")
        if not href or EDITION_SLUG_REGEX.search(href):
            return None

        text = link.text.lower()
        href_ver = base_ver.replace(".", "-")
        exc_kws = ["secondary"] + [
            k.lower() for k in ctx.app_data.get("apkm_exclude", [])
        ]
        inc_kws = [k.lower() for k in ctx.app_data.get("apkm_include", [])]

        if base_ver.lower() not in text and href_ver not in href.lower():
            return None
        if any(k in text for k in exc_kws):
            return None
        if inc_kws and not all(k in text for k in inc_kws):
            return None

        return urljoin("https://www.apkmirror.com", href)

    def _find_release(self, ctx: Context) -> Optional[str]:
        t_ver = ctx.target_ver
        base_ver = t_ver.split("-")[0] if "-" in t_ver and t_ver[:1].isdigit() else t_ver
        search_term = ctx.app_data.get('search_term', ctx.pkg)
        short_term = search_term.replace(" Browser", "").replace(" App", "").split("-")[0].strip()

        queries = [
            f"{search_term} {base_ver}",
            f"{short_term} {base_ver}",
            search_term,
            short_term
        ]
        queries = list(dict.fromkeys(queries))

        for q in queries:
            url = f"https://www.apkmirror.com/?post_type=app_release&s={quote_plus(q)}"
            ctx.limiter.wait()
            resp = ctx.scraper.get(url, timeout=60)
            if _is_waf_blocked(resp.status_code, resp.text) or resp.status_code != 200:
                continue

            if "?post_type=app_release&s=" not in resp.url:
                print("[INFO] Auto-redirected to release page.")
                return resp.url

            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.find_all("a", class_="fontBlack"):
                valid_url = self._is_valid_release_link(link, base_ver, ctx)
                if valid_url:
                    return valid_url

        return None

    def _log_expected_sha256(self, soup: BeautifulSoup) -> None:
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

    def _select_download_button(self, soup: BeautifulSoup, force_b: bool) -> Optional[Any]:
        btns = [
            btn for btn in soup.find_all("a", class_="downloadButton")
            if "variantsButton" not in btn.get("class", []) and btn.has_attr("href")
            and not btn["href"].startswith("#")
        ]
        if not btns:
            return None

        best_btn = None
        best_score = -1

        for btn in btns:
            text = btn.text.lower()
            score = 0
            is_bundle_btn = "bundle" in text

            if force_b and is_bundle_btn:
                score += 10
            elif not force_b and not is_bundle_btn:
                score += 10

            if "download" in text:
                score += 5

            if score > best_score:
                best_score = score
                best_btn = btn

        return best_btn

    def _process_variant_page(
        self, ctx: Context, var_url: str, force_b: bool
    ) -> Optional[str]:
        ctx.limiter.wait()
        v_resp = ctx.scraper.get(var_url, timeout=60)
        if (
            _is_waf_blocked(v_resp.status_code, v_resp.text)
            or v_resp.status_code != 200
        ):
            return None

        v_soup = BeautifulSoup(v_resp.text, "html.parser")

        btn = self._select_download_button(v_soup, force_b)
        if not btn:
            return None

        is_actual_bundle = "bundle" in btn.text.lower()
        if not is_actual_bundle:
            self._log_expected_sha256(v_soup)

        file_type_log = "APKM Bundle" if is_actual_bundle else "Raw APK"
        print(f"[INFO] Preparing to extract: {file_type_log}")

        dl_page = urljoin("https://www.apkmirror.com", btn["href"])
        ctx.limiter.wait()
        d_resp = ctx.scraper.get(dl_page, timeout=60)
        if (
            _is_waf_blocked(d_resp.status_code, d_resp.text)
            or d_resp.status_code != 200
        ):
            return None

        dl_btn = BeautifulSoup(d_resp.text, "html.parser").find("a", {"rel": "nofollow"})
        if dl_btn and "href" in dl_btn.attrs:
            out_path = ctx.get_out_path(".apkm" if is_actual_bundle else ".apk")
            dl_url = urljoin("https://www.apkmirror.com", dl_btn["href"])
            print(f"[INFO] Downloading {file_type_log} from APKMirror...")
            if download_file_stream(ctx.scraper, dl_url, out_path, dl_page):
                return out_path
        return None

    def _extract_row(
        self, ctx: Context, row: Any, force_b: bool, ver_code: str
    ) -> Optional[str]:
        text = row.text.lower()
        has_valid = any(
            a in text for a in (ctx.arch.lower(), "universal", "noarch", "nodpi", "anydpi")
        )
        has_any = any(
            a in text
            for a in ("arm64-v8a", "armeabi-v7a", "x86", "x86_64", "armeabi")
        )

        if force_b and "bundle" not in text:
            return None

        if has_valid or not has_any:
            if not ver_code or str(ver_code) in text:
                if link := row.find("a", class_="accent_color"):
                    rel_url = urljoin(
                        "https://www.apkmirror.com", link["href"]
                    )
                    return self._process_variant_page(
                        ctx, rel_url, force_b
                    )
        return None

    def _download_variant(
        self, ctx: Context, rel_url: str, ver_code: str, force_b: bool
    ) -> Optional[str]:
        ctx.limiter.wait()
        resp = ctx.scraper.get(rel_url, timeout=60)
        if _is_waf_blocked(resp.status_code, resp.text) or resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        if rows := soup.find_all("div", class_="table-row"):
            for row in rows:
                if out := self._extract_row(
                    ctx, row, force_b, ver_code
                ):
                    return out
        elif soup.find("a", class_="downloadButton"):
            if out := self._process_variant_page(ctx, rel_url, force_b):
                return out
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
                ctx,
                rel_url,
                ver_code,
                ctx.app_data.get("force_bundle", False),
            )
        except (requests.exceptions.RequestException, OSError) as err:
            print(f"[ERROR] Tier 1 failed: {err}")
        return None
