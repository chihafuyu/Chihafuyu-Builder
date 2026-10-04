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
USER_AGENT="ChihafuyuBuilder/1.3 (Termux; Android)"
WORK_DIR="$TERMUX_PREFIX/var/chihafuyu-workspace"
REPO_URL="https://raw.githubusercontent.com/chihafuyu/Chihafuyu-Builder/main"
API_URL="https://api.github.com/repos/chihafuyu/Chihafuyu-Builder"

TEMP_PATCH=""
ECO_CHOICE=""
TARGET_REPO=""
TARGET_JSON=""
TARGET_MPP=""
ECO_DIR=""
TRACK_CHOICE=""
APK_CHOICE=""
TEMP_LOG_FILE=""

# Cleanup routine triggered on exit, interrupt, or error.
# The .mpp file is intentionally retained to leverage smart caching on subsequent runs.
cleanup() {
    local exit_code=$?
    if [[ -n "$TEMP_LOG_FILE" && -f "$TEMP_LOG_FILE" ]]; then
        rm -f "$TEMP_LOG_FILE"
    fi
    exit "$exit_code"
}

trap cleanup EXIT INT TERM ERR

check_dependencies() {
    local missing=()
    command -v curl >/dev/null 2>&1 || missing+=("curl")
    command -v jq >/dev/null 2>&1 || missing+=("jq")
    
    # Enforce Java 21 requirement for Morphe patching
    if ! java -version 2>&1 | grep -q 'version "21'; then
        missing+=("openjdk-21")
    fi

    if [[ ${#missing[@]} -gt 0 ]]; then
        echo -e "${YELLOW}[INFO] Installing missing dependencies: ${missing[*]}${NC}"
        pkg update -y && pkg install "${missing[@]}" -y
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
            ((attempts++))
            if [[ $attempts -ge 30 ]]; then
                echo -e "${RED}[ERROR] Storage access timeout.${NC}" >&2
                exit 1
            fi
        done
    fi
}

# Dynamically extracts the target repository and patch filename by parsing the YAML workflow
fetch_yaml_config() {
    echo -e "${YELLOW}[INFO] Fetching patcher configuration for $ECO_CHOICE...${NC}"
    local yaml_url="${REPO_URL}/.github/workflows/${ECO_CHOICE}-patcher.yml"
    local yaml_content
    
    yaml_content=$(curl -sL --max-time 15 -A "$USER_AGENT" "$yaml_url")
    
    # Extract configuration variables using regex to match exact YAML keys
    TARGET_REPO=$(echo "$yaml_content" | sed -n 's/.*repo_url:[[:space:]]*"\(.*\)".*/\1/p' | head -n 1)
    TARGET_MPP=$(echo "$yaml_content" | sed -n 's/.*custom_patch_filename:[[:space:]]*"\(.*\)".*/\1/p' | head -n 1)
    
    if [[ -z "$TARGET_REPO" ]]; then
        echo -e "${RED}[WARN] Could not parse repo_url from YAML. Using fallback.${NC}" >&2
        TARGET_REPO="chihafuyu/morphe-patches"
    fi
    
    if [[ -z "$TARGET_MPP" ]]; then
        TARGET_MPP="${ECO_CHOICE}-custom.mpp"
    fi
    
    echo -e "${CYAN}Target Repo:${NC} $TARGET_REPO"
    echo -e "${CYAN}Target Patch File:${NC} $TARGET_MPP"
}

# Retrieves available ecosystems directly from the GitHub API directory contents
select_ecosystem() {
    echo -e "${YELLOW}[INFO] Fetching ecosystem list from GitHub API...${NC}"
    
    local api_response
    api_response=$(curl -s --max-time 15 -A "$USER_AGENT" "$API_URL/contents/ecosystem")
    
    if ! echo "$api_response" | jq -e 'type == "array"' >/dev/null; then
        echo -e "${RED}[ERROR] Failed to fetch ecosystem list. GitHub API rate limit?${NC}" >&2
        exit 1
    fi
    
    local eco_list=()
    mapfile -t eco_list < <(echo "$api_response" | jq -r '.[].name' | grep '\.json$' | sed 's/\.json$//')
    
    if [[ ${#eco_list[@]} -eq 0 ]]; then
        echo -e "${RED}[ERROR] No ecosystems found.${NC}" >&2
        exit 1
    fi
    
    eco_list+=("Exit")
    
    echo -e "\n${WHITE}Select Ecosystem Patches:${NC}"
    COLUMNS=20
    local PS3_BAK="${PS3:-}"
    PS3="Enter your choice: "
    
    select choice in "${eco_list[@]}"; do
        if [[ "$choice" == "Exit" ]]; then
            echo -e "${YELLOW}Exiting builder. Goodbye!${NC}"
            exit 0
        elif [[ -n "$choice" ]]; then
            ECO_CHOICE="$choice"
            echo -e "${GREEN}Selected ecosystem: $ECO_CHOICE${NC}"
            
            TARGET_JSON="${ECO_CHOICE}.json"
            fetch_yaml_config
            
            ECO_DIR="$TERMUX_HOME/storage/downloads/Chihafuyu-$ECO_CHOICE"
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
    local json_url="${REPO_URL}/ecosystem/${TARGET_JSON}"
    
    echo -e "${CYAN}=== Supported Applications ===${NC}"
    
    if ! curl -sL --max-time 15 -f "$json_url" | jq -r '.[].apps | to_entries[] | " - \(.value.search_term) (v\(.value.stable[0] // "Any"))"'; then
        echo -e "${RED}[WARN] Could not fetch configuration for $ECO_CHOICE.${NC}" >&2
    fi
    echo -e "${CYAN}==============================${NC}"
}

select_track() {
    echo -e "\n${WHITE}Select Patch Track:${NC}"
    local tracks=("Stable" "Pre-release" "Exit")
    
    local PS3_BAK="${PS3:-}"
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

fetch_components() {
    echo -e "\n${YELLOW}[INFO] Checking core components & synchronizing files...${NC}"
    
    local morphe_url="https://github.com/MorpheApp/morphe-cli/releases/latest/download/morphe-cli.jar"
    
    # Smart Cache Logic: Uses curl '-z' and '-R' to download the JAR only if a newer version exists on the server
    if [[ -s "morphe.jar" ]]; then
        echo -e "${CYAN}Checking for morphe-cli updates...${NC}"
        curl -sL --max-time 300 -R -z "morphe.jar" -A "$USER_AGENT" "$morphe_url" -o morphe.jar
    else
        echo -e "${CYAN}Downloading morphe-cli...${NC}"
        curl -sL --max-time 300 -R -A "$USER_AGENT" "$morphe_url" -o morphe.jar
    fi

    local patch_url=""
    if [[ "$TRACK_CHOICE" == "Stable" ]]; then
        patch_url="https://github.com/${TARGET_REPO}/releases/latest/download/${TARGET_MPP}"
    else
        patch_url=$(curl -s --max-time 15 -A "$USER_AGENT" "https://api.github.com/repos/${TARGET_REPO}/releases" | \
            jq -r --arg MPP "$TARGET_MPP" 'map(select(.prerelease == true)) | .[0].assets[]? | select(.name == $MPP) | .browser_download_url')
        
        if [[ -z "$patch_url" || "$patch_url" == "null" ]]; then
            echo -e "${RED}[ERROR] No Pre-release version found for $ECO_CHOICE in $TARGET_REPO.${NC}" >&2
            exit 1
        fi
    fi
    
    # Segregate local .mpp filenames by track to prevent cache validation conflicts between stable and pre-release
    TEMP_PATCH="${TARGET_MPP%.mpp}-${TRACK_CHOICE// /-}.mpp"
    
    # Smart Cache Logic: Downloads the .mpp file only if the remote file modification time is newer
    if [[ -s "$TEMP_PATCH" ]]; then
        echo -e "${CYAN}Checking for $TEMP_PATCH updates...${NC}"
        curl -sL --max-time 60 -R -z "$TEMP_PATCH" -A "$USER_AGENT" "$patch_url" -o "$TEMP_PATCH"
    else
        echo -e "${CYAN}Downloading $TEMP_PATCH...${NC}"
        curl -sL --max-time 60 -R -A "$USER_AGENT" "$patch_url" -o "$TEMP_PATCH"
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
    
    shopt -s nullglob
    local apk_files=("$ECO_DIR/"*.apk "$ECO_DIR/"*.apkm "$ECO_DIR/"*.xapk)
    shopt -u nullglob

    if [[ ${#apk_files[@]} -eq 0 ]]; then
        echo -e "${RED}[ERROR] No APK/Bundle files found in $ECO_DIR!${NC}" >&2
        exit 1
    fi

    apk_files+=("Exit")

    echo -e "${WHITE}Select the file to patch:${NC}"
    local PS3_BAK="${PS3:-}"
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

    if java -jar morphe.jar patch -b "$TEMP_PATCH" -a "$APK_CHOICE" -o "$final_apk" 2>&1 | tee "$TEMP_LOG_FILE"; then
        echo -e "\n${CYAN}=========================================${NC}"
        echo -e "${GREEN} SUCCESS! PATCHING COMPLETED             ${NC}"
        echo -e "${CYAN}=========================================${NC}"
        echo -e "${YELLOW}Your patched app is ready at:${NC}\n$final_apk"
        
        read -r -n 1 -p "$(echo -e "\n${WHITE}Do you want to export the debug log to the ecosystem folder? (y/n)${NC} ")" export_log
        echo ""
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
