#!/bin/bash
set -euo pipefail
umask 077

# Define colors for output
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
WHITE='\033[1;37m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}=========================================${NC}"
echo -e "${YELLOW}       CUSTOM KEYSTORE GENERATOR         ${NC}"
echo -e "${CYAN}=========================================${NC}\n"

if ! command -v keytool &> /dev/null; then
    echo -e "${RED}Error: 'keytool' command not found. Please ensure Java (JDK) is installed and added to your system PATH.${NC}" >&2
    exit 1
fi

if ! command -v base64 &> /dev/null; then
    echo -e "${RED}Error: 'base64' command not found. This utility is required for encoding.${NC}" >&2
    exit 1
fi

read -r -p "1. Enter the Keystore alias (e.g., myalias) [Default: myalias]: " ALIAS
ALIAS=${ALIAS:-myalias}

if [[ ! "$ALIAS" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo -e "${RED}Error: Alias can only contain letters, numbers, dots, underscores, and hyphens.${NC}" >&2
    exit 1
fi

echo -e "\n${WHITE}2. Enter Distinguished Name (DNAME) Information:${NC}"

read -r -p "   a. First and Last Name (CN) [Default: Android Debug]: " CN
CN=${CN:-Android Debug}

read -r -p "   b. Organizational Unit (OU) [Default: Patcher]: " OU
OU=${OU:-Patcher}

read -r -p "   c. Organization Name (O) [Default: Android]: " O
O=${O:-Android}

read -r -p "   d. City or Locality (L) [Default: Unknown]: " L
L=${L:-Unknown}

read -r -p "   e. State or Province (S) [Default: Unknown]: " S
S=${S:-Unknown}

read -r -p "   f. Two-Letter Country Code (C) (e.g., US) [Default: US]: " C
C=${C:-US}

if [[ ! "$C" =~ ^[A-Za-z]{2}$ ]]; then
    echo -e "${RED}Error: Country code must contain exactly two letters (e.g., US, ID, UK).${NC}" >&2
    exit 1
fi

# Prevent DN semantic injection by safely rejecting restricted characters and newlines
validate_dn() {
    local name="$1"
    local value="$2"
    case "$value" in
        *','*|*'+'*|*'='*|*'\'*|*'"'*|'<'*|'>'*|*';'*)
            echo -e "${RED}Error: '$name' contains unsupported characters ( , + = \\ \" < > ; ).${NC}" >&2
            exit 1
            ;;
    esac
    if [[ "$value" =~ $'\n' || "$value" =~ $'\r' ]]; then
        echo -e "${RED}Error: '$name' contains a newline character.${NC}" >&2
        exit 1
    fi
}

validate_dn "CN" "$CN"
validate_dn "OU" "$OU"
validate_dn "O" "$O"
validate_dn "L" "$L"
validate_dn "S" "$S"

DNAME="CN=$CN, OU=$OU, O=$O, L=$L, S=$S, C=$C"

KEYSTORE_FILE="custom_keystore.keystore"
BASE64_FILE="custom_keystore_base64.txt"

# Ensure we don't accidentally overwrite existing keys or follow broken symlinks
for output in "$KEYSTORE_FILE" "$BASE64_FILE"; do
    if [[ -e "$output" || -L "$output" ]]; then
        echo -e "${RED}Error: Output file '$output' already exists. Refusing to overwrite to prevent accidental key loss.${NC}" >&2
        exit 1
    fi
done

echo -e "\n${WHITE}3. Security Configuration:${NC}"

while true; do
    read -r -s -p "   Enter Keystore password (minimum 6 characters): " PASSWORD
    echo
    if [ ${#PASSWORD} -lt 6 ]; then
        echo -e "${RED}   Error: The password must be at least 6 characters long.${NC}"
        continue
    fi
    
    read -r -s -p "   Confirm Keystore password: " PASSWORD_CONFIRM
    echo
    if [[ "$PASSWORD" != "$PASSWORD_CONFIRM" ]]; then
        echo -e "${RED}   Error: Passwords do not match. Please try again.${NC}"
        unset PASSWORD PASSWORD_CONFIRM
        continue
    fi
    
    unset PASSWORD_CONFIRM
    break
done

echo -e "\n${YELLOW}[INFO] Generating the new Keystore file...${NC}"

# Secure transaction via temp directory
WORK_DIR="$(mktemp -d)"
trap 'unset PASSWORD PASSWORD_CONFIRM 2>/dev/null; rm -rf -- "$WORK_DIR"' EXIT INT TERM HUP

PASSWORD_FILE="$WORK_DIR/pass"
TMP_KEYSTORE="$WORK_DIR/temp.keystore"
TMP_BASE64="$WORK_DIR/temp.txt"

printf '%s' "$PASSWORD" > "$PASSWORD_FILE"
chmod 600 "$PASSWORD_FILE"
unset PASSWORD

# Keystore generation (allowing stderr to pass through for debugging)
if keytool -genkeypair \
    -keystore "$TMP_KEYSTORE" \
    -storetype PKCS12 \
    -alias "$ALIAS" \
    -keyalg RSA \
    -keysize 4096 \
    -validity 36500 \
    -storepass:file "$PASSWORD_FILE" \
    -keypass:file "$PASSWORD_FILE" \
    -dname "$DNAME" > /dev/null; then

    echo -e "${YELLOW}[INFO] Verifying generated keystore structure...${NC}"
    
    # Verify the integrity of the generated keystore
    if ! keytool -list -keystore "$TMP_KEYSTORE" -storepass:file "$PASSWORD_FILE" -alias "$ALIAS" > /dev/null 2>&1; then
        echo -e "${RED}Error: Generated Keystore failed integrity verification.${NC}" >&2
        exit 1
    fi

    echo -e "${YELLOW}[INFO] Converting the Keystore to a Base64 string...${NC}"

    # Portable Base64 conversion suitable for macOS/Linux
    base64 "$TMP_KEYSTORE" | tr -d '\r\n' > "$TMP_BASE64"
    chmod 600 "$TMP_KEYSTORE" "$TMP_BASE64"
    
    if [[ ! -s "$TMP_BASE64" ]]; then
        echo -e "${RED}Error: Generated Base64 output is empty.${NC}" >&2
        exit 1
    fi

    echo -e "${YELLOW}[INFO] Validating Base64 round-trip translation...${NC}"
    
    # Verify decoding matches original exactly
    base64 -d "$TMP_BASE64" > "$WORK_DIR/roundtrip.keystore"
    if ! cmp -s "$TMP_KEYSTORE" "$WORK_DIR/roundtrip.keystore"; then
        echo -e "${RED}Error: Base64 round-trip verification failed. File corruption detected.${NC}" >&2
        exit 1
    fi

    # Best-effort atomic move with rollback
    PUBLISHED_KEYSTORE=0
    mv -- "$TMP_KEYSTORE" "$KEYSTORE_FILE"
    PUBLISHED_KEYSTORE=1

    if ! mv -- "$TMP_BASE64" "$BASE64_FILE"; then
        if (( PUBLISHED_KEYSTORE )); then
            rm -f -- "$KEYSTORE_FILE"
        fi
        echo -e "${RED}Error: Failed to publish Base64 output.${NC}" >&2
        exit 1
    fi

    echo -e "\n${CYAN}=========================================${NC}"
    echo -e "${GREEN} SUCCESS! KEYSTORE GENERATED SECURELY${NC}"
    echo -e "${CYAN}=========================================${NC}"
    echo -e "${YELLOW}[INFO] Keystore saved to: '$KEYSTORE_FILE'${NC}"
    echo -e "${GREEN}[INFO] Base64 string saved to: '$BASE64_FILE'${NC}"
    echo -e "${WHITE}Please open the text file to copy your Base64 string for GitHub Secrets.${NC}\n"
else
    echo -e "${RED}Failed to generate the Keystore. Please review the error above.${NC}" >&2
    exit 1
fi
