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
- 📥 **Multi-Tier Smart Downloader** — Aggressive fallback chain across Google Play Store, HuggingFace, Archive.org, APKMirror, APKPure, and direct GitHub Releases. Backed by a local **FlareSolverr Docker microservice** to seamlessly bypass Cloudflare WAF and Turnstile inside GitHub Actions.
- 📦 **Split/Bundle Support** — Pulls XAPK/APKM bundles when patches require them (e.g., Google apps), with dynamic segment matching for exact version accuracy.
- ⚙️ **Dynamic Options Injection** — Generates and modifies `options.json` on the fly to apply custom patch preferences and locale stripping.
- 📁 **Local Patch Support** — Use custom patch bundles directly from your repository.
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
| `API_ID` | Telegram API ID from my.telegram.org |
| `API_HASH` | Telegram API hash from my.telegram.org |
| `SESSION_STRING` | Pyrogram session string from Step 3 |
| `CHAT_ID` | Target Telegram channel/group ID or private invite link |

### 5. Customize Ecosystems

Edit the JSON files under `ecosystem/` (e.g., `arandomhooman.json`, `byehi98.json`) to add, remove, or modify tracked applications.

**For Termux local builds:** if your ecosystem uses complex matrices (like `Piko` or `SysAdminDoc`), place split JSON files in `ecosystem-termux/` and register them in `ecosystem-termux/repo_map.json`. The Termux script parses this map to build its interactive menu.

### 6. Local Build (Termux / Android)

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
