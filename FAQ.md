# ❓ Frequently Asked Questions (FAQ)

Welcome to the FAQ section! If you encounter any issues while using the Automated APK Patcher, you might find the solution here.

### 1. How do I patch multiple apps with specific "Custom" versions?
If you select **Custom** in the `App Version Selection` dropdown, you can provide specific versions for multiple apps using a key-value format. 
In the **Custom App Version** text box, type the app name and version separated by an equals sign (`=`), and use commas to separate multiple apps.
**Example:** `x-twitter=12.15.1-release.0, instagram=439.0.0.37.89`

### 2. The workflow failed with a `[ERROR] FAILED: DMCA Trap` message. What does this mean?
Occasionally, APK mirroring sites will replace a requested APK with a dummy/stub file due to copyright (DMCA) takedown requests. Our scraper is designed to automatically detect these "traps" and abort the patching process to prevent you from installing a corrupted app. 
**Solution:** Try selecting a slightly older or newer version of the app, or wait until the upstream source provides a working APK.

### 3. I successfully patched `YouTube` and/or `YouTube Music`, but the app crashes on startup!
Patched `Google` apps require a framework to spoof `Google Play Services`. Without it, the app will instantly crash or refuse to log in.
**Solution:** You must install [microG-RE](https://github.com/MorpheApp/MicroG-RE/releases/latest) on your device and log in with your `Google account` before opening the patched `Google` apps.

### 4. How can I enable specific patch options (like "Dynamic color")?
We use an `options_override` parameter in the `ecosystems.json` file to force specific patches to be enabled or disabled.
If you are a maintainer or have cloned this repository, you can edit the JSON file and add the exact patch name under the app's configuration:
```json
"options_override": {
    "Dynamic color": {
        "enabled": true
    }
}
```

### 5. Can I add my own apps to the patcher?
Yes! The patcher is modular. You can add new apps by editing the `ecosystems.json` file. You need to provide the app's package name, a `search_term`, and at least one `stable` fallback version. The scraper will automatically attempt to fetch the APK from multiple tiers (`Archive.org`, `APKMirror`, `APKCombo`, `Aptoide` and `Uptodown`).

### 6. Why did the workflow fail with `Version defined as 'Any'. Skipping.`?
Some apps (like certain system apps or highly fragmented bundles) are marked with version `"Any"` in our JSON configuration because their versions are too varied to hardcode. Our automated scraper skips these by default to prevent downloading the wrong architecture. You must provide a specific version number using the Custom option to patch them.

### 7. What apps are supported in the `rushiranpise` ecosystem, and how do I select them?
Because the `rushiranpise` ecosystem supports a massive list of apps, we use a text input box instead of individual checkboxes. To patch them, you can simply type all in the Apps to patch box to process everything. Alternatively, you can double-click to easily copy the specific app names from the list below. *(If you select multiple apps, make sure to separate them with a comma)*:

- `1-1-1-1`
- `accubattery`
- `accuweather`
- `adobe-scan`
- `aida64`
- `amoledpix`
- `ampere`
- `anime-depth`
- `apkmirror-installer`
- `calm`
- `canva`
- `colornote`
- `cpu-z`
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
- `sd-maid-se`
- `stargazing-hub`
- `stickerly`
- `strava`
- `terabox`
- `turboscan`
- `uptodown-store`
- `wallverse`
- `waze`
- `windscribe-vpn`
- `wolfram-alpha`
