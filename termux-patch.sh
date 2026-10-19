#!/usr/bin/env bash
set -euo pipefail

# ─── Constants ───────────────────────────────────────────────────
CYAN='\033[0;36m'; YELLOW='\033[1;33m'; WHITE='\033[1;37m'
GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'

TERMUX_HOME="${HOME:-/data/data/com.termux/files/home}"
USER_AGENT="ChihafuyuBuilder/2.0 (Termux; Android)"

WORK_DIR="$TERMUX_HOME/.cache/chihafuyu-workspace"
BASE_DIR="$TERMUX_HOME/storage/downloads/Chihafuyu"
MORPHE_JAR="$BASE_DIR/morphe.jar"
REPO_URL="https://raw.githubusercontent.com/chihafuyu/Chihafuyu-Builder/main"

# ─── Runtime state ───────────────────────────────────────────────
TEMP_PATCH="" ECO_CHOICE="" TARGET_REPO="" TARGET_JSON_PATH=""
ECO_DIR="" TRACK_CHOICE="" APK_CHOICE="" TEMP_LOG_FILE=""
WAKELOCK_HELD=0
KEYSTORE_PATH="" KEYSTORE_ALIAS="Morphe"
KEYSTORE_STORE_PASS="" KEYSTORE_ENTRY_PASS="Morphe" KEYSTORE_SIGNER="Morphe"

# ─── Cleanup ─────────────────────────────────────────────────────
cleanup() {
    local code=$?
    trap - EXIT INT TERM
    if [[ $WAKELOCK_HELD -eq 1 ]]; then
        termux-wake-unlock >/dev/null 2>&1 || true
    fi
    if [[ -n "$TEMP_LOG_FILE" && -f "$TEMP_LOG_FILE" ]]; then
        rm -f "$TEMP_LOG_FILE"
    fi
    exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ─── Menu helper ─────────────────────────────────────────────────
_select_menu() {
    local prompt="$1"
    shift 1
    local items=("$@") backup="${PS3:-}" choice
    PS3="$prompt"
    select choice in "${items[@]}"; do
        if [[ -n "$choice" ]]; then
            PS3="$backup"
            printf '%s' "$choice"
            return 0
        fi
        echo -e "${RED}Invalid selection.${NC}" >&2
    done
    PS3="$backup"
    return 1
}

# ─── Pre-flight ──────────────────────────────────────────────────
check_dependencies() {
    local missing=()
    command -v curl >/dev/null 2>&1 || missing+=("curl")
    command -v jq   >/dev/null 2>&1 || missing+=("jq")
    if ! command -v java >/dev/null 2>&1 \
        || ! java -version 2>&1 | grep -q 'version "21'; then
        missing+=("openjdk-21")
    fi
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo -e "${YELLOW}[INFO] Installing: ${missing[*]}${NC}"
        pkg update -y || true
        if ! pkg install "${missing[@]}" -y; then
            echo -e "${RED}[ERROR] Failed to install dependencies. Check your internet connection or Termux mirrors.${NC}" >&2
            exit 1
        fi
    fi
}

ensure_storage_access() {
    local target="$TERMUX_HOME/storage/downloads"
    if [[ -d "$target" ]]; then
        return 0
    fi
    echo -e "${YELLOW}[INFO] Requesting internal storage access...${NC}"
    termux-setup-storage
    local attempts=0
    while [[ ! -d "$target" ]]; do
        sleep 1
        attempts=$((attempts + 1))
        if [[ $attempts -ge 30 ]]; then
            echo -e "${RED}[ERROR] Storage access timeout.${NC}" >&2
            exit 1
        fi
    done
}

# ─── Selectors ───────────────────────────────────────────────────
select_ecosystem() {
    echo -e "${YELLOW}[INFO] Fetching ecosystem map...${NC}"
    local map_url="${REPO_URL}/ecosystem-termux/repo_map.json"
    local map_data
    if ! map_data=$(curl -sL --max-time 30 -A "$USER_AGENT" "$map_url"); then
        echo -e "${RED}[ERROR] Failed to fetch repo_map.json.${NC}" >&2
        exit 1
    fi
    if ! printf '%s' "$map_data" | jq -e 'type == "object"' >/dev/null 2>&1; then
        echo -e "${RED}[ERROR] Failed to parse repo_map.json.${NC}" >&2
        exit 1
    fi

    local eco_list=()
    mapfile -t eco_list < <(printf '%s' "$map_data" | jq -r 'to_entries[] | select(.value.aggregate_repos == null) | .key')
    
    if [[ ${#eco_list[@]} -eq 0 ]]; then
        echo -e "${RED}[ERROR] No ecosystems found.${NC}" >&2
        exit 1
    fi
    eco_list+=("Exit")

    echo -e "\n${WHITE}Select Ecosystem Patches:${NC}"
    export COLUMNS=20
    ECO_CHOICE=$(_select_menu "Enter your choice: " "${eco_list[@]}")
    
    echo -e "${GREEN}Selected: $ECO_CHOICE${NC}"

    TARGET_REPO=$(printf '%s' "$map_data" | jq -r --arg e "$ECO_CHOICE" '.[$e].repo_url // empty')
    TARGET_JSON_PATH=$(printf '%s' "$map_data" | jq -r --arg e "$ECO_CHOICE" '.[$e].config_path // empty')
    if [[ -z "$TARGET_REPO" ]]; then
        TARGET_REPO="chihafuyu/morphe-patches"
    fi
    if [[ -z "$TARGET_JSON_PATH" ]]; then
        TARGET_JSON_PATH="ecosystem/${ECO_CHOICE}.json"
    fi

    echo -e "${CYAN}Target Repo:${NC} $TARGET_REPO"
    ECO_DIR="$BASE_DIR/$ECO_CHOICE"
    mkdir -p "$ECO_DIR"
}

show_supported_apps() {
    echo -e "\n${YELLOW}[INFO] Supported apps for $ECO_CHOICE:${NC}"
    local json_url="${REPO_URL}/${TARGET_JSON_PATH}"
    local jq_filter
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

    echo -e "${CYAN}=== Supported Applications ===${NC}"
    if ! curl -sL --max-time 30 -f "$json_url" | jq -r "$jq_filter"; then
        echo -e "${RED}[WARN] Could not fetch $TARGET_JSON_PATH.${NC}" >&2
    fi
    echo -e "${CYAN}==============================${NC}"
}

select_track() {
    echo -e "\n${WHITE}Select Patch Track:${NC}"
    TRACK_CHOICE=$(_select_menu "Enter track number: " \
        "Stable" "Pre-release" "Back" "Exit")
        
    echo -e "${GREEN}Selected track: $TRACK_CHOICE${NC}"

    if [[ "$TRACK_CHOICE" == "Pre-release" ]]; then
        echo -e "\n${YELLOW}⚠️  Pre-release track selected.${NC}"
        echo -e "${YELLOW}    Patches are experimental; bugs and crashes possible.${NC}"
        echo -e "${CYAN}    --force will be applied to skip version checks.${NC}\n"
    fi
}

# ─── Downloads ───────────────────────────────────────────────────
_download_with_cache() {
    local url="$1" dest="$2" timeout="$3"
    local tmp="${dest}.tmp"

    if [[ -s "$dest" ]]; then
        if ! curl -sLf --max-time "$timeout" -R -z "$dest" \
            -A "$USER_AGENT" "$url" -o "$tmp"; then
            rm -f "$tmp"
            echo -e "${YELLOW}[WARN] Update check failed; keeping cache.${NC}" >&2
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
}

fetch_components() {
    echo -e "\n${YELLOW}[INFO] Checking components...${NC}"

    local api_cmd=(curl -sLf --max-time 30 -A "$USER_AGENT")
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        echo -e "${GREEN}[INFO] Using authenticated GitHub API.${NC}"
        api_cmd+=("-H" "Authorization: Bearer $GITHUB_TOKEN")
    fi

    echo -e "${CYAN}Checking morphe-cli...${NC}"
    local cli_json
    if ! cli_json=$("${api_cmd[@]}" \
        "https://api.github.com/repos/MorpheApp/morphe-desktop/releases/latest"); then
        echo -e "${RED}[ERROR] Failed to query Morphe CLI release.${NC}" >&2
        exit 1
    fi
    local morphe_url
    morphe_url=$(printf '%s' "$cli_json" | jq -r '
        .assets[]? | select(.name | test("morphe-desktop-.*-all\\.jar$"))
        | .browser_download_url' | tail -n1) || true
    if [[ -z "$morphe_url" || "$morphe_url" == "null" ]]; then
        echo -e "${RED}[ERROR] No Morphe CLI JAR asset found.${NC}" >&2
        exit 1
    fi
    if ! _download_with_cache "$morphe_url" "$MORPHE_JAR" 300; then
        echo -e "${RED}[ERROR] Failed to download morphe-cli.${NC}" >&2
        exit 1
    fi

    local api_url jq_filter
    if [[ "$TRACK_CHOICE" == "Stable" ]]; then
        api_url="https://api.github.com/repos/${TARGET_REPO}/releases/latest"
        jq_filter='.assets[]? | select(.name | endswith(".mpp")) | .browser_download_url'
    else
        api_url="https://api.github.com/repos/${TARGET_REPO}/releases"
        jq_filter='map(select(.prerelease == true)) | .[0].assets[]? | select(.name | endswith(".mpp")) | .browser_download_url'
    fi

    local api_resp
    if ! api_resp=$("${api_cmd[@]}" "$api_url"); then
        echo -e "${RED}[ERROR] Failed to query $TARGET_REPO.${NC}" >&2
        exit 1
    fi

    local patch_url
    patch_url=$(printf '%s' "$api_resp" | jq -r "$jq_filter" | tail -n1) || true
    if [[ -z "$patch_url" || "$patch_url" == "null" ]]; then
        echo -e "${RED}[ERROR] No .mpp for $ECO_CHOICE in $TARGET_REPO ($TRACK_CHOICE).${NC}" >&2
        exit 1
    fi

    TEMP_PATCH="${ECO_CHOICE}-${TRACK_CHOICE// /-}.mpp"
    echo -e "${CYAN}Checking ecosystem patches...${NC}"
    if ! _download_with_cache "$patch_url" "$WORK_DIR/$TEMP_PATCH" 60; then
        echo -e "${RED}[ERROR] Failed to download patches.${NC}" >&2
        exit 1
    fi
}

# ─── Keystore ────────────────────────────────────────────────────
_read_cfg() {
    local file="$1" key="$2"
    if [[ ! -f "$file" ]]; then
        return 0
    fi
    grep -m1 "^${key}=" "$file" 2>/dev/null | cut -d= -f2-
}

_autodetect_keystore() {
    local nullglob_was_set=0
    if shopt -q nullglob; then
        nullglob_was_set=1
    fi
    shopt -s nullglob
    local candidates=(
        "$BASE_DIR"/*.jks "$BASE_DIR"/*.keystore "$BASE_DIR"/*.bks
        "$BASE_DIR"/*.p12 "$BASE_DIR"/*.pfx
    )
    if [[ $nullglob_was_set -eq 0 ]]; then
        shopt -u nullglob
    fi
    if [[ ${#candidates[@]} -gt 0 ]]; then
        printf '%s' "${candidates[0]}"
    fi
}

load_keystore_config() {
    local cfg="${KEYSTORE_CONFIG:-$BASE_DIR/keystore.txt}"
    local cfg_path="" cfg_alias="" cfg_store="" cfg_entry="" cfg_signer=""

    if [[ -f "$cfg" ]]; then
        echo -e "${GREEN}[INFO] Reading keystore config: $cfg${NC}"
        if [[ -n "$(find "$cfg" -perm /go+r 2>/dev/null)" ]]; then
            echo -e "${YELLOW}[WARN] $cfg is group/world readable (chmod 600).${NC}" >&2
        fi
        cfg_path=$(_read_cfg "$cfg" path)
        cfg_alias=$(_read_cfg "$cfg" alias)
        cfg_store=$(_read_cfg "$cfg" store_password)
        cfg_entry=$(_read_cfg "$cfg" entry_password)
        cfg_signer=$(_read_cfg "$cfg" signer)
    fi

    if [[ -z "${KEYSTORE_PATH:-}" ]]; then
        if [[ -n "$cfg_path" ]]; then
            KEYSTORE_PATH="$cfg_path"
        else
            KEYSTORE_PATH=$(_autodetect_keystore)
        fi
    fi

    if [[ -z "$KEYSTORE_PATH" ]]; then
        echo -e "${CYAN}[INFO] No custom keystore; using Morphe default.${NC}"
        return 0
    fi
    if [[ ! -f "$KEYSTORE_PATH" ]]; then
        echo -e "${RED}[ERROR] Keystore not found: $KEYSTORE_PATH${NC}" >&2
        KEYSTORE_PATH=""
        return 0
    fi
    echo -e "${GREEN}[INFO] Using custom keystore: $(basename "$KEYSTORE_PATH")${NC}"

    if [[ -z "${KEYSTORE_ALIAS:-}" ]]; then
        KEYSTORE_ALIAS="${cfg_alias:-Morphe}"
    fi
    if [[ -z "${KEYSTORE_SIGNER:-}" ]]; then
        KEYSTORE_SIGNER="${cfg_signer:-Morphe}"
    fi

    if [[ -n "${KEYSTORE_STORE_PASSWORD:-}" ]]; then
        KEYSTORE_STORE_PASS="$KEYSTORE_STORE_PASSWORD"
    else
        KEYSTORE_STORE_PASS="${cfg_store:-}"
    fi

    if [[ -n "${KEYSTORE_PASSWORD:-}" ]]; then
        KEYSTORE_ENTRY_PASS="$KEYSTORE_PASSWORD"
    elif [[ -n "$cfg_entry" ]]; then
        KEYSTORE_ENTRY_PASS="$cfg_entry"
    else
        local input
        read -r -s -p "$(echo -e "${WHITE}Entry password (blank = 'Morphe'): ${NC}")" input
        echo ""
        KEYSTORE_ENTRY_PASS="${input:-Morphe}"
    fi

    echo -e "${CYAN}[INFO] Alias: $KEYSTORE_ALIAS | Signer: $KEYSTORE_SIGNER${NC}"
}

# ─── APK selection & patching ────────────────────────────────────
wait_for_apk() {
    echo -e "\n${WHITE}⚠️ ACTION REQUIRED ⚠️${NC}"
    echo -e "Download the APK or bundle (.apk / .apkm / .xapk) of the app to patch."
    echo -e "Place the file(s) into:"
    echo -e "${YELLOW}$ECO_DIR${NC}"

    read -r -p "$(echo -e "${CYAN}Press [ENTER] when ready...${NC}")" _
}

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
        echo -e "${RED}[ERROR] No APK/Bundle files found.${NC}" >&2
        exit 1
    fi
    apk_files+=("Back" "Exit")

    echo -e "${WHITE}Select the file to patch:${NC}"
    APK_CHOICE=$(_select_menu "Select APK number: " "${apk_files[@]}")
    
    if [[ "$APK_CHOICE" != "Exit" && "$APK_CHOICE" != "Back" ]]; then
        echo -e "${GREEN}Target: $(basename "$APK_CHOICE")${NC}"
    fi
}

execute_patch() {
    local base_name
    base_name=$(basename "$APK_CHOICE")
    local final_apk="$ECO_DIR/Patched-${base_name%.*}.apk"
    TEMP_LOG_FILE="$WORK_DIR/patch_log_$(date +%s).txt"

    local java_cmd=(
        java -jar "$MORPHE_JAR" patch
        --patches "$WORK_DIR/$TEMP_PATCH"
        --out "$final_apk"
        --bytecode-mode FULL
    )

    if [[ -n "$KEYSTORE_PATH" ]]; then
        java_cmd+=(
            --keystore "$KEYSTORE_PATH"
            --keystore-password "$KEYSTORE_STORE_PASS"
            --keystore-entry-alias "$KEYSTORE_ALIAS"
            --keystore-entry-password "$KEYSTORE_ENTRY_PASS"
            --signer "$KEYSTORE_SIGNER"
        )
    fi
    if [[ "$TRACK_CHOICE" == "Pre-release" ]]; then
        java_cmd+=("--force")
    fi
    java_cmd+=("$APK_CHOICE")

    echo -e "\n${YELLOW}[INFO] Patching... (do not close Termux)${NC}"
    if [[ "$TRACK_CHOICE" == "Pre-release" ]]; then
        echo -e "${CYAN}[INFO] Pre-release: --force enabled.${NC}"
    fi
    if [[ -n "$KEYSTORE_PATH" ]]; then
        echo -e "${CYAN}[INFO] Signing with: $(basename "$KEYSTORE_PATH")${NC}"
    fi

    if "${java_cmd[@]}" 2>&1 | tee "$TEMP_LOG_FILE"; then
        echo -e "\n${CYAN}=========================================${NC}"
        echo -e "${GREEN} SUCCESS! PATCHING COMPLETED             ${NC}"
        echo -e "${CYAN}=========================================${NC}"
        echo -e "${YELLOW}Output:${NC} $final_apk"

        local export_log
        read -r -p "$(echo -e "${WHITE}Export debug log to ecosystem folder? (y/n)${NC} ")" export_log
        if [[ "$export_log" =~ ^[Yy]$ ]]; then
            cp "$TEMP_LOG_FILE" "$ECO_DIR/"
            echo -e "${GREEN}Log saved to: $ECO_DIR/$(basename "$TEMP_LOG_FILE")${NC}"
        fi
    else
        echo -e "\n${RED}[ERROR] Patching failed!${NC}" >&2
        cp "$TEMP_LOG_FILE" "$ECO_DIR/"
        echo -e "${YELLOW}Check: $ECO_DIR/$(basename "$TEMP_LOG_FILE")${NC}"
        exit 1
    fi
}

# ─── Entrypoint ──────────────────────────────────────────────────
main() {
    echo -e "${CYAN}=========================================${NC}"
    echo -e "${YELLOW}       CHIHAFUYU LOCAL BUILDER           ${NC}"
    echo -e "${CYAN}=========================================${NC}\n"

    mkdir -p "$WORK_DIR"
    cd "$WORK_DIR" || exit 1

    check_dependencies
    ensure_storage_access

    # State machine for Global Back navigation
    local step=1
    while true; do
        case "$step" in
            1)
                select_ecosystem
                if [[ "$ECO_CHOICE" == "Exit" ]]; then
                    echo -e "${YELLOW}Exiting builder. Goodbye!${NC}"
                    exit 0
                fi
                step=2
                ;;
            2)
                show_supported_apps
                step=3
                ;;
            3)
                select_track
                if [[ "$TRACK_CHOICE" == "Exit" ]]; then
                    echo -e "${YELLOW}Exiting builder. Goodbye!${NC}"
                    exit 0
                elif [[ "$TRACK_CHOICE" == "Back" ]]; then
                    step=1
                else
                    step=4
                fi
                ;;
            4)
                fetch_components
                step=5
                ;;
            5)
                load_keystore_config
                step=6
                ;;
            6)
                wait_for_apk
                step=7
                ;;
            7)
                select_apk
                if [[ "$APK_CHOICE" == "Exit" ]]; then
                    echo -e "${YELLOW}Exiting builder. Goodbye!${NC}"
                    exit 0
                elif [[ "$APK_CHOICE" == "Back" ]]; then
                    step=3
                else
                    step=8
                fi
                ;;
            8)
                # Acquire wakelock just before the long-running patch.
                if command -v termux-wake-lock >/dev/null 2>&1; then
                    if termux-wake-lock >/dev/null 2>&1; then
                        WAKELOCK_HELD=1
                        echo -e "${GREEN}[INFO] Wakelock acquired.${NC}"
                    fi
                else
                    echo -e "${YELLOW}[WARN] termux-wake-lock missing; install termux-tools.${NC}" >&2
                fi
                echo -e "${CYAN}[TIP] Set Display → Screen timeout to 'Never' to keep${NC}"
                echo -e "${CYAN}      the screen on (wakelock only keeps CPU awake).${NC}\n"

                execute_patch
                break
                ;;
        esac
    done
}

main
