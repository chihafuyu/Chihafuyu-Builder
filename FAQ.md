# ❓ Frequently Asked Questions (FAQ)

Welcome to the FAQ. If you run into trouble while using **Chihafuyu Builder**, you'll likely find the answer below. If not, open an issue or check the [README](README.md).

---

## 1. How do I patch multiple apps with specific "Custom" versions?

Select **Custom** in the `App Version Selection` dropdown, then use a `name=version` pair format in the **Custom App Version** field. Separate multiple apps with commas.

**Example:**

```text
x-twitter=12.15.1-release.0, instagram=439.0.0.37.89
```

Each app is matched by its key in the ecosystem JSON. Apps not listed fall back to their configured `stable` version.

---

## 2. How do I enable specific patch options (like "Dynamic color")?

Patch enablement is controlled through `options_override` inside the ecosystem-specific JSON files (e.g., `piko.json`, `kveld9.json`, `SysAdminDoc.json`). Maintainers can add any patch name under the target app:

```json
"options_override": {
    "Dynamic color": {
        "enabled": true
    }
}
```

Any patch not listed in `options_override` keeps its built-in default. To disable a patch that's on by default, set `"enabled": false`.

---

## 3. How do I use my own keystore to sign patched APKs?

By default, Morphe CLI signs patched APKs with an **auto-generated shared keystore**. That's fine for first-time installs, but if you want any of the following, you'll want your own:

- Install a new patched build **over a previously patched build** without uninstalling
- Keep a **consistent signature** across all your APKs
- Match the signature of a keystore you already use elsewhere

> **Important:** Android only allows an installed app to be updated by an APK signed with the **same key**. Switching keystores means you must uninstall the existing patched app first (which deletes its data).

### On GitHub Actions

Set these repository secrets:

| Secret | Purpose |
| :--- | :--- |
| `KEYSTORE_BASE64` | Base64-encoded keystore file |
| `KEYSTORE_ALIAS` | Alias of the key entry inside the keystore |
| `KEYSTORE_PASSWORD` | Entry password |
| `KEYSTORE_SIGNER_NAME` | Signer name embedded in the APK signature |

See the [README](README.md#2-generate-your-custom-keystore) for the helper script that generates these.

### On Termux (Local)

Drop your keystore file into:

```text
/storage/downloads/Chihafuyu/
```

The script auto-detects it (checks `.jks`, `.keystore`, `.bks`, `.p12`, `.pfx` in that order). If you have multiple keystores or want explicit control, create a `keystore.txt` in the same folder:

```ini
path=/storage/downloads/Chihafuyu/my-key.jks
alias=myalias
store_password=
entry_password=mysecret
signer=MyApp
```

All fields are optional. Blank lines and lines starting with `#` are ignored.

### Credential resolution order

For every field, the script checks in this order:

1. **Environment variable** (`KEYSTORE_PATH`, `KEYSTORE_PASSWORD`, `KEYSTORE_ALIAS`, `KEYSTORE_STORE_PASSWORD`, `KEYSTORE_SIGNER`)
2. **`keystore.txt`** field
3. **Auto-detect** from `BASE_DIR` (path only)
4. **Interactive prompt** (entry password only)
5. **Morphe default** — alias `Morphe`, entry password `Morphe`, store password empty

### Supported formats

`.jks`, `.bks`, `.p12`, `.pfx`, and any file named `*.keystore`. Morphe detects the format **from the file contents**, not the extension, and converts to BKS internally. Your original file is never modified.

> **Security tip:** If `keystore.txt` is readable by group/other users, the script warns you. Under `/storage/` you can't `chmod`, so consider keeping secrets in `$HOME` and pointing `path=` at the location — or supply credentials via environment variables instead.

---

## 4. Can I add my own apps to the patcher?

Yes — the patcher is fully modular. Add new apps by editing the appropriate ecosystem JSON file. Each app requires:

- A `package` name (e.g., `com.example.app`)
- A `search_term` for the downloader
- At least one entry under `stable` (or `beta`)

The 9-tier smart downloader will attempt to fetch the APK from any of these sources in order: **Google Play Store, HuggingFace, Archive.org, APKMirror, APKPure, APKCombo, Aptoide, Uptodown, GitHub Releases**.

Once added, the app appears in the interactive menu and can be dispatched through GitHub Actions.

---

## 5. The workflow failed with `[ERROR] FAILED: DMCA Trap`. What does this mean?

APK mirroring sites occasionally replace a requested APK with a dummy/stub file in response to copyright (DMCA) takedown requests. The scraper detects these traps and **aborts the patch** rather than letting you install a corrupted APK.

**Solutions:**

- Retry with a slightly **older or newer** version of the app.
- Wait a few days for the upstream source to publish a working APK.
- Try a different `download_source` in the workflow dispatch (e.g., `huggingface` or `github`).

---

## 6. My patched Google app (YouTube, YouTube Music, etc.) crashes on startup

Patched Google apps require a **Google Play Services spoofing framework**. Without it, they crash instantly or refuse to sign in.

**Solution:** Install [microG-RE](https://github.com/MorpheApp/MicroG-RE/releases/latest) on your device and log in with your Google account **before** opening the patched Google app.

---

## 7. Why did the workflow fail with `Version defined as 'Any'. Skipping.`?

Some apps (system apps, highly fragmented bundles, region-specific builds) are marked with `"stable": ["Any"]` in the JSON config because their versions vary too widely to pin. The automated scraper skips them by default to avoid downloading the wrong architecture.

**Solution:** Provide an explicit version through the **Custom App Version** field (see [Q1](#1-how-do-i-patch-multiple-apps-with-specific-custom-versions)). Example:

```text
some-app=1.2.3
```

---

## 8. What apps are supported in large ecosystems like `arandomhooman` and `rushiranpise`?

Because these ecosystems track dozens of apps, the dispatch form uses a **text input** instead of individual checkboxes (to stay under GitHub Actions input limits). Type `all` in the **Apps to patch** field to process everything, or paste a comma-separated list of specific app keys.

**Example:**

```text
finch, poweramp, webtoon
```

### `arandomhooman` apps

- `adm`
- `alpha-progression`
- `bandlab`
- `battery-guru`
- `cronometer`
- `directchat`
- `finch`
- `flightradar24`
- `foldersync`
- `inshot`
- `liquid-gallery`
- `poweramp`
- `smart-audiobook`
- `video-converter`
- `webtoon`

### `rushiranpise` apps

- `1-1-1-1`
- `accubattery`
- `accuweather`
- `aida64`
- `amoledpix`
- `ampere`
- `anime-depth`
- `apkmirror-installer`
- `calm`
- `canva`
- `colornote`
- `cpu-z`
- `drama-box` *(downloads via Uptodown)*
- `electron`
- `hola-vpn`
- `http-sniffer`
- `inure`
- `kahoot`
- `kinemaster`
- `lark-player`
- `life360`
- `ml-manager`
- `mobioffice`
- `netguard`
- `network-guru`
- `ninja-vpn`
- `proton-vpn`
- `proxyman`
- `psiphon-pro`
- `rar`
- `stargazing-hub`
- `stickerly`
- `turboscan`
- `wallverse`
- `windscribe-vpn`
- `wolfram-alpha`

---

## 🙋 Still stuck

Open an issue with:

1. The **workflow run URL** (if GitHub Actions)
2. The **full log output** from the failure
3. The **ecosystem and app** you were patching

The community can usually spot the cause within a few minutes.
