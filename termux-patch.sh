#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────────────
# Color palette for terminal output
# ─────────────────────────────────────────────────────────────────
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
WHITE='\033[1;37m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

# ─────────────────────────────────────────────────────────────────
# Paths & constants
# ─────────────────────────────────────────────────────────────────
TERMUX_PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
TERMUX_HOME="${HOME:-/data/data/com.termux/files/home}"
USER_AGENT="ChihafuyuBuilder/2.0 (Termux; Android)"
WORK_DIR="$TERMUX_PREFIX/var/chihafuyu-workspace"
BASE_DIR="$TERMUX_HOME/storage/downloads/Chihafuyu"
MORPHE_JAR="$BASE_DIR/morphe.jar"
REPO_URL="https://raw.githubusercontent.com/chihafuyu/Chihafuyu-Builder/main"

# ─────────────────────────────────────────────────────────────────
# Runtime state (populated during the interactive flow)
# ─────────────────────────────────────────────────────────────────
TEMP_PATCH=""      # filename of the .mpp bundle for this run
ECO_CHOICE=""      # selected ecosystem key (e.g. "morphe", "piko")
TARGET_REPO=""     # GitHub "owner/repo" hosting the patch releases
TARGET_JSON_PATH="" # path to the ecosystem app list inside the repo
ECO_DIR=""         # local folder where the user drops the input APK
TRACK_CHOICE=""    # "Stable" or "Pre-release"
APK_CHOICE=""      # absolute path to the chosen APK/bundle
TEMP_LOG_FILE=""   # scratch log path used by cleanup()

# ─────────────────────────────────────────────────────────────────
# Cleanup & signal handling
# Runs exactly once on shell exit. Signal handlers convert INT/TERM
# into conventional exit codes so this handler is not invoked twice.
# ─────────────────────────────────────────────────────────────────
cleanup() {
    local exit_code=$?
    trap - EXIT INT TERM
    if [[ -n "$TEMP_LOG_FILE" && -f "$TEMP_LOG_FILE" ]]; then
        rm -f "$TEMP_LOG_FILE"
    fi
    exit "$exit_code"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ─────────────────────────────────────────────────────────────────
# Pre-flight checks
# ─────────────────────────────────────────────────────────────────

# Verifies curl/jq/java are installed and prompts pkg to install anything
# missing. Java 21 is enforced because Morphe patches require it.
check_dependencies() {
    local missing=()
    command -v curl >/dev/null 2>&1 || missing+=("curl")
    command -v jq >/dev/null 2>&1 || missing+=("jq")

    # Guard with command -v first to avoid noisy "command not found".
    if ! command -v java >/dev/null 2>&1 \
        || ! java -version 2>&1 | grep -q 'version "21'; then
        missing+=("openjdk-21")
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo -e "${YELLOW}[INFO] Installing missing dependencies: ${missing[*]}${NC}"
        if ! pkg update -y; then
            echo -e "${YELLOW}[WARN] pkg update failed, continuing with existing index.${NC}" >&2
        fi
        pkg install "${missing[@]}" -y
        echo -e "${GREEN}[INFO] Dependencies installed!${NC}\n"
    fi
}

# Ensures Termux has access to ~/storage/downloads. Waits up to 30s
# after termux-setup-storage for the user to grant the permission.
ensure_storage_access() {
    local target_dir="$TERMUX_HOME/storage/downloads"
    if [[ ! -d "$target_dir" ]]; then
        echo -e "${YELLOW}[INFO] Requesting internal storage access...${NC}"
        termux-setup-storage

        local attempts=0
        while [[ ! -d "$target_dir" ]]; do
            sleep 1
            attempts=$((attempts + 1))
            if [[ $attempts -ge 30 ]]; then
                echo -e "${RED}[ERROR] Storage access timeout.${NC}" >&2
                exit 1
            fi
        done
    fi
}

# ─────────────────────────────────────────────────────────────────
# Interactive selection
# ─────────────────────────────────────────────────────────────────

# Fetches ecosystem-termux/repo_map.json from the builder repo and lets
# the user pick an ecosystem. Populates TARGET_REPO, TARGET_JSON_PATH,
# and ECO_DIR.
select_ecosystem() {
    echo -e "${YELLOW}[INFO] Fetching ecosystem map from repository...${NC}"

    local map_url="${REPO_URL}/ecosystem-termux/repo_map.json"
    local map_data=""

    if ! map_data=$(curl -sL --max-time 15 -A "$USER_AGENT" "$map_url"); then
        echo -e "${RED}[ERROR] Failed to fetch repo_map.json.${NC}" >&2
        exit 1
    fi

    if ! printf '%s' "$map_data" | jq -e 'type == "object"' >/dev/null 2>&1; then
        echo -e "${RED}[ERROR] Failed to parse repo_map.json.${NC}" >&2
        exit 1
    fi

    local eco_list=()
    mapfile -t eco_list < <(printf '%s' "$map_data" | jq -r 'keys[]')

    if [[ ${#eco_list[@]} -eq 0 ]]; then
        echo -e "${RED}[ERROR] No ecosystems found in repo_map.json.${NC}" >&2
        exit 1
    fi

    eco_list+=("Exit")

    echo -e "\n${WHITE}Select Ecosystem Patches:${NC}"

    local PS3_BAK="${PS3:-}"
    local choice
    PS3="Enter your choice: "
    export COLUMNS=20

    select choice in "${eco_list[@]}"; do
        if [[ "$choice" == "Exit" ]]; then
            echo -e "${YELLOW}Exiting builder. Goodbye!${NC}"
            exit 0
        elif [[ -n "$choice" ]]; then
            ECO_CHOICE="$choice"
            echo -e "${GREEN}Selected ecosystem: $ECO_CHOICE${NC}"

            TARGET_REPO=$(printf '%s' "$map_data" | jq -r --arg eco "$ECO_CHOICE" '.[$eco].repo_url // empty')
            TARGET_JSON_PATH=$(printf '%s' "$map_data" | jq -r --arg eco "$ECO_CHOICE" '.[$eco].config_path // empty')

            # Fallbacks for ecosystems that omit explicit mapping.
            if [[ -z "$TARGET_REPO" ]]; then
                TARGET_REPO="chihafuyu/morphe-patches"
            fi

            if [[ -z "$TARGET_JSON_PATH" ]]; then
                TARGET_JSON_PATH="ecosystem/${ECO_CHOICE}.json"
            fi

            echo -e "${CYAN}Target Repo:${NC} $TARGET_REPO"

            ECO_DIR="$BASE_DIR/$ECO_CHOICE"
            mkdir -p "$ECO_DIR"
            break
        else
            echo -e "${RED}Invalid selection.${NC}" >&2
        fi
    done

    PS3="$PS3_BAK"
}

# Prints the list of apps supported by the selected ecosystem. Shows
# stable and beta versions when available so the user knows what to
# expect before picking a track.
show_supported_apps() {
    echo -e "\n${YELLOW}[INFO] Fetching supported apps for $ECO_CHOICE...${NC}"
    local json_url="${REPO_URL}/${TARGET_JSON_PATH}"
    local jq_filter

    echo -e "${CYAN}=== Supported Applications ===${NC}"

    jq_filter='.[].apps | to_entries[] |
        " - \(.value.search_term) (\(
            if .value.stable[0]
            then "stable: v\(.value.stable[0])"
            else "stable: Any"
            end
        )\(
            if (.value.beta // []) | length > 0
            then " | beta: v\(.value.beta[0])"
            else ""
            end
        ))"'

    if ! curl -sL --max-time 15 -f "$json_url" | jq -r "$jq_filter"; then
        echo -e "${RED}[WARN] Could not fetch configuration from $TARGET_JSON_PATH.${NC}" >&2
    fi
    echo -e "${CYAN}==============================${NC}"
}

# Asks the user to choose between Stable and Pre-release patch bundles.
# Pre-release emits a warning and later enables --force during patching.
select_track() {
    echo -e "\n${WHITE}Select Patch Track:${NC}"
    local tracks=("Stable" "Pre-release" "Exit")

    local PS3_BAK="${PS3:-}"
    local choice
    PS3="Enter track number: "

    select choice in "${tracks[@]}"; do
        if [[ "$choice" == "Exit" ]]; then
            exit 0
        elif [[ -n "$choice" ]]; then
            TRACK_CHOICE="$choice"
            echo -e "${GREEN}Selected track: $TRACK_CHOICE${NC}"

            if [[ "$TRACK_CHOICE" == "Pre-release" ]]; then
                echo -e "\n${YELLOW}⚠️  WARNING: Pre-release track selected.${NC}"
                echo -e "${YELLOW}    Pre-release patches are experimental and may contain bugs,${NC}"
                echo -e "${YELLOW}    unfinished features, or cause the patched app to crash.${NC}"
                echo -e "${YELLOW}    Use at your own risk.${NC}"
                echo -e "${CYAN}    The patcher will run with --force to allow version${NC}"
                echo -e "${CYAN}    mismatches between the APK and the patch bundle.${NC}\n"
            fi
            break
        else
            echo -e "${RED}Invalid selection.${NC}" >&2
        fi
    done

    PS3="$PS3_BAK"
}

# ─────────────────────────────────────────────────────────────────
# Downloads
# ─────────────────────────────────────────────────────────────────

# Downloads url to dest using HTTP caching (-z). A .tmp intermediate
# prevents corruption if the connection drops mid-download. On failure
# with an existing cached copy, the cached file is kept and warning
# is emitted instead of failing the run.
_download_with_cache() {
    local url="$1"
    local dest="$2"
    local timeout="$3"
    local tmp="${dest}.tmp"

    if [[ -s "$dest" ]]; then
        if ! curl -sLf --max-time "$timeout" -R -z "$dest" \
            -A "$USER_AGENT" "$url" -o "$tmp"; then
            rm -f "$tmp"
            echo -e "${YELLOW}[WARN] Update check failed, keeping cached copy.${NC}" >&2
            return 0
        fi
        if [[ -f "$tmp" ]]; then
            mv -f "$tmp" "$dest"
        fi
        return 0
    fi

    if ! curl -sLf --max-time "$timeout" -R \
        -A "$USER_AGENT" "$url" -o "$tmp"; then
        rm -f "$tmp"
        return 1
    fi
    mv -f "$tmp" "$dest"
    return 0
}

# Resolves and downloads two artifacts:
#   1. Morphe CLI JAR from MorpheApp/morphe-desktop (latest release)
#   2. Patch bundle .mpp from TARGET_REPO (Stable or Pre-release)
fetch_components() {
    echo -e "\n${YELLOW}[INFO] Checking core components & synchronizing files...${NC}"

    # GitHub API command as an array to preserve spaces in the token.
    local api_curl_cmd=(curl -sLf --max-time 15 -A "$USER_AGENT")
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        echo -e "${GREEN}[INFO] GitHub Token detected. Using authenticated API requests.${NC}"
        api_curl_cmd+=("-H" "Authorization: Bearer $GITHUB_TOKEN")
    fi

    # ── Morphe CLI: resolve asset URL from MorpheApp/morphe-desktop ──
    echo -e "${CYAN}Checking morphe-cli...${NC}"

    local cli_api_url="https://api.github.com/repos/MorpheApp/morphe-desktop/releases/latest"
    local cli_release_json=""
    if ! cli_release_json=$("${api_curl_cmd[@]}" "$cli_api_url"); then
        echo -e "${RED}[ERROR] Failed to query Morphe CLI release info.${NC}" >&2
        exit 1
    fi

    # Picks the newest "morphe-desktop-*-all.jar" asset; sort + tail
    # protects against multiple matches in unusual release layouts.
    local morphe_url=""
    morphe_url=$(printf '%s' "$cli_release_json" | jq -r '
        .assets[]? | select(.name | test("morphe-desktop-.*-all\\.jar$"))
        | .browser_download_url' | tail -n 1) || true

    if [[ -z "$morphe_url" || "$morphe_url" == "null" ]]; then
        echo -e "${RED}[ERROR] No Morphe CLI JAR asset found in the latest release.${NC}" >&2
        exit 1
    fi

    if ! _download_with_cache "$morphe_url" "$MORPHE_JAR" 300; then
        echo -e "${RED}[ERROR] Failed to download morphe-cli and no cache available.${NC}" >&2
        exit 1
    fi

    # ── Patches: resolve .mpp asset from the target repo ──
    local api_url
    local jq_filter
    if [[ "$TRACK_CHOICE" == "Stable" ]]; then
        api_url="https://api.github.com/repos/${TARGET_REPO}/releases/latest"
        jq_filter='.assets[]? | select(.name | endswith(".mpp")) | .browser_download_url'
    else
        api_url="https://api.github.com/repos/${TARGET_REPO}/releases"
        jq_filter='map(select(.prerelease == true)) | .[0].assets[]? | select(.name | endswith(".mpp")) | .browser_download_url'
    fi

    local api_response=""
    if ! api_response=$("${api_curl_cmd[@]}" "$api_url"); then
        echo -e "${RED}[ERROR] Failed to query GitHub API for $TARGET_REPO.${NC}" >&2
        exit 1
    fi

    local patch_url=""
    patch_url=$(printf '%s' "$api_response" | jq -r "$jq_filter" | tail -n 1) || true

    if [[ -z "$patch_url" || "$patch_url" == "null" ]]; then
        echo -e "${RED}[ERROR] No .mpp file found for $ECO_CHOICE in $TARGET_REPO ($TRACK_CHOICE). API rate limit exceeded?${NC}" >&2
        exit 1
    fi

    # Cache name segregates by ecosystem AND track so switching tracks
    # does not accidentally reuse an incompatible bundle.
    TEMP_PATCH="${ECO_CHOICE}-${TRACK_CHOICE// /-}.mpp"

    echo -e "${CYAN}Checking ecosystem patches...${NC}"
    if ! _download_with_cache "$patch_url" "$TEMP_PATCH" 60; then
        echo -e "${RED}[ERROR] Failed to download ecosystem patches.${NC}" >&2
        exit 1
    fi
}

# ─────────────────────────────────────────────────────────────────
# APK selection & patching
# ─────────────────────────────────────────────────────────────────

# Pauses the flow until the user drops their APK/bundle into ECO_DIR
# and confirms with the Enter key.
wait_for_apk() {
    echo -e "\n${WHITE}⚠️ ACTION REQUIRED ⚠️${NC}"
    echo -e "Please download the Raw APK or Bundle (.apk / .apkm / .xapk) of the app you want to patch."
    echo -e "Place the file(s) into this specific folder:"
    echo -e "${YELLOW}$ECO_DIR${NC}"
    echo -e "\nThe tool is in standby mode..."

    read -r -p "$(echo -e "${CYAN}Press [ENTER] when you have placed the file(s) to continue...${NC}")" _
}

# Scans ECO_DIR for .apk/.apkm/.xapk files and lets the user pick one.
# Saves/restores the caller's nullglob state so we don't pollute it.
select_apk() {
    echo -e "\n${WHITE}Scanning $ECO_DIR for APKs...${NC}"

    local nullglob_was_set=0
    if shopt -q nullglob; then
        nullglob_was_set=1
    fi
    shopt -s nullglob

    local apk_files=("$ECO_DIR/"*.apk "$ECO_DIR/"*.apkm "$ECO_DIR/"*.xapk)

    if [[ $nullglob_was_set -eq 0 ]]; then
        shopt -u nullglob
    fi

    if [[ ${#apk_files[@]} -eq 0 ]]; then
        echo -e "${RED}[ERROR] No APK/Bundle files found in $ECO_DIR!${NC}" >&2
        exit 1
    fi

    apk_files+=("Exit")

    echo -e "${WHITE}Select the file to patch:${NC}"
    local PS3_BAK="${PS3:-}"
    local choice
    PS3="Select APK number: "

    select choice in "${apk_files[@]}"; do
        if [[ "$choice" == "Exit" ]]; then
            exit 0
        elif [[ -n "$choice" ]]; then
            APK_CHOICE="$choice"
            echo -e "${GREEN}Target: $(basename "$APK_CHOICE")${NC}"
            break
        else
            echo -e "${RED}Invalid selection.${NC}" >&2
        fi
    done

    PS3="$PS3_BAK"
}

# Runs the Morphe CLI on the chosen APK. The java command is built as
# an array so we can conditionally add --force without string surgery.
# Pre-release track enables --force to permit version mismatches; Stable
# leaves it off for strict compatibility checking.
execute_patch() {
    local base_name
    base_name=$(basename "$APK_CHOICE")

    local final_apk="$ECO_DIR/Patched-${base_name%.*}.apk"
    TEMP_LOG_FILE="$WORK_DIR/patch_log_$(date +%s).txt"

    local java_cmd=(
        java -jar "$MORPHE_JAR" patch
        -b "$TEMP_PATCH"
        -a "$APK_CHOICE"
        -o "$final_apk"
    )
    if [[ "$TRACK_CHOICE" == "Pre-release" ]]; then
        java_cmd+=("--force")
    fi

    echo -e "\n${YELLOW}[INFO] Starting the patching process... (Do not close Termux!)${NC}"
    if [[ "$TRACK_CHOICE" == "Pre-release" ]]; then
        echo -e "${CYAN}[INFO] Pre-release track: running with --force.${NC}"
    fi

    # tee duplicates stdout/stderr to both the terminal and the temp log.
    if "${java_cmd[@]}" 2>&1 | tee "$TEMP_LOG_FILE"; then
        echo -e "\n${CYAN}=========================================${NC}"
        echo -e "${GREEN} SUCCESS! PATCHING COMPLETED             ${NC}"
        echo -e "${CYAN}=========================================${NC}"
        echo -e "${YELLOW}Your patched app is ready at:${NC}\n$final_apk"

        local export_log
        read -r -p "$(echo -e "\n${WHITE}Do you want to export the debug log to the ecosystem folder? (y/n)${NC} ")" export_log
        if [[ "$export_log" =~ ^[Yy]$ ]]; then
            cp "$TEMP_LOG_FILE" "$ECO_DIR/"
            echo -e "${GREEN}Log saved to: $ECO_DIR/$(basename "$TEMP_LOG_FILE")${NC}"
        fi
    else
        echo -e "\n${RED}[ERROR] Patching failed!${NC}" >&2
        cp "$TEMP_LOG_FILE" "$ECO_DIR/"
        echo -e "${YELLOW}Check the error log here: $ECO_DIR/$(basename "$TEMP_LOG_FILE")${NC}"
        exit 1
    fi
}

# ─────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────

# Orchestrates the interactive flow: bootstrap → select → download →
# patch. Any step may exit early; cleanup() handles the rest.
main() {
    echo -e "${CYAN}=========================================${NC}"
    echo -e "${YELLOW}       CHIHAFUYU LOCAL BUILDER           ${NC}"
    echo -e "${CYAN}=========================================${NC}\n"

    mkdir -p "$WORK_DIR"
    cd "$WORK_DIR" || exit 1

    check_dependencies
    ensure_storage_access

    select_ecosystem
    show_supported_apps
    select_track
    fetch_components
    wait_for_apk
    select_apk
    execute_patch
}

main
