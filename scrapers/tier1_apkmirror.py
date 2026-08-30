"""Tier 1 Scraper: APKMirror."""

from typing import Any, Optional
from urllib.parse import quote_plus, urljoin
from bs4 import BeautifulSoup
import requests

from core.context import Context
from core.utils import _is_waf_blocked, download_file_stream
from .base import BaseScraper


class ApkmirrorScraper(BaseScraper):
    """Scrapes APKs from APKMirror handling WAF and variants."""

    @property
    def tier_name(self) -> str:
        """Returns the tier identifier."""
        return "apkmirror"

    def _find_release(self, ctx: Context) -> Optional[str]:
        t_ver = ctx.target_ver
        base_ver = t_ver.split("-")[0] if "-" in t_ver and t_ver[:1].isdigit() else t_ver
        query = quote_plus(
            f"{ctx.app_data.get('search_term', ctx.pkg)} {base_ver}"
        )
        url = f"https://www.apkmirror.com/?post_type=app_release&s={query}"

        ctx.limiter.wait()
        resp = ctx.scraper.get(url, timeout=60)
        if _is_waf_blocked(resp.status_code, resp.text) or resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, "html.parser")
        exc_kws = ["secondary"] + [
            k.lower() for k in ctx.app_data.get("apkm_exclude", [])
        ]
        inc_kws = [k.lower() for k in ctx.app_data.get("apkm_include", [])]

        for link in soup.find_all("a", class_="fontBlack"):
            text = link.text.lower()
            if base_ver.lower() not in text or any(
                kw in text for kw in exc_kws
            ):
                continue
            if inc_kws and not all(kw in text for kw in inc_kws):
                continue
            return urljoin("https://www.apkmirror.com", link["href"])
        return None

    def _process_variant_page(
        self, ctx: Context, var_url: str, is_bundle: bool
    ) -> Optional[str]:
        ctx.limiter.wait()
        v_resp = ctx.scraper.get(var_url, timeout=60)
        if (
            _is_waf_blocked(v_resp.status_code, v_resp.text)
            or v_resp.status_code != 200
        ):
            return None

        btn = BeautifulSoup(v_resp.text, "html.parser").find(
            "a", class_="downloadButton"
        )
        if not btn:
            return None

        dl_page = urljoin("https://www.apkmirror.com", btn["href"])
        ctx.limiter.wait()
        d_resp = ctx.scraper.get(dl_page, timeout=60)
        if (
            _is_waf_blocked(d_resp.status_code, d_resp.text)
            or d_resp.status_code != 200
        ):
            return None

        dl_btn = BeautifulSoup(d_resp.text, "html.parser").find(
            "a", {"rel": "nofollow"}
        )
        if dl_btn and "href" in dl_btn.attrs:
            out_path = ctx.get_out_path(".apkm" if is_bundle else ".apk")
            dl_url = urljoin("https://www.apkmirror.com", dl_btn["href"])
            print("[INFO] Downloading from APKMirror...")
            if download_file_stream(ctx.scraper, dl_url, out_path, dl_page):
                return out_path
        return None

    def _extract_row(
        self, ctx: Context, row: Any, is_bundle: bool, ver_code: str
    ) -> Optional[str]:
        text = row.text.lower()
        has_valid = any(
            a in text for a in (ctx.arch.lower(), "universal", "noarch")
        )
        has_any = any(
            a in text
            for a in ("arm64-v8a", "armeabi-v7a", "x86", "x86_64", "armeabi")
        )

        kind_match = ("bundle" in text) if is_bundle else ("bundle" not in text)
        if kind_match and (has_valid or not has_any):
            if not ver_code or str(ver_code) in text:
                if link := row.find("a", class_="accent_color"):
                    rel_url = urljoin(
                        "https://www.apkmirror.com", link["href"]
                    )
                    return self._process_variant_page(
                        ctx, rel_url, is_bundle
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
            for is_bndl in (False, True) if not force_b else (True,):
                for row in rows:
                    if out := self._extract_row(
                        ctx, row, is_bndl, ver_code
                    ):
                        return out
        elif btn := soup.find("a", class_="downloadButton"):
            is_bndl = "bundle" in btn.text.lower()
            if not force_b or is_bndl:
                return self._process_variant_page(
                    ctx,
                    urljoin("https://www.apkmirror.com", btn["href"]),
                    is_bndl,
                )
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
