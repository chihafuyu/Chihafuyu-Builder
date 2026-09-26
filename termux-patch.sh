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
USER_AGENT="ChihafuyuBuilder/1.1 (Termux; Android)"
WORK_DIR="$TERMUX_PREFIX/var/chihafuyu-workspace"
REPO_URL="https://raw.githubusercontent.com/chihafuyu/Chihafuyu-Builder/main"

TEMP_PATCH=""
ECO_CHOICE=""
TARGET_REPO=""
TARGET_JSON=""
TARGET_MPP=""
ECO_DIR=""
TRACK_CHOICE=""
APK_CHOICE=""

# Clean up temporary files on exit or interrupt
cleanup() {
    local exit_code=$?
    if [[ -n "$TEMP_PATCH" && -f "$TEMP_PATCH" ]]; then
        rm -f "$TEMP_PATCH"
    fi
    exit "$exit_code"
}

trap cleanup EXIT INT TERM ERR

check_dependencies() {
    local missing=()

    command -v curl >/dev/null 2>&1 || missing+=("curl")
    command -v jq >/dev/null 2>&1 || missing+=("jq")
    
    # Enforce Java 21 requirement
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

set_eco_data() {
    TARGET_MPP="${ECO_CHOICE}-custom.mpp"
    TEMP_PATCH="$TARGET_MPP"

    case "$ECO_CHOICE" in
        "ajstrick81")    TARGET_REPO="ajstrick81/morphe-androidtv-patches"; TARGET_JSON="ajstrick81.json" ;;
        "Akash-Sriram")  TARGET_REPO="Akash-Sriram/morphe-google-photos"; TARGET_JSON="Akash-Sriram.json" ;;
        "andrewliang25") TARGET_REPO="andrewliang25/morphe-patches"; TARGET_JSON="andrewliang25.json" ;;
        "anxyis")        TARGET_REPO="anxyis/anxy-patches"; TARGET_JSON="anxyis.json" ;;
        "arandomhooman") TARGET_REPO="arandomhooman/hoomans-morphe-patches"; TARGET_JSON="arandomhooman.json" ;;
        "BholeyKaBhakt") TARGET_REPO="BholeyKaBhakt/android-patches-xtra"; TARGET_JSON="BholeyKaBhakt.json" ;;
        "browzomje")     TARGET_REPO="browzomje/browzomje-patches"; TARGET_JSON="browzomje.json" ;;
        "byehi98")       TARGET_REPO="byehi98/okish-morphe-patches"; TARGET_JSON="byehi98.json" ;;
        "De-Vanced")     TARGET_REPO="RookieEnough/De-Vanced"; TARGET_JSON="De-Vanced.json" ;;
        "dh6k")          TARGET_REPO="dh6k/morphe-patches"; TARGET_JSON="dh6k.json" ;;
        "heval99")       TARGET_REPO="heval99/Heval-Morphe-Patches"; TARGET_JSON="heval99.json" ;;
        "hoo-dles")      TARGET_REPO="hoo-dles/morphe-patches"; TARGET_JSON="hoo-dles.json" ;;
        "hushfacebook")  TARGET_REPO="SysAdminDoc/hushfacebook"; TARGET_JSON="hushfacebook.json" ;;
        "hushfeed")      TARGET_REPO="SysAdminDoc/hushfeed"; TARGET_JSON="hushfeed.json" ;;
        "hxreborn")      TARGET_REPO="hxreborn/morphe-patches"; TARGET_JSON="hxreborn.json" ;;
        "icysymmetra")   TARGET_REPO="icysymmetra/tiktok-patches-for-morphe"; TARGET_JSON="icysymmetra.json" ;;
        "jasonwu1994")   TARGET_REPO="jasonwu1994/Gboard-patches"; TARGET_JSON="jasonwu1994.json" ;;
        "kiraio-moe")    TARGET_REPO="kiraio-moe/Lain-Patches"; TARGET_JSON="kiraio-moe.json" ;;
        "kuchingneko28") TARGET_REPO="kuchingneko28/ipusnas-patches"; TARGET_JSON="kuchingneko28.json" ;;
        "kveld9")        TARGET_REPO="kveld9/kveld-morphe-patches"; TARGET_JSON="kveld9.json" ;;
        "legendsciber")  TARGET_REPO="legendsciber/morphe-patches"; TARGET_JSON="legendsciber.json" ;;
        "MiguelNinja19") TARGET_REPO="MiguelNinja19/miguel-morphe-patches"; TARGET_JSON="MiguelNinja19.json" ;;
        "morphe")        TARGET_REPO="MorpheApp/morphe-patches"; TARGET_JSON="morphe.json" ;;
        "PathxmOp")      TARGET_REPO="PrathxmOp/Prathxm-Patches"; TARGET_JSON="PathxmOp.json" ;;
        "piko")          TARGET_REPO="crimera/piko"; TARGET_JSON="piko.json" ;;
        "rabilrbl")      TARGET_REPO="rabilrbl/fluffy-patches"; TARGET_JSON="rabilrbl.json" ;;
        "Riky")          TARGET_REPO="riky-dev/morphe-patches"; TARGET_JSON="Riky.json" ;;
        "rushiranpise")  TARGET_REPO="rushiranpise/morphe-patches"; TARGET_JSON="rushiranpise.json" ;;
        "satanmerde")    TARGET_REPO="SatanMerde/D-moniakPatches"; TARGET_JSON="satanmerde.json" ;;
        "zeldrisho")     TARGET_REPO="zeldrisho/morphe-patches"; TARGET_JSON="zeldrisho.json" ;;
    esac
}

select_ecosystem() {
    echo -e "${WHITE}Select Ecosystem Patches:${NC}"
    local ecosystems=(
        "ajstrick81" "Akash-Sriram" "andrewliang25" "anxyis" "arandomhooman" "BholeyKaBhakt"
        "browzomje" "byehi98" "De-Vanced" "dh6k" "heval99" "hoo-dles" "hushfacebook"
        "hushfeed" "hxreborn" "icysymmetra" "jasonwu1994" "kiraio-moe" "kuchingneko28"
        "kveld9" "legendsciber" "MiguelNinja19" "morphe" "PathxmOp" "piko" "rabilrbl"
        "Riky" "rushiranpise" "satanmerde" "zeldrisho" "Exit"
    )
    
    COLUMNS=20
    local PS3_BAK="${PS3:-}"
    PS3="Enter your choice: "
    
    select choice in "${ecosystems[@]}"; do
        if [[ "$choice" == "Exit" ]]; then
            echo -e "${YELLOW}Exiting builder. Goodbye!${NC}"
            exit 0
        elif [[ -n "$choice" ]]; then
            ECO_CHOICE="$choice"
            echo -e "${GREEN}Selected ecosystem: $ECO_CHOICE${NC}"
            set_eco_data
            
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
    
    # Pipe curl directly to jq to avoid creating temporary files
    if ! curl -sL -f "$json_url" | jq -r '.[].apps | to_entries[] | " - \(.value.search_term) (v\(.value.stable[0] // "Any"))"'; then
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
    echo -e "\n${YELLOW}[INFO] Checking core components & downloading patches...${NC}"
    
    if [[ ! -f "morphe.jar" ]]; then
        curl -sL -A "$USER_AGENT" "https://github.com/MorpheApp/morphe-cli/releases/latest/download/morphe-cli.jar" -o morphe.jar
    fi

    local patch_url=""
    if [[ "$TRACK_CHOICE" == "Stable" ]]; then
        patch_url="https://github.com/${TARGET_REPO}/releases/latest/download/${TARGET_MPP}"
    else
        patch_url=$(curl -s -A "$USER_AGENT" "https://api.github.com/repos/${TARGET_REPO}/releases" | \
            jq -r --arg MPP "$TARGET_MPP" 'map(select(.prerelease == true)) | .[0].assets[]? | select(.name == $MPP) | .browser_download_url')
        
        if [[ -z "$patch_url" || "$patch_url" == "null" ]]; then
            echo -e "${RED}[ERROR] No Pre-release version found for $ECO_CHOICE.${NC}" >&2
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
    
    read -r -p "$(echo -e "${CYAN}Press [ENTER] when you have placed the file(s) to continue...${NC}")" _
}

select_apk() {
    echo -e "\n${WHITE}Scanning $ECO_DIR for APKs...${NC}"
    
    # Ensure glob expansion evaluates gracefully if no files exist
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
    local log_file
    
    # Separated declaration and assignment to avoid masking command substitution errors (SC2155)
    log_file="$WORK_DIR/patch_log_$(date +%s).txt"

    echo -e "\n${YELLOW}[INFO] Starting the patching process... (Do not close Termux!)${NC}"

    if java -jar morphe.jar patch -b "$TEMP_PATCH" -a "$APK_CHOICE" -o "$final_apk" 2>&1 | tee "$log_file"; then
        echo -e "\n${CYAN}=========================================${NC}"
        echo -e "${GREEN} SUCCESS! PATCHING COMPLETED             ${NC}"
        echo -e "${CYAN}=========================================${NC}"
        echo -e "${YELLOW}Your patched app is ready at:${NC}\n$final_apk"
        
        read -r -n 1 -p "$(echo -e "\n${WHITE}Do you want to export the debug log to the ecosystem folder? (y/n)${NC} ")" export_log
        echo ""
        if [[ "$export_log" =~ ^[Yy]$ ]]; then
            cp "$log_file" "$ECO_DIR/"
            echo -e "${GREEN}Log saved to: $ECO_DIR/$(basename "$log_file")${NC}"
        fi
    else
        echo -e "\n${RED}[ERROR] Patching failed!${NC}" >&2
        cp "$log_file" "$ECO_DIR/"
        echo -e "${YELLOW}Check the error log here: $ECO_DIR/$(basename "$log_file")${NC}"
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
