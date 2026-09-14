#!/usr/bin/env bash
set -eo pipefail

# UI Colors
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
WHITE='\033[1;37m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

# Global Variables
USER_AGENT="ChihafuyuBuilder/1.0 (Termux; Android)"
TEMP_PATCH="patches.mpp"
WORK_DIR="$PREFIX/var/chihafuyu-workspace"
OUT_DIR="$HOME/storage/downloads/Chihafuyu-Output"

# Cleanup function triggered on exit or interrupt
cleanup() {
    local exit_code=$?
    if [[ -f "$TEMP_PATCH" ]]; then
        rm -f "$TEMP_PATCH"
    fi
    exit $exit_code
}

trap cleanup EXIT INT TERM ERR

# Check and install system dependencies automatically
check_dependencies() {
    local missing_pkgs=()

    if ! command -v curl &> /dev/null; then missing_pkgs+=("curl"); fi
    if ! command -v jq &> /dev/null; then missing_pkgs+=("jq"); fi
    if ! command -v java &> /dev/null; then missing_pkgs+=("openjdk-21"); fi

    if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
        echo -e "${YELLOW}[INFO] Missing dependencies detected: ${missing_pkgs[*]}${NC}"
        echo -e "${YELLOW}[INFO] Updating system and installing packages... (This may take a while)${NC}"
        pkg update -y && pkg upgrade -y
        pkg install "${missing_pkgs[@]}" -y
        echo -e "${GREEN}[INFO] Dependencies installed successfully!${NC}\n"
    fi
}

# Ensure internal storage access in Termux
ensure_storage_access() {
    local target_dir="$HOME/storage/downloads"
    if [[ ! -d "$target_dir" ]]; then
        echo -e "${YELLOW}[INFO] Requesting internal storage access...${NC}"
        termux-setup-storage
        
        local attempts=0
        while [[ ! -d "$target_dir" ]]; do
            sleep 1
            ((attempts++))
            if [[ $attempts -ge 30 ]]; then
                echo -e "${RED}[ERROR] Storage access timeout.${NC}"
                exit 1
            fi
        done
    fi
}

# Setup working directories
setup_workspace() {
    mkdir -p "$WORK_DIR" "$OUT_DIR"
    cd "$WORK_DIR" || exit 1
}

# Select ecosystem patch
select_ecosystem() {
    echo -e "${WHITE}Select Ecosystem Patches:${NC}"
    local ecosystems=(
        "ajstrick81" "anxyis" "arandomhooman" "BholeyKaBhakt" "browzomje"
        "byehi98" "De-Vanced" "dh6k" "hoo-dles" "hxreborn"
        "icysymmetra" "jasonwu1994" "kiraio-moe" "kuchingneko28" "kveld9"
        "MiguelNinja19" "morphe" "PathxmOp" "piko" "rabilrbl"
        "Riky" "rushiranpise" "SapitoSucio"
    )
    COLUMNS=20
    select ECO_CHOICE in "${ecosystems[@]}"; do
        if [[ -n "$ECO_CHOICE" ]]; then
            echo -e "${GREEN}Selected ecosystem: $ECO_CHOICE${NC}"
            break
        else
            echo -e "${RED}Invalid selection.${NC}"
        fi
    done
}

# Select release track
select_track() {
    echo -e "\n${WHITE}Select Patch Track:${NC}"
    local tracks=("Stable" "Pre-release")
    select TRACK_CHOICE in "${tracks[@]}"; do
        if [[ -n "$TRACK_CHOICE" ]]; then
            echo -e "${GREEN}Selected track: $TRACK_CHOICE${NC}"
            break
        else
            echo -e "${RED}Invalid selection.${NC}"
        fi
    done
}

# Select target APK
select_apk() {
    echo -e "\n${WHITE}Searching for APK files in the Download folder...${NC}"
    shopt -s nullglob
    local apk_files=("$HOME/storage/downloads/"*.apk)
    shopt -u nullglob

    if [[ ${#apk_files[@]} -eq 0 ]]; then
        echo -e "${RED}[ERROR] No APK files found in the Download folder.${NC}"
        exit 1
    fi

    echo -e "${WHITE}Select the APK to patch:${NC}"
    select APK_CHOICE in "${apk_files[@]}"; do
        if [[ -n "$APK_CHOICE" ]]; then
            echo -e "${GREEN}Target APK: $(basename "$APK_CHOICE")${NC}"
            break
        else
            echo -e "${RED}Invalid selection.${NC}"
        fi
    done
}

# Fetch CLI tools and patches securely
fetch_components() {
    echo -e "\n${YELLOW}[INFO] Checking core components...${NC}"
    
    if [[ ! -f "morphe.jar" ]]; then
        echo -e "${CYAN}Downloading the latest Morphe CLI...${NC}"
        curl -sL -A "$USER_AGENT" "https://github.com/MorpheApp/morphe-cli/releases/latest/download/morphe-cli.jar" -o morphe.jar
    fi

    echo -e "${CYAN}Downloading patches ($TRACK_CHOICE) for $ECO_CHOICE...${NC}"
    local patch_url=""

    if [[ "$TRACK_CHOICE" == "Stable" ]]; then
        patch_url="https://github.com/${ECO_CHOICE}/morphe-patches/releases/latest/download/patches.mpp"
    else
        echo -e "${YELLOW}[INFO] Fetching Pre-release data via GitHub API...${NC}"
        patch_url=$(curl -s -A "$USER_AGENT" "https://api.github.com/repos/${ECO_CHOICE}/morphe-patches/releases" | jq -r 'map(select(.prerelease == true)) | .[0].assets[] | select(.name == "patches.mpp") | .browser_download_url')
        
        if [[ "$patch_url" == "null" || -z "$patch_url" ]]; then
            echo -e "${RED}[ERROR] No Pre-release version found in the $ECO_CHOICE repository.${NC}"
            exit 1
        fi
    fi

    curl -sL -A "$USER_AGENT" "$patch_url" -o "$TEMP_PATCH"
}

# Execute patching process
execute_patch() {
    local final_apk="$OUT_DIR/Patched-$(basename "$APK_CHOICE")"
    local log_file="$OUT_DIR/patch_log_$(date +%s).txt"

    echo -e "\n${YELLOW}[INFO] Starting the patching process... (Do not close Termux!)${NC}"

    if java -jar morphe.jar patch -b "$TEMP_PATCH" -a "$APK_CHOICE" -o "$final_apk" 2>&1 | tee "$log_file"; then
        echo -e "\n${CYAN}=========================================${NC}"
        echo -e "${GREEN} SUCCESS! PATCHING COMPLETED             ${NC}"
        echo -e "${CYAN}=========================================${NC}"
        echo -e "${YELLOW}Patched APK: $final_apk${NC}"
        echo -e "${YELLOW}Debug Log: $log_file${NC}"
    else
        echo -e "\n${RED}[ERROR] Patching failed! Check the log: $log_file${NC}"
        exit 1
    fi
}

# Main Execution Flow
main() {
    echo -e "${CYAN}=========================================${NC}"
    echo -e "${YELLOW}       CHIHAFUYU LOCAL BUILDER           ${NC}"
    echo -e "${CYAN}=========================================${NC}\n"

    check_dependencies
    ensure_storage_access
    setup_workspace
    select_ecosystem
    select_track
    select_apk
    fetch_components
    execute_patch
}

main
