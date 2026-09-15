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
USER_AGENT="ChihafuyuBuilder/1.0 (Termux; Android)"
TEMP_PATCH="patches.mpp"
WORK_DIR="$TERMUX_PREFIX/var/chihafuyu-workspace"
REPO_URL="https://raw.githubusercontent.com/chihafuyu/Chihafuyu-Builder/main"

cleanup() {
    local exit_code=$?
    if [[ -f "$TEMP_PATCH" ]]; then
        rm -f "$TEMP_PATCH"
    fi
    exit "$exit_code"
}

trap cleanup EXIT INT TERM ERR

check_dependencies() {
    local missing_pkgs=()

    if ! command -v curl > /dev/null 2>&1; then missing_pkgs+=("curl"); fi
    if ! command -v jq > /dev/null 2>&1; then missing_pkgs+=("jq"); fi
    if ! command -v java > /dev/null 2>&1; then missing_pkgs+=("openjdk-21"); fi

    if [[ ${#missing_pkgs[@]} -gt 0 ]]; then
        echo -e "${YELLOW}[INFO] Missing dependencies detected: ${missing_pkgs[*]}${NC}"
        echo -e "${YELLOW}[INFO] Updating system and installing packages... (This may take a while)${NC}"
        pkg update -y && pkg upgrade -y
        pkg install "${missing_pkgs[@]}" -y
        echo -e "${GREEN}[INFO] Dependencies installed successfully!${NC}\n"
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
                echo -e "${RED}[ERROR] Storage access timeout.${NC}"
                exit 1
            fi
        done
    fi
}

setup_workspace() {
    mkdir -p "$WORK_DIR"
    cd "$WORK_DIR" || exit 1
}

select_ecosystem() {
    echo -e "${WHITE}Select Ecosystem Patches:${NC}"
    local ecosystems=(
        "ajstrick81" "anxyis" "arandomhooman" "BholeyKaBhakt" "browzomje"
        "byehi98" "De-Vanced" "dh6k" "hoo-dles" "hxreborn"
        "icysymmetra" "jasonwu1994" "kiraio-moe" "kuchingneko28" "kveld9"
        "legendsciber" "MiguelNinja19" "morphe" "PathxmOp" "piko" "rabilrbl"
        "Riky" "rushiranpise" "SapitoSucio" "Exit"
    )
    COLUMNS=20
    select ECO_CHOICE in "${ecosystems[@]}"; do
        if [[ "$ECO_CHOICE" == "Exit" ]]; then
            echo -e "${YELLOW}Exiting builder. Goodbye!${NC}"
            exit 0
        elif [[ -n "$ECO_CHOICE" ]]; then
            echo -e "${GREEN}Selected ecosystem: $ECO_CHOICE${NC}"
            ECO_DIR="$TERMUX_HOME/storage/downloads/Chihafuyu-$ECO_CHOICE"
            mkdir -p "$ECO_DIR"
            break
        else
            echo -e "${RED}Invalid selection.${NC}"
        fi
    done
}

show_supported_apps() {
    echo -e "\n${YELLOW}[INFO] Fetching supported apps for $ECO_CHOICE...${NC}"
    local json_url="${REPO_URL}/ecosystem/${ECO_CHOICE}.json"
    
    if curl -sL -f "$json_url" -o eco.json; then
        echo -e "${CYAN}=== Supported Applications ===${NC}"
        jq -r '.[].apps | to_entries[] | " - \(.value.search_term) (v\(.value.stable[0] // "Any"))"' eco.json
        echo -e "${CYAN}==============================${NC}"
        rm -f eco.json
    else
        echo -e "${RED}[WARN] Could not fetch configuration for $ECO_CHOICE.${NC}"
        echo -e "${WHITE}Make sure the ecosystem name matches the JSON file in your repository.${NC}"
    fi
}

select_track() {
    echo -e "\n${WHITE}Select Patch Track:${NC}"
    local tracks=("Stable" "Pre-release" "Exit")
    select TRACK_CHOICE in "${tracks[@]}"; do
        if [[ "$TRACK_CHOICE" == "Exit" ]]; then
            echo -e "${YELLOW}Exiting builder. Goodbye!${NC}"
            exit 0
        elif [[ -n "$TRACK_CHOICE" ]]; then
            echo -e "${GREEN}Selected track: $TRACK_CHOICE${NC}"
            break
        else
            echo -e "${RED}Invalid selection.${NC}"
        fi
    done
}

fetch_components() {
    echo -e "\n${YELLOW}[INFO] Checking core components & downloading patches...${NC}"
    
    if [[ ! -f "morphe.jar" ]]; then
        curl -sL -A "$USER_AGENT" "https://github.com/MorpheApp/morphe-cli/releases/latest/download/morphe-cli.jar" -o morphe.jar
    fi

    local patch_url=""
    if [[ "$TRACK_CHOICE" == "Stable" ]]; then
        patch_url="https://github.com/${ECO_CHOICE}/morphe-patches/releases/latest/download/patches.mpp"
    else
        patch_url=$(curl -s -A "$USER_AGENT" "https://api.github.com/repos/${ECO_CHOICE}/morphe-patches/releases" | jq -r 'map(select(.prerelease == true)) | .[0].assets[] | select(.name == "patches.mpp") | .browser_download_url')
        if [[ "$patch_url" == "null" || -z "$patch_url" ]]; then
            echo -e "${RED}[ERROR] No Pre-release version found for $ECO_CHOICE.${NC}"
            exit 1
        fi
    fi
    curl -sL -A "$USER_AGENT" "$patch_url" -o "$TEMP_PATCH"
}

wait_for_apk() {
    echo -e "\n${WHITE}⚠️ ACTION REQUIRED ⚠️${NC}"
    echo -e "Please download the Raw APK or Bundle (.apk / .apkm / .xapk) of the app you want to patch."
    echo -e "Place the file(s) into this specific folder:"
    echo -e "${YELLOW}$ECO_DIR${NC}"
    echo -e "\nThe tool is in standby mode..."
    echo -e "${CYAN}Press [ENTER] when you have placed the file(s) to continue...${NC}"
    read -r _ || true
}

select_apk() {
    echo -e "\n${WHITE}Scanning $ECO_DIR for APKs...${NC}"
    shopt -s nullglob
    local apk_files=("$ECO_DIR/"*.apk "$ECO_DIR/"*.apkm "$ECO_DIR/"*.xapk)
    shopt -u nullglob

    if [[ ${#apk_files[@]} -eq 0 ]]; then
        echo -e "${RED}[ERROR] No APK/Bundle files found in $ECO_DIR!${NC}"
        echo -e "Make sure you moved the file correctly. Rerun the script to try again."
        exit 1
    fi

    apk_files+=("Exit")

    echo -e "${WHITE}Select the file to patch:${NC}"
    select APK_CHOICE in "${apk_files[@]}"; do
        if [[ "$APK_CHOICE" == "Exit" ]]; then
            echo -e "${YELLOW}Exiting builder. Goodbye!${NC}"
            exit 0
        elif [[ -n "$APK_CHOICE" ]]; then
            echo -e "${GREEN}Target: $(basename "$APK_CHOICE")${NC}"
            break
        else
            echo -e "${RED}Invalid selection.${NC}"
        fi
    done
}

execute_patch() {
    local base_name
    base_name=$(basename "$APK_CHOICE")
    
    local final_apk="$ECO_DIR/Patched-${base_name%.*}.apk"
    local log_file="$WORK_DIR/patch_log_$(date +%s).txt"
    local export_log=""

    echo -e "\n${YELLOW}[INFO] Starting the patching process... (Do not close Termux!)${NC}"

    if java -jar morphe.jar patch -b "$TEMP_PATCH" -a "$APK_CHOICE" -o "$final_apk" 2>&1 | tee "$log_file"; then
        echo -e "\n${CYAN}=========================================${NC}"
        echo -e "${GREEN} SUCCESS! PATCHING COMPLETED             ${NC}"
        echo -e "${CYAN}=========================================${NC}"
        echo -e "${YELLOW}Your patched app is ready at:${NC}"
        echo -e "$final_apk"
        
        echo -e "\n${WHITE}Do you want to export the debug log to the ecosystem folder? (y/n)${NC}"
        read -r -n 1 export_log || true
        echo ""
        if [[ "$export_log" =~ ^[Yy]$ ]]; then
            cp "$log_file" "$ECO_DIR/"
            echo -e "${GREEN}Log saved to: $ECO_DIR/$(basename "$log_file")${NC}"
        fi
    else
        echo -e "\n${RED}[ERROR] Patching failed!${NC}"
        echo -e "${WHITE}Exporting error log to ecosystem folder...${NC}"
        cp "$log_file" "$ECO_DIR/"
        echo -e "${YELLOW}Check the log here: $ECO_DIR/$(basename "$log_file")${NC}"
        exit 1
    fi
}

main() {
    echo -e "${CYAN}=========================================${NC}"
    echo -e "${YELLOW}       CHIHAFUYU LOCAL BUILDER           ${NC}"
    echo -e "${CYAN}=========================================${NC}\n"

    check_dependencies
    ensure_storage_access
    setup_workspace
    
    select_ecosystem
    show_supported_apps
    select_track
    fetch_components
    wait_for_apk
    select_apk
    execute_patch
}

main
