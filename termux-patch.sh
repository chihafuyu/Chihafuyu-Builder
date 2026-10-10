#!/usr/bin/env bash
set -euo pipefail

CYAN='\033[0;36m'
YELLOW='\033[1;33m'
WHITE='\033[1;37m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

TERMUX_PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
TERMUX_HOME="${HOME:-/data/data/com.termux/files/home}"
USER_AGENT="ChihafuyuBuilder/2.0 (Termux; Android)"
WORK_DIR="$TERMUX_PREFIX/var/chihafuyu-workspace"
BASE_DIR="$TERMUX_HOME/storage/downloads/Chihafuyu"
MORPHE_JAR="$BASE_DIR/morphe.jar"
REPO_URL="https://raw.githubusercontent.com/chihafuyu/Chihafuyu-Builder/main"

TEMP_PATCH=""
ECO_CHOICE=""
TARGET_REPO=""
TARGET_JSON_PATH=""
ECO_DIR=""
TRACK_CHOICE=""
APK_CHOICE=""
TEMP_LOG_FILE=""

# Runs exactly once on shell exit. Signal handlers below convert INT/TERM
# into conventional exit codes so this handler is not invoked twice.
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

check_dependencies() {
    local missing=()
    command -v curl >/dev/null 2>&1 || missing+=("curl")
    command -v jq >/dev/null 2>&1 || missing+=("jq")

    # Morphe patches require Java 21.
    if ! java -version 2>&1 | grep -q 'version "21'; then
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

select_ecosystem() {
    echo -e "${YELLOW}[INFO] Fetching ecosystem map from repository...${NC}"

    local map_url="${REPO_URL}/ecosystem-termux/repo_map.json"
    local map_data=""

    if ! map_data=$(curl -sL --max-time 15 -A "$USER_AGENT" "$map_url"); then
        echo -e "${RED}[ERROR] Failed to fetch repo_map.json.${NC}" >&2
        exit 1
    fi

    if ! echo "$map_data" | jq -e 'type == "object"' >/dev/null 2>&1; then
        echo -e "${RED}[ERROR] Failed to parse repo_map.json.${NC}" >&2
        exit 1
    fi

    local eco_list=()
    mapfile -t eco_list < <(echo "$map_data" | jq -r 'keys[]')

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

            TARGET_REPO=$(echo "$map_data" | jq -r --arg eco "$ECO_CHOICE" '.[$eco].repo_url // empty')
            TARGET_JSON_PATH=$(echo "$map_data" | jq -r --arg eco "$ECO_CHOICE" '.[$eco].config_path // empty')

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

show_supported_apps() {
    echo -e "\n${YELLOW}[INFO] Fetching supported apps for $ECO_CHOICE...${NC}"
    local json_url="${REPO_URL}/${TARGET_JSON_PATH}"

    echo -e "${CYAN}=== Supported Applications ===${NC}"

    if ! curl -sL --max-time 15 -f "$json_url" \
        | jq -r '.[].apps | to_entries[] | " - \(.value.search_term) (v\(.value.stable[0] // "Any"))"'; then
        echo -e "${RED}[WARN] Could not fetch configuration from $TARGET_JSON_PATH.${NC}" >&2
    fi
    echo -e "${CYAN}==============================${NC}"
}

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
            break
        else
            echo -e "${RED}Invalid selection.${NC}" >&2
        fi
    done

    PS3="$PS3_BAK"
}

# Downloads url to dest using HTTP caching (-z).
# Uses a .tmp intermediate so a mid-download failure cannot corrupt dest.
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

fetch_components() {
    echo -e "\n${YELLOW}[INFO] Checking core components & synchronizing files...${NC}"

    local morphe_url="https://github.com/MorpheApp/morphe-cli/releases/latest/download/morphe-cli.jar"

    echo -e "${CYAN}Checking morphe-cli...${NC}"
    if ! _download_with_cache "$morphe_url" "$MORPHE_JAR" 300; then
        echo -e "${RED}[ERROR] Failed to download morphe-cli and no cache available.${NC}" >&2
        exit 1
    fi

    # GitHub API command as an array to preserve spaces in the token.
    local api_curl_cmd=(curl -sLf --max-time 15 -A "$USER_AGENT")
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        echo -e "${GREEN}[INFO] GitHub Token detected. Using authenticated API requests.${NC}"
        api_curl_cmd+=("-H" "Authorization: Bearer $GITHUB_TOKEN")
    fi

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
    patch_url=$(echo "$api_response" | jq -r "$jq_filter" | tail -n 1) || true

    if [[ -z "$patch_url" || "$patch_url" == "null" ]]; then
        echo -e "${RED}[ERROR] No .mpp file found for $ECO_CHOICE in $TARGET_REPO ($TRACK_CHOICE). API rate limit exceeded?${NC}" >&2
        exit 1
    fi

    TEMP_PATCH="${ECO_CHOICE}-${TRACK_CHOICE// /-}.mpp"

    echo -e "${CYAN}Checking ecosystem patches...${NC}"
    if ! _download_with_cache "$patch_url" "$TEMP_PATCH" 60; then
        echo -e "${RED}[ERROR] Failed to download ecosystem patches.${NC}" >&2
        exit 1
    fi
}

wait_for_apk() {
    echo -e "\n${WHITE}⚠️ ACTION REQUIRED ⚠️${NC}"
    echo -e "Please download the Raw APK or Bundle (.apk / .apkm / .xapk) of the app you want to patch."
    echo -e "Place the file(s) into this specific folder:"
    echo -e "${YELLOW}$ECO_DIR${NC}"
    echo -e "\nThe tool is in standby mode..."

    read -r -p "$(echo -e "${CYAN}Press [ENTER] when you have placed the file(s) to continue...${NC}")" _
}

select_apk() {
    echo -e "\n${WHITE}Scanning $ECO_DIR for APKs...${NC}"

    # Preserve the caller's nullglob state.
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

execute_patch() {
    local base_name
    base_name=$(basename "$APK_CHOICE")

    local final_apk="$ECO_DIR/Patched-${base_name%.*}.apk"
    TEMP_LOG_FILE="$WORK_DIR/patch_log_$(date +%s).txt"

    echo -e "\n${YELLOW}[INFO] Starting the patching process... (Do not close Termux!)${NC}"

    if java -jar "$MORPHE_JAR" patch -b "$TEMP_PATCH" -a "$APK_CHOICE" -o "$final_apk" 2>&1 | tee "$TEMP_LOG_FILE"; then
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
