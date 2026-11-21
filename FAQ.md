# ❓ Frequently Asked Questions (FAQ)

Welcome to the FAQ. If you run into trouble while using **Chihafuyu Builder**, you'll likely find the answer below. If not, open an issue or check the [README](README.md).

---

## 1. How do I patch multiple apps with specific "Custom" versions?

Select **Custom** in the `App Version Selection` dropdown, then use a `name=version` pair format in the **Custom App Version** field. Separate multiple apps with commas.

**Example:**

```text
twitter-newx=12.41.0-prod.01, instagram=452.1.0.53.64
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

The 7-tier smart downloader will attempt to fetch the APK from any of these sources in order: **Direct URL, GitHub Releases, HuggingFace, APKMirror, Archive.org, APKPure, and Google Play Store**.

Once added, the app appears in the interactive menu and can be dispatched through GitHub Actions.

To archive the original artifact for the Tier 0 fallback, also set the app to `true` in `config/apps-<ecosystem>.toml` and run the [source vault workflow](README.md#6-fill-the-source-vault-optional).

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

## 7. Why did the workflow skip an app with `[INFO] '{ctx.pkg}' resolves to 'Any'. Skipping (requires explicit version).`?

Some apps (system apps, highly fragmented bundles, region-specific builds) are marked with `"stable": ["Any"]` in the JSON config because their versions vary too widely to pin. The automated scraper skips them by default to avoid downloading the wrong architecture.

**Solution:** Provide an explicit version through the **Custom App Version** field (see [Q1](#1-how-do-i-patch-multiple-apps-with-specific-custom-versions)). Example:

```text
some-app=1.2.3
```

---

## 8. What apps are supported in large ecosystems like `arandomhooman` and `rushiranpise`?

Because these ecosystems track dozens of apps, the dispatch form uses a **text input** instead of individual checkboxes (to stay under GitHub Actions input limits).

- Typing `all` in the **Apps to patch** field will read the `config/apps-{ecosystem}.toml` file and process every app marked as `true`.
- To process specific apps without changing the TOML, paste a comma-separated list of specific app keys.

**Example:**

```text
finch, poweramp, webtoon
```

> In the source-vault workflow, a blank **App keys** field is equivalent to `all`. See [Q10](#10-do-i-have-to-list-every-app-in-app-keys-for-the-source-vault).

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

## 9. What is the "Fetch Googlag Apps (Source Vault)" workflow?

It is a **manual-only** workflow that archives original app artifacts into a HuggingFace dataset used as the Tier 0 fallback source. It does **not** patch, sign, generate `options.json`, or publish patched output.

Run it after changing ecosystem configs when you want the vault to contain the new or updated originals. It is intentionally separate from the ecosystem patcher workflows, and it writes to a different dataset from `HF_DATASET_ID`. See [README §6](README.md#6-fill-the-source-vault-optional) for the full input reference.

---

## 10. Do I have to list every app in "App keys" for the source vault?

No. Leave **App keys** blank, or enter `all`, to follow the selected ecosystem's TOML:

- `true` → download the app
- `false` → ignore the app

The ecosystem JSON still supplies the `package`, `stable`/`beta` version, `archive_id`, and scraper hints. For example, `hxreborn` currently has 188 TOML entries and 45 `true` entries, so a dry run plans 45 artifacts.

To narrow the TOML selection for one run, enter specific keys:

```text
1weather,acalendar
```

or ecosystem-qualified keys:

```text
hxreborn/1weather,hxreborn/acalendar
```

The app filter can narrow the selection, but it cannot enable an app that is `false` in the TOML. Set it to `true` first, then run the workflow.

---

## 11. Why did an app fail with "no concrete target version" or a version mismatch?

These failures are deliberate safeguards for Tier 0 naming:

- **`no concrete target version`** — the JSON has `"stable": ["any"]`, an empty version, or no version for the selected channel. There is no safe filename to create.
- **`Play version mismatch`** — Google Play served a different version than the target. The artifact is discarded instead of being renamed to the target version. In `auto` mode, the workflow then tries APKMirror for the pinned version.
- **APKMirror mismatch or missing version** — the fallback could not supply the exact target, so the app is skipped.

To fix it, set a concrete version in the ecosystem JSON or use the `custom_version` input:

```text
some-app=1.2.3
```

Using the correct target version matters because Tier 0 finds cached files by their exact `package_version.ext` name.

---

## 12. How do I fill a large vault without hitting the GitHub Actions time limit?

Use the `limit` input. It is applied **after** already-present vault files are skipped, so repeated bounded runs keep advancing through the remaining apps instead of reprocessing the same first entries.

Example sequence:

1. Run with `limit=25` and `dry_run=true` to confirm the plan.
2. Run with `limit=25` and `dry_run=false`.
3. Repeat the real run; files already in the vault are reported as `already_present`.
4. Increase `limit` when you have enough runtime headroom.

`max_runtime_minutes` lets the job stop gracefully and marks the remaining plans as `skipped`. Use `force=true` only when you intentionally want to replace an existing filename.

The full `all` set is large (hundreds of artifacts), so bounded runs are the recommended way to seed it.

---

## 13. Which HuggingFace secrets and dataset does the source vault use?

| Setting | Purpose |
| :--- | :--- |
| `HF_SOURCE_TOKEN` | Preferred token for source-vault uploads. |
| `HF_TOKEN` | Fallback token when `HF_SOURCE_TOKEN` is not set. |
| `HF_SOURCE_DATASET_ID` | Optional single dataset override in `owner/name` form. |
| `HF_DATASET_ID` | **Patched-output** dataset; do not use it for the source vault. |

When no override is provided, each app uses:

```text
{hf_user or chihafuyu}/{archive_id}
```

With the current ecosystem configs that resolves to `chihafuyu/ZeNiX`. If the dataset does not exist, the workflow creates it as a public dataset. The uploader writes a SHA-256 `vault_manifest.json` alongside the artifacts.

A real run also needs at least one scanner key (`VT_API_KEY`, `HA_API_KEY`, or `MD_API_KEY`). Play credentials are needed for Google Play downloads; without them, `auto` uses APKMirror only and `google_play` alone fails. Dry runs need none of these.

---

## 14. Does the source vault workflow delete or overwrite files?

No remote files are deleted by this workflow. Its behavior is intentionally non-destructive:

- Existing filenames are skipped unless `force=true`.
- Uploads are committed in batches, with a per-file fallback if a batch fails.
- A failed upload leaves the verified staged file in place for inspection.
- With `no_upload=true`, verified files are removed after the run unless `keep_files=true`.
- `vault_manifest.json` merges new entries into existing metadata; it is not replaced with an empty manifest.
- If the existing remote manifest cannot be read safely, it is left untouched rather than overwritten.

The only local cleanup is removal of verified artifacts after a successful upload (or after `no_upload` when `keep_files` is disabled). Files that fail verification are removed before they can be uploaded.

---

## 🙋 Still stuck

Open an issue with:

1. The **workflow run URL** (if GitHub Actions)
2. The **full log output** from the failure
3. The **ecosystem and app** you were patching

The community can usually spot the cause within a few minutes.
