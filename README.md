<h1 align="center">🛠️ Chihafuyu Builder</h1>

<p align="center">
  <em>An automated APK downloading and patching pipeline running natively on GitHub Actions.</em>
</p>

---

## 📖 About
**Chihafuyu Builder** utilizes GitHub Actions to provide an automated environment for fetching APKs from various sources and patching them using the `Morphe CLI`.

## ✨ Features
- 🤖 **Automated Workflow:** Trigger the patching process directly from GitHub Actions without requiring local setup.
- 📥 **Multi-Tier Downloader:** Retrieves APKs using multiple fallback sources (`Archive.org`, `APKMirror`, `APKPure`, `APKCombo`, `Aptoide`, and `Uptodown`).
- ⚙️ **Dynamic Options Injection:** Automatically generates and modifies the `options.json` file to apply custom patch preferences.
- 📁 **Local Patch Support:** Allows the use of custom `.mpp` files directly from the repository.
- 🚀 **Release Generation:** Automatically signs and uploads the finished APKs directly to GitHub Releases.

## 🙏 Credits
This project uses methods and tools from the following developers:
- [**Crimera**](https://github.com/crimera) - `APKMirror` bypass technique (Header Spoofing & Referer Injection). Licensed under GPLv3.
- [**j-hc**](https://github.com/j-hc) - `Uptodown` and `Archive` downloader logic. Licensed under GPLv3.
- [**Morphe**](https://github.com/MorpheApp) - Patching CLI and base ecosystem. Licensed under GPLv3.
- [**apkeep**](https://github.com/EFForg/apkeep) - `APKPure` fallback download mechanism. Licensed under MIT.

## 📚 Frequently Asked Questions (FAQ)

**Got questions or running into errors?** Check out our [FAQ Page](FAQ.md) first!

## 📄 License
Distributed under the **MIT License**.

**Copyright (c) 2026 chihafuyu**

> You are free to use, modify, and distribute this tool for any purpose, as long as you keep the original copyright notice above. It is provided _"as is"_, without warranty of any kind. Use it at your own risk!
