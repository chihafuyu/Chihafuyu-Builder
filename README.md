<h1 align="center">🛠️ Chihafuyu Builder</h1>

<p align="center">
  <em>An automated, highly modular APK downloading and patching pipeline running natively on GitHub Actions. Designed to be a customizable template for any patching ecosystem.</em>
</p>

---

## 📖 About
**Chihafuyu Builder** utilizes GitHub Actions to provide a fully automated environment for fetching APKs from various sources and patching them using the `Morphe CLI`. It is built with modularity in mind—you can easily fork or use this repository as a template to build your own cloud-based APK factory for any patch ecosystem without writing a single line of Python.

## ✨ Features
- 🤖 **Automated Workflow:** Trigger the patching process directly from GitHub Actions without requiring local setup.
- 🎛️ **Granular Dispatch Controls:** Choose to patch specific apps, run the entire ecosystem, and optionally dispatch the output to GitHub Releases or a Telegram Channel.
- 📥 **7-Tier Smart Downloader:** Retrieves APKs using an aggressive fallback mechanism (`Archive.org`, `APKMirror`, `APKPure`, `APKCombo`, `Aptoide`, `Uptodown`, and direct `GitHub Releases`).
- 📦 **Split/Bundle Support:** Natively bypasses base-APK limitations to pull specific XAPK/APKM bundles when required by the patches (e.g., Google Apps).
- ⚙️ **Dynamic Options Injection:** Automatically generates and modifies the `options.json` file on-the-fly to apply custom patch preferences and locale stripping.
- 📁 **Local Patch Support:** Allows the use of custom `.mpp` files directly from your repository.
- 🚀 **Seamless Distribution:** Automatically signs and uploads the finished APKs directly to GitHub Releases and your private Telegram channels using Session Strings.

## 🚀 How to Use This Template

Want to build your own automated patcher? Follow these steps:

### 1. Create Your Repository
Click the green **Use this template** button at the top of this repository to create your own copy.

### 2. Generate Your Custom Keystore
To sign your patched APKs, you will need a cryptographic Keystore. We have included an interactive PowerShell script to make this painless:
1. Open the repository folder on your Windows PC.
2. Right-click on **`Generate_Keystore.ps1`** and select **Run with PowerShell**.
3. Follow the interactive prompts to set your Alias, Password, and Distinguished Name (DNAME).
4. The script will generate a 4096-bit, 100-year validity Keystore (`.keystore`) and automatically convert it into a Base64 text file (`custom_keystore_base64.txt`).

### 3. Set Up GitHub Secrets
For the automated workflows to function, you MUST configure the following secrets in your repository (`Settings > Secrets and variables > Actions`):

*   **`KEYSTORE_BASE64`**: Paste the entire contents of the `custom_keystore_base64.txt` generated in Step 2.
*   **`KEYSTORE_ALIAS`**: The alias you chose during Keystore generation.
*   **`KEYSTORE_PASSWORD`**: The password you set during Keystore generation.
*   **`KEYSTORE_SIGNER_NAME`**: The specific signer name for the CLI.
*   **`API_ID`** & **`API_HASH`**: Your Telegram API credentials (obtainable from my.telegram.org).
*   **`SESSION_STRING`**: Your Pyrogram/Telethon session string (Userbot) to bypass the 50MB bot upload limit.
*   **`CHAT_ID`**: The Target Telegram channel or group ID (or private invite link).

### 4. Customize Ecosystems
Edit the `ecosystems.json` file to add, remove, or modify the applications you want to track. You can define target architectures, specific search terms, and inject custom patch options effortlessly.

## 🙏 Credits & Acknowledgements
This project uses methods and tools from the following developers:
- [**crimera**](https://github.com/crimera) - `APKMirror` bypass technique (Header Spoofing & Referer Injection). Licensed under GPLv3.
- [**j-hc**](https://github.com/j-hc) - `Uptodown` and `Archive` downloader logic. Licensed under GPLv3.
- [**Morphe**](https://github.com/MorpheApp) - Patching CLI and base ecosystem. Licensed under GPLv3.
- [**apkeep**](https://github.com/EFForg/apkeep) - `APKPure` fallback download mechanism. Licensed under MIT.
- [**Morphe Community Patches**](https://morphe-patches.software/) - Community patches, featuring a bunch of apps. Copyright Morphe (copyrighted and not licensed under open source terms).
- [**NagramX**](https://github.com/risin42/NagramX) - Original inspiration and base logic for `automating Telegram uploads` via GitHub Actions. Licensed under GPLv3.

## 📚 Frequently Asked Questions (FAQ)
**Got questions or running into errors?** Check out our [FAQ Page](FAQ.md) first to see the full list of supported apps and troubleshooting steps!

## 📄 License
Distributed under the **MIT License**

**Copyright (c) 2026 chihafuyu**

> You are free to use, modify, and distribute this tool for any purpose, as long as you keep the original copyright notice above. It is provided _"as is"_, without warranty of any kind. Use it at your own risk!
