"""Tier 1 Scraper: APKMirror."""

import re
from typing import Any, Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

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

    def _is_valid_release_link(
        self, link: Any, base_ver: str, exc_kws: list, inc_kws: list
    ) -> Optional[str]:
        """Validates if a release link matches the required criteria strictly."""
        href = link.get("href", "")
        if not href or EDITION_SLUG_REGEX.search(href):
            return None

        text = link.text.lower()
        href_ver = base_ver.replace(".", "-")

        has_ver_text = base_ver.lower() in text
        has_ver_href = href_ver.lower() in href.lower()

        if not has_ver_text and not has_ver_href:
            return None

        if any(k in text for k in exc_kws):
            return None

        if inc_kws and not all(k in text for k in inc_kws):
            return None

        return urljoin("https://www.apkmirror.com", href)

    def _find_release(self, ctx: Context) -> Optional[str]:
        """Finds the release page URL for the target version."""
        base_ver = (
            ctx.target_ver.split("-")[0]
            if "-" in ctx.target_ver and ctx.target_ver[:1].isdigit()
            else ctx.target_ver
        )
        search_term = ctx.app_data.get("search_term", ctx.pkg)

        if "." in search_term and " " not in search_term:
            short_term = search_term
        else:
            short_term = (
                search_term.replace(" Browser", "")
                .replace(" App", "")
                .split("-")[0]
                .strip()
            )

        exc_kws = ["secondary"] + [
            k.lower() for k in ctx.app_data.get("apkm_exclude", []) if k.strip()
        ]
        inc_kws = [k.lower() for k in ctx.app_data.get("apkm_include", []) if k.strip()]

        queries = list(dict.fromkeys([
            ctx.pkg,
            search_term,
            f"{search_term} {base_ver}",
            short_term
        ]))

        for query in queries:
            ctx.limiter.wait()
            url = f"https://www.apkmirror.com/?post_type=app_release&s={quote_plus(query)}"
            resp = ctx.scraper.get(url, timeout=60)

            if _is_waf_blocked(resp.status_code, resp.text) or resp.status_code != 200:
                continue

            # Handle automatic redirection directly to a release page
            if "?post_type=app_release" not in resp.url and "-release/" in resp.url:
                print("[INFO] Auto-redirected to release page.")
                return resp.url

            soup = BeautifulSoup(resp.text, "html.parser")
            for link in soup.find_all("a", class_="fontBlack"):
                valid_url = self._is_valid_release_link(link, base_ver, exc_kws, inc_kws)
                if valid_url:
                    return valid_url

        return None

    def _log_expected_sha256(self, soup: BeautifulSoup) -> None:
        """Extracts and logs the expected SHA-256 hash from the variant page."""
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
        """Selects the best download button from the variant page."""
        btns = [
            btn for btn in soup.find_all("a", class_="downloadButton")
            if "variantsButton" not in btn.get("class", []) and btn.has_attr("href")
            and not btn["href"].startswith("#")
        ]
        if not btns:
            return None

        best_btn = None
        best_score = -100

        for btn in btns:
            text = btn.text.lower()
            score = 0
            is_bundle_btn = "bundle" in text

            if force_b and not is_bundle_btn:
                score -= 50
            elif not force_b and is_bundle_btn:
                score -= 50
            else:
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
        """Processes the specific variant page to find the final download link."""
        ctx.limiter.wait()
        v_resp = ctx.scraper.get(var_url, timeout=60)
        if _is_waf_blocked(v_resp.status_code, v_resp.text) or v_resp.status_code != 200:
            return None

        v_soup = BeautifulSoup(v_resp.text, "html.parser")
        btn = self._select_download_button(v_soup, force_b)
        if not btn:
            return None

        is_actual_bundle = "bundle" in btn.text.lower()
        if not is_actual_bundle:
            self._log_expected_sha256(v_soup)

        file_type = "APKM Bundle" if is_actual_bundle else "Raw APK"
        print(f"[INFO] Preparing to extract: {file_type}")

        dl_page = urljoin("https://www.apkmirror.com", btn["href"])
        ctx.limiter.wait()
        d_resp = ctx.scraper.get(dl_page, timeout=60)
        if _is_waf_blocked(d_resp.status_code, d_resp.text) or d_resp.status_code != 200:
            return None

        dl_btn = BeautifulSoup(d_resp.text, "html.parser").find("a", {"rel": "nofollow"})
        if dl_btn and "href" in dl_btn.attrs:
            out_path = ctx.get_out_path(".apkm" if is_actual_bundle else ".apk")
            dl_url = urljoin("https://www.apkmirror.com", dl_btn["href"])
            print(f"[INFO] Downloading {file_type} from APKMirror...")
            if download_file_stream(ctx.scraper, dl_url, out_path, dl_page):
                return out_path
        return None

    def _extract_row(
        self, ctx: Context, row: Any, force_b: bool, ver_code: str, strict: bool = True
    ) -> Optional[str]:
        """Extracts the variant URL from a table row if it matches criteria."""
        text = row.text.lower()
        is_bundle = "bundle" in text

        # Reject formats based on bundle preference strictly
        if strict and ((force_b and not is_bundle) or (not force_b and is_bundle)):
            return None

        target_arch = ctx.arch.lower()
        is_multi_arm = "arm64-v8a" in text and "armeabi-v7a" in text

        # Base architecture match logic
        arch_match = (
            target_arch in text
            or "universal" in text
            or "noarch" in text
            or not any(a in text for a in ("arm64-v8a", "armeabi-v7a", "x86", "x86_64", "armeabi"))
        )

        # Handling for universal architecture targets
        if target_arch == "universal":
            if is_multi_arm:
                arch_match = True
            elif not strict and ("arm64-v8a" in text or "armeabi-v7a" in text):
                arch_match = True

        # EXCEPTION: If the target is a specific ARM architecture (e.g., arm64-v8a),
        # but the APKMirror file is a combined multi-ARM package (arm64-v8a + armeabi-v7a),
        # treat it as a match because it contains the target architecture.
        elif is_multi_arm and target_arch in ("arm64-v8a", "armeabi-v7a"):
            arch_match = True

        # Process valid URLs
        if arch_match and (not ver_code or str(ver_code).lower() in text):
            link = row.find("a", class_="accent_color")
            if link:
                return self._process_variant_page(
                    ctx, urljoin("https://www.apkmirror.com", link["href"]), force_b
                )

        return None

    def _download_variant(
        self, ctx: Context, rel_url: str, ver_code: str, force_b: bool
    ) -> Optional[str]:
        """Downloads the matching variant from the release page."""
        ctx.limiter.wait()
        resp = ctx.scraper.get(rel_url, timeout=60)
        if _is_waf_blocked(resp.status_code, resp.text) or resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Look for standard variant rows
        rows = soup.find_all("div", class_="table-row")

        if rows:
            # Pass 1: Strict match (Preferred format only)
            for row in rows:
                out = self._extract_row(ctx, row, force_b, ver_code, strict=True)
                if out:
                    return out

            # Pass 2: Fallback match (Accept any available format)
            for row in rows:
                out = self._extract_row(ctx, row, force_b, ver_code, strict=False)
                if out:
                    return out

        # Fallback for single-variant pages where the download button is present
        # but there is no "table-row" variants list.
        else:
            # Look for ANY anchor tag that contains 'downloadButton' as part of its class list
            dl_btn = soup.find(
                lambda tag: tag.name == "a" and "downloadButton" in tag.get("class", [])
            )

            if dl_btn:
                # If found, the current page IS the variant page
                return self._process_variant_page(ctx, rel_url, force_b)

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
