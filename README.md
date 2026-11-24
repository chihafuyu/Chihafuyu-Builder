<h1 align="center">🛠️ Chihafuyu Builder</h1>

<p align="center">
  <em>An automated, highly modular APK downloading and patching pipeline running natively on GitHub Actions. Designed as a customizable template for any patch ecosystem.</em>
</p>

---

## 📖 About

**Chihafuyu Builder** is a GitHub Actions–driven pipeline that fetches APKs from multiple sources and patches them using a compatible CLI. It's built around modularity, so you can fork it or use it as a template to run your own cloud-based APK factory for any patch ecosystem — without writing a single line of Python.

## ✨ Features

- 🤖 **Automated & Parallelized Workflow** — Trigger patching straight from GitHub Actions, with matrix concurrency scoping to prevent CI bottlenecks.
- 🎛️ **Granular Dispatch Controls** — Patch specific apps or an entire ecosystem, then route output to GitHub Releases, a Telegram channel, or both.
- 📥 **Multi-Tier Smart Downloader** — Aggressive fallback chain across Direct URLs, GitHub Releases, HuggingFace, APKMirror, Archive.org, APKPure, and Google Play Store. Backed by a local **FlareSolverr Docker microservice** to seamlessly bypass Cloudflare WAF and Turnstile inside GitHub Actions.
- 🛡️ **Security Hardened** — Enforces strict HTTPS validation, fails fast if no malware scanners are configured, and runs third-party tools in a credential-free sandbox.
- 📦 **Split/Bundle Support** — Pulls XAPK/APKM bundles when patches require them (e.g., Google apps), with dynamic segment matching for exact version accuracy.
- 🗄️ **Source Vault Sync** — Optional manual workflow that archives original APK/APKS/XAPK/APKM files to a separate HuggingFace dataset using the exact Tier 0 filenames, then prunes superseded versions after a successful replacement upload.
- ⚙️ **Dynamic Options Injection** — Generates and modifies `options.json` on the fly to apply custom patch preferences and locale stripping.
- 📁 **Local Patch Support** — Use custom patch bundles directly from your repository.
- 🧪 **Upstream Test Builds** — Fetch the newest Morphe or Piko CI `.mpp` artifact directly from GitHub Actions, with SHA-256 verification.
- 🚀 **Seamless Distribution** — Signs finished APKs and uploads them to GitHub Releases and private Telegram channels via session strings.

## 🚀 How to Use This Template

### 1. Create Your Repository

Click **Use this template** at the top of this repository to create your own copy.

### 2. Generate Your Custom Keystore

To sign your patched APKs, you'll need a cryptographic keystore. A helper script is included for Linux, macOS, and WSL.

1. Open a terminal in the repository folder.
2. Grant execution permission:
   ```bash
   chmod +x generate_keystore.sh
   ```
3. Run it:
   ```bash
   ./generate_keystore.sh
   ```
4. Follow the interactive prompts to set your alias, password, and distinguished name (DNAME).
5. The script produces a `.keystore` file and a Base64 text file (`custom_keystore_base64.txt`) with no line breaks, ready for GitHub Secrets.

### 3. Generate a Telegram Session String (Optional)

To auto-upload patched APKs to Telegram (bypassing the 50 MB bot upload limit), you'll need a Pyrogram session string.

1. Ensure **Python 3.12 or newer** is installed.
2. Install the required library:
   ```bash
   pip install pyrogram
   ```
3. Run the script:
   ```bash
   python generate_session.py
   ```
4. Enter your `API_ID` and `API_HASH` (available at `https://my.telegram.org/`).
5. Follow the Telegram login steps in the terminal.
6. Copy the session string immediately.

### 4. Set Up GitHub Secrets

Configure the following secrets at **Settings → Secrets and variables → Actions**:

| Secret | Description |
| :--- | :--- |
| `GEMINI_API_KEY` | Google Gemini API key for the automated PR code reviewer |
| `KEYSTORE_BASE64` | Contents of `custom_keystore_base64.txt` from Step 2 |
| `KEYSTORE_ALIAS` | Keystore alias chosen during generation |
| `KEYSTORE_PASSWORD` | Keystore password chosen during generation |
| `KEYSTORE_SIGNER_NAME` | Signer name passed to the CLI |
| `PLAY_EMAIL` | Google Play account email for APK fetching |
| `PLAY_AAS_TOKEN` | Google Play AAS token for authentication |
| `DEVICE_PROPERTIES_B64` | Base64-encoded device properties for Play Store emulation |
| `VT_API_KEY` | VirusTotal API key for scan verification |
| `HA_API_KEY` | HybridAnalysis API key |
| `MD_API_KEY` | MetaDefender API key |
| `HF_DATASET_ID` | HuggingFace dataset that receives **patched** APKs |
| `HF_TOKEN` | HuggingFace token for patched-output uploads; also the fallback token for the source vault |
| `HF_SOURCE_TOKEN` | Optional dedicated HuggingFace token for the source vault (falls back to `HF_TOKEN`) |
| `HF_SOURCE_DATASET_ID` | Optional source-vault dataset override; when blank, each app uses `{hf_user}/{archive_id}` |
| `PATCH_SOURCE_TOKEN` | Fine-grained PAT with **Actions: Read** on the upstream patch repository; required for the `Test Artifact` patches track |
| `API_ID` | Telegram API ID from my.telegram.org |
| `API_HASH` | Telegram API hash from my.telegram.org |
| `SESSION_STRING` | Pyrogram session string from Step 3 |
| `CHAT_ID` | Target Telegram channel/group ID or private invite link |

> **⚠️ CRITICAL:** You **must** provide at least one of `VT_API_KEY`, `HA_API_KEY`, or `MD_API_KEY`. The pipeline will fail-fast and abort if no malware scanners are configured.

### 5. Customize Ecosystems

Edit the JSON files under `ecosystem/` (e.g., `arandomhooman.json`, `byehi98.json`) to add, remove, or modify tracked applications.

**For Termux local builds:** if your ecosystem uses complex matrices (like `Piko` or `SysAdminDoc`), place split `JSON` files in `ecosystem` directory and register them in `repo_map.json`. The Termux script parses this map to build its interactive menu.

**Upstream mirror fallback:** the auto-update config workflow reads `repo_map.json`. Add `fallback_urls` to an ecosystem entry to retry a mirror (for example Codeberg) when the GitHub raw source is unavailable or the account is suspended. `kveld9` and `kveld9-extra` already point to their Codeberg mirrors.

### 6. Fill the Source Vault (Optional)

Tier 0 can serve cached **original** artifacts from HuggingFace. The **Fetch Googlag Apps (Source Vault)** workflow fills that cache manually. It never patches, signs, or generates `options.json`, and it writes to a dataset separate from `HF_DATASET_ID` (the patched-output dataset). By default it keeps one version per package and removes the superseded file only after the replacement upload succeeds.

#### How apps are selected

Each ecosystem uses two files:

| File | Role |
| :--- | :--- |
| `config/apps-<ecosystem>.toml` | Download switch. Only apps marked `true` are processed; `false` is ignored. |
| `ecosystem/<ecosystem>.json` | Package name, `stable`/`beta` version, `archive_id`, and scraper hints. |

Leave **App keys** blank (or use `all`) to follow the TOML. For example, `hxreborn` currently has 188 TOML entries, 45 of them `true`; a dry run therefore plans 45 artifacts. To narrow the TOML selection for one run, use a comma-separated list such as `1weather,acalendar` or `hxreborn/1weather`.

#### Run the workflow

1. Open **Actions → 📂 Fetch Googlag Apps (Source Vault) → Run workflow**.
2. Set **Ecosystems** to one ecosystem (for example `hxreborn`) or `all`.
3. Leave **App keys** blank to use every `true` app in the selected TOML, or enter an explicit override.
4. Run once with **Dry run** enabled to review the plan and counts.
5. Run again with **Dry run** disabled to download, verify, and upload.

Useful inputs:

| Input | Behavior |
| :--- | :--- |
| `version_selection` | `stable` or `beta`; an explicit `custom_version` pin wins. |
| `download_source` | `auto` tries Google Play first, then APKMirror. |
| `limit` | Maximum unique artifacts to attempt after already-present files are skipped; `0` means no limit. |
| `custom_version` | Optional `app=version` pins, for example `1weather=13.1.0`. A single value without `=` applies globally. |
| `hf_dataset` | Optional single dataset override. Blank uses each app's `{hf_user}/{archive_id}` repo. |
| `dry_run` | Prints the plan only; no downloads, uploads, scanners, or credentials needed. |
| `force` | Re-downloads even when the same filename already exists in the vault. |
| `prune_old_versions` | Enabled by default. After a new artifact uploads successfully, removes older artifacts of the same package in the same dataset. Disable it to keep multiple versions. |
| `no_upload` | Downloads and verifies locally without uploading. |
| `keep_files` | Keeps staged files after upload, or after `no_upload`. |
| `strict` | Fails a real run when any selected app cannot be vaulted. |

#### Safety guarantees

- **Play is primary; APKMirror is fallback.** Google Play cannot request an older version, so the downloaded artifact's embedded `versionName` is verified. If it does not match the target, the artifact is discarded and APKMirror is tried for the pinned version in `auto` mode. A mismatched file is never renamed to the target version.
- **Original containers are preserved.** Files stay APK, XAPK, APKM, or APKS; they are not unwrapped, patched, signed, or relabeled.
- **Tier 0 naming is exact.** Files use `vault_filename()` (`package_version.ext`), so the patcher can find them later.
- **Existing files are skipped.** Without `force`, the workflow checks the vault inventory before applying `limit`, which lets repeated bounded runs keep advancing.
- **Pruning is post-upload and metadata-driven.** Uploads are batched; a failed upload leaves both the staged file and every old remote version untouched. Only after a replacement upload succeeds are other files whose `vault_manifest.json` entry has the same `package` removed. Filename prefixes are never trusted for ownership, so `com.foo` cannot accidentally delete `com.foo_bar`. Files without manifest metadata, versions targeted by the same run, and unrelated packages are left alone.
- **A failed or unreadable manifest disables pruning.** The workflow will not guess which files belong to an app, and it merges new entries instead of replacing the existing manifest.
- **No guessing.** An enabled app with `"stable": ["any"]` or no concrete version is reported as failed. Add a pin through `custom_version` or set a concrete version in the ecosystem JSON.

> **Pruning warning:** the version selected by the run is treated as current. If you pin an older version with `custom_version` while a newer file is already in the vault, the newer file is considered superseded. Set `prune_old_versions=false` when you intentionally keep multiple versions of the same package.
>
> Pruning only runs after a replacement upload. If the target file is already present, the workflow reports `already_present` and leaves old files in place; use `force=true` to re-upload the current target and prune, or wait for the next version.

A real run needs `HF_SOURCE_TOKEN` (or `HF_TOKEN`) and at least one malware scanner (`VT_API_KEY`, `HA_API_KEY`, or `MD_API_KEY`). Play credentials are needed for Google Play downloads; without them, `auto` uses APKMirror only and `google_play` alone fails. The workflow starts FlareSolverr automatically for APKMirror. Full results are saved as the `vault-sync-summary` artifact.

#### Source vault vs. patched output

| | Source vault | Patched output |
| :--- | :--- | :--- |
| Workflow | Fetch Googlag Apps (Source Vault) | Ecosystem patcher workflows |
| Contents | Original APK/APKS/XAPK/APKM | Patched, signed APKs |
| Dataset | `HF_SOURCE_DATASET_ID`, or per-app `{hf_user}/{archive_id}` | `HF_DATASET_ID` |
| Naming | Tier 0 `package_version.ext` | Patcher output names |

Keep the two datasets separate. Pointing `hf_dataset` or `HF_SOURCE_DATASET_ID` at `HF_DATASET_ID` would mix original and patched files in one repository.

You can also run the same logic locally:

```bash
python vault_sync.py --ecosystems hxreborn --apps all --dry-run
python vault_sync.py --ecosystems hxreborn --apps all --limit 10 --prune-old-versions
```

### 7. Local Build (Termux / Android)

Patch directly on an Android device without depending on GitHub Actions limits. The Termux builder supports dynamic matrix routing, anti-cache corruption, and API rate-limit bypass.

#### Quick Start

Open Termux and run:

```bash
apt update && apt full-upgrade -y && apt install curl -y && \
  bash <(curl -sL https://raw.githubusercontent.com/chihafuyu/Chihafuyu-Builder/main/termux-patch.sh)
```

#### Create a Shortcut (Recommended)

Register `chihafuyu` as a one-word command:

```bash
echo 'alias chihafuyu="bash <(curl -sL https://raw.githubusercontent.com/chihafuyu/Chihafuyu-Builder/main/termux-patch.sh)"' >> ~/.bashrc
source ~/.bashrc
```

From then on, just type `chihafuyu` in Termux to start patching.

#### Bypass GitHub API Rate Limits (Optional)

Anonymous GitHub API requests are capped at 60 per hour. A classic personal access token (no scopes required) raises this to 5,000 per hour. Generate one at [GitHub Developer Settings](https://github.com/settings/tokens) and store it once:

```bash
echo 'export GITHUB_TOKEN="your_ghp_token_here"' >> ~/.bashrc && source ~/.bashrc
```

### 8. Use Upstream Test Builds (Optional)

Some patch repositories publish CI-only `.mpp` bundles as GitHub Actions artifacts instead of releases. The `patches_track` input accepts `Test Artifact` for those:

- It reads the newest successful `build_pull_request.yml` run on the `dev` branch of the ecosystem's `repo_url`.
- Only runs whose head repository is `repo_url` itself are eligible, so fork pull-request artifacts are ignored.
- If the newest runs have no unexpired artifact, it checks older successful runs.
- It accepts a ZIP artifact containing an `.mpp` member (Piko's `patches-bundle`) or a raw `.mpp` artifact (Morphe).
- The artifact is checked against GitHub's SHA-256 digest before use.

Piko exposes this as **Patches Track → Test Artifact**.

GitHub does not allow the built-in `GITHUB_TOKEN` to read another repository's artifacts. Add a `PATCH_SOURCE_TOKEN` repository secret: a fine-grained PAT with **Actions: Read** access to the upstream public repository. Without it, `Test Artifact` stops with a clear error.

If you do not want to manage a PAT, use `Pre-release`. Piko publishes `v*-dev.*` prereleases with `.mpp` assets, but released builds can lag behind the newest CI build.

> Test builds can target newer app versions than the pins in `ecosystem/piko.json`. If patching fails with an unsupported-version error, update the ecosystem version or switch to `Pre-release`/`Stable`.

## 🙏 Credits & Acknowledgments

This project builds on work from the following developers:

- [**Morphe**](https://github.com/MorpheApp) — Patching CLI and base ecosystem. Licensed under GPLv3.
- [**crimera**](https://github.com/crimera) — APKMirror bypass technique (header spoofing and Referer injection). Licensed under GPLv3.
- [**j-hc**](https://github.com/j-hc) — Uptodown and Archive downloader logic. Licensed under GPLv3.
- [**rushiranpise**](https://github.com/rushiranpise) — WAF/Turnstile detection, Bing fallback, and scraping logic (APKMirror regex and Uptodown validation) from `helper-for-morphe`. Licensed under GPLv3.
- [**apkeep**](https://github.com/EFForg/apkeep) — APKPure fallback download mechanism. Licensed under MIT.
- [**NagramX**](https://github.com/risin42/NagramX) — Original inspiration and base logic for automating Telegram uploads via GitHub Actions. Licensed under GPLv3.
- [**Morphe Community Patches**](https://morphe-patches.software/) — Community patches featuring a wide variety of apps. Copyright Morphe (not licensed under open-source terms).

## 📚 FAQ

Questions or errors? Check the [FAQ page](FAQ.md) for the full list of supported apps and troubleshooting steps.

## 📄 License

Distributed under the **MIT License**.

**Copyright (c) 2026 chihafuyu**

> You are free to use, modify, and distribute this tool for any purpose, as long as you keep the original copyright notice above. Provided _"as is"_, without warranty of any kind. Use it at your own risk.
