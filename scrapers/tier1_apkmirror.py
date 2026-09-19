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

    def _get_filter_kws(self, ctx: Context) -> tuple[list[str], list[str]]:
        """Extracts excluded and included keywords from app data."""
        exc_kws = ["secondary"] + [
            k.lower() for k in ctx.app_data.get("apkm_exclude", []) if k.strip()
        ]
        inc_kws = [k.lower() for k in ctx.app_data.get("apkm_include", []) if k.strip()]
        return exc_kws, inc_kws

    def _get_search_queries(self, ctx: Context, base_ver: str) -> list[str]:
        """Builds a list of search queries based on app data and target version."""
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

        return list(dict.fromkeys([
            ctx.pkg,
            search_term,
            f"{search_term} {base_ver}",
            short_term
        ]))

    def _find_release(self, ctx: Context) -> Optional[str]:
        """Finds the release page URL for the target version."""
        base_ver = (
            ctx.target_ver.split("-")[0]
            if "-" in ctx.target_ver and ctx.target_ver[:1].isdigit()
            else ctx.target_ver
        )

        queries = self._get_search_queries(ctx, base_ver)
        exc_kws, inc_kws = self._get_filter_kws(ctx)

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

    def _get_download_buttons(self, soup: BeautifulSoup) -> list[Any]:
        """Extracts potential download button elements from variant page."""
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

    def _pick_variant_button(self, btns: list[Any], is_bundle: bool) -> Any:
        """Picks the best variant button depending on bundle requirement."""
        for btn in btns:
            has_force = "forcebaseapk" in btn["href"].lower()
            if (is_bundle and not has_force) or (not is_bundle and has_force):
                return btn
        return btns[0]

    def _get_final_download_link(self, d_soup: BeautifulSoup) -> Optional[Any]:
        """Locates the final download link from the download page."""
        dl_btn = d_soup.find("a", id="download-link")
        if dl_btn:
            return dl_btn

        return d_soup.find(
            lambda tag: tag.name == "a" and tag.has_attr("href") and (
                "download.php" in tag["href"] or "/download/?key=" in tag["href"]
            )
        )

    def _process_variant_page(
        self, ctx: Context, var_url: str, is_bundle: bool
    ) -> Optional[str]:
        """Processes the specific variant page to find the final download link."""
        ctx.limiter.wait()
        v_resp = ctx.scraper.get(var_url, timeout=60)

        if _is_waf_blocked(v_resp.status_code, v_resp.text) or v_resp.status_code != 200:
            print(f"[WARN] APKMirror variant page failed (HTTP {v_resp.status_code}).")
            return None

        v_soup = BeautifulSoup(v_resp.text, "html.parser")
        btns = self._get_download_buttons(v_soup)
        if not btns:
            print("[WARN] Download button not found on variant page.")
            return None

        btn = self._pick_variant_button(btns, is_bundle)

        if not is_bundle:
            self._log_expected_sha256(v_soup)

        file_type = "APKM Bundle" if is_bundle else "Raw APK"
        print(f"[INFO] Preparing to extract: {file_type}")

        dl_page = urljoin("https://www.apkmirror.com", btn["href"])
        ctx.limiter.wait()
        d_resp = ctx.scraper.get(dl_page, timeout=60)

        if _is_waf_blocked(d_resp.status_code, d_resp.text) or d_resp.status_code != 200:
            print(f"[WARN] APKMirror download page failed (HTTP {d_resp.status_code}).")
            return None

        d_soup = BeautifulSoup(d_resp.text, "html.parser")
        dl_btn = self._get_final_download_link(d_soup)

        if not dl_btn or not dl_btn.has_attr("href"):
            print("[WARN] Final download link not found on APKMirror.")
            return None

        out_path = ctx.get_out_path(".apkm" if is_bundle else ".apk")
        dl_url = urljoin("https://www.apkmirror.com", dl_btn["href"])
        print(f"[INFO] Downloading {file_type} from APKMirror...")

        if download_file_stream(ctx.scraper, dl_url, out_path, dl_page):
            return out_path
        return None

    def _is_arch_match(self, text: str, target_arch: str, pass_idx: int) -> bool:
        """Helper to determine if the table row matches the requested architecture."""
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

    def _extract_row(
        self, ctx: Context, row: Any, opts: dict
    ) -> Optional[str]:
        """Extracts the variant URL from a table row if it matches criteria."""
        text = row.text.lower()
        pass_idx = opts.get("pass_idx", 1)
        force_b = opts.get("force_b", False)
        ver_code = opts.get("ver_code", "")

        is_bundle = "bundle" in text
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
        """Downloads the matching variant from the release page."""
        ctx.limiter.wait()
        resp = ctx.scraper.get(rel_url, timeout=60)

        if _is_waf_blocked(resp.status_code, resp.text) or resp.status_code != 200:
            print(f"[WARN] APKMirror release page failed (HTTP {resp.status_code}).")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.find_all("div", class_="table-row")

        if rows:
            for pass_idx in (1, 2, 3):
                opts = {"force_b": force_b, "ver_code": ver_code, "pass_idx": pass_idx}
                for row in rows:
                    out = self._extract_row(ctx, row, opts)
                    if out:
                        return out

            print("[WARN] No matching variants found in release table.")
            return None

        dl_btn = soup.find(
            lambda tag: tag.name == "a" and "downloadButton" in tag.get("class", [])
        )

        if dl_btn:
            is_bundle = "bundle" in dl_btn.text.lower()
            return self._process_variant_page(ctx, rel_url, is_bundle)

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
        except (requests.exceptions.RequestException, OSError) as err:
            print(f"[ERROR] Tier 1 failed: {err}")
        return None
