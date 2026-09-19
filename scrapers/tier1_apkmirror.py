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

    def _score_download_btn(self, btn: Any, force_b: bool) -> int:
        """Calculates a score for a given download button."""
        text = btn.text.lower()
        href = btn.get("href", "").lower()
        score = 0

        is_bundle_btn = "bundle" in text
        is_force_base = "forcebaseapk" in href

        if force_b:
            score += 10 if (is_bundle_btn or is_force_base) else -50
        else:
            if is_force_base:
                score += 20
            elif is_bundle_btn:
                score -= 50
            else:
                score += 10

        if "download" in text:
            score += 5

        return score

    def _select_download_button(self, soup: BeautifulSoup, force_b: bool) -> Optional[Any]:
        """Selects the best download button from the variant page."""
        seen = set()
        best_btn = None
        best_score = -100

        for btn in soup.find_all("a"):
            href = btn.get("href", "")
            classes = btn.get("class", [])

            if isinstance(classes, str):
                classes = [classes]

            if not href or href.startswith("#") or "variantsButton" in classes:
                continue

            if "downloadButton" not in classes and "/download/?key=" not in href:
                continue

            if href in seen:
                continue

            seen.add(href)
            score = self._score_download_btn(btn, force_b)
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
            print(f"[WARN] APKMirror variant page failed (HTTP {v_resp.status_code}).")
            return None

        v_soup = BeautifulSoup(v_resp.text, "html.parser")
        btn = self._select_download_button(v_soup, force_b)
        if not btn:
            print("[WARN] Download button not found on variant page.")
            return None

        is_actual_bundle = "bundle" in btn.text.lower()
        if not is_actual_bundle:
            self._log_expected_sha256(v_soup)

        file_type = "APKM Bundle" if is_actual_bundle else "Raw APK"
        print(f"[INFO] Preparing to extract: {file_type}")

        dl_page = urljoin("https://www.apkmirror.com", btn.get("href", ""))
        ctx.limiter.wait()
        d_resp = ctx.scraper.get(dl_page, timeout=60)

        if _is_waf_blocked(d_resp.status_code, d_resp.text) or d_resp.status_code != 200:
            print(f"[WARN] APKMirror download page failed (HTTP {d_resp.status_code}).")
            return None

        d_soup = BeautifulSoup(d_resp.text, "html.parser")
        dl_btn = d_soup.find("a", id="download-link")

        if not dl_btn:
            dl_btn = d_soup.find(
                lambda tag: tag.name == "a" and tag.has_attr("href") and (
                    "download.php" in tag["href"] or "/download/?key=" in tag["href"]
                )
            )

        if dl_btn and dl_btn.has_attr("href"):
            out_path = ctx.get_out_path(".apkm" if is_actual_bundle else ".apk")
            dl_url = urljoin("https://www.apkmirror.com", dl_btn["href"])
            print(f"[INFO] Downloading {file_type} from APKMirror...")
            if download_file_stream(ctx.scraper, dl_url, out_path, dl_page):
                return out_path
        else:
            print("[WARN] Final download link not found on APKMirror.")

        return None

    def _is_arch_match(self, text: str, target_arch: str, pass_idx: int) -> bool:
        """Helper to determine if the table row matches the requested architecture."""
        if target_arch in text or "universal" in text or "noarch" in text:
            return True

        if not any(a in text for a in ("arm64-v8a", "armeabi-v7a", "x86", "x86_64", "armeabi")):
            return True

        is_multi_arm = "arm64-v8a" in text and "armeabi-v7a" in text

        if target_arch == "universal":
            if is_multi_arm or (pass_idx == 3 and ("arm64-v8a" in text or "armeabi-v7a" in text)):
                return True

        if is_multi_arm and target_arch in ("arm64-v8a", "armeabi-v7a"):
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

        if pass_idx in (1, 2):
            is_bundle = "bundle" in text
            if (force_b and not is_bundle) or (not force_b and is_bundle):
                return None

        if pass_idx == 1 and ver_code and str(ver_code).lower() not in text:
            return None

        if self._is_arch_match(text, ctx.arch.lower(), pass_idx):
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

        if _is_waf_blocked(resp.status_code, resp.text):
            print(f"[WARN] APKMirror WAF blocked release page (HTTP {resp.status_code}).")
            return None
        if resp.status_code != 200:
            print(f"[WARN] APKMirror returned HTTP {resp.status_code} on release page.")
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

        # Fallback for single-variant pages where the download button is present
        # but there is no "table-row" variants list.
        dl_btn = soup.find(
            lambda tag: tag.name == "a" and "downloadButton" in tag.get("class", [])
        )

        if dl_btn:
            return self._process_variant_page(ctx, rel_url, force_b)

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
