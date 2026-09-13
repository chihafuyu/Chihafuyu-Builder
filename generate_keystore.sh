#!/bin/bash

# Clear the console for readability
clear

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

# Verify if the Java keytool utility is available in the system environment
if ! command -v keytool &> /dev/null; then
    echo -e "${RED}Error: 'keytool' command not found. Please ensure Java (JDK) is installed and added to your system PATH.${NC}"
    exit 1
fi

read -p "1. Enter the Keystore alias (e.g., myalias) [Default: myalias]: " ALIAS
ALIAS=${ALIAS:-myalias}

while true; do
    read -s -p "2. Enter the Keystore password (minimum 6 characters): " PASSWORD
    echo
    if [ ${#PASSWORD} -ge 6 ]; then
        break
    else
        echo -e "${RED}The password must be at least 6 characters long.${NC}"
    fi
done

echo -e "\n${WHITE}3. Enter Distinguished Name (DNAME) Information:${NC}"

read -p "   a. First and Last Name (CN) [Default: Android Debug]: " CN
CN=${CN:-Android Debug}

read -p "   b. Organizational Unit (OU) [Default: Patcher]: " OU
OU=${OU:-Patcher}

read -p "   c. Organization Name (O) [Default: Android]: " O
O=${O:-Android}

read -p "   d. City or Locality (L) [Default: Unknown]: " L
L=${L:-Unknown}

read -p "   e. State or Province (S) [Default: Unknown]: " S
S=${S:-Unknown}

read -p "   f. Two-Letter Country Code (C) (e.g., US) [Default: US]: " C
C=${C:-US}

DNAME="CN=$CN, OU=$OU, O=$O, L=$L, S=$S, C=$C"

KEYSTORE_FILE="custom_keystore.keystore"
BASE64_FILE="custom_keystore_base64.txt"

# Remove target files if they already exist to prevent keytool prompt hangs
rm -f "$KEYSTORE_FILE" "$BASE64_FILE"

echo -e "\n${YELLOW}[INFO] Generating the new Keystore file...${NC}"

if keytool -genkey -v \
    -keystore "$KEYSTORE_FILE" \
    -alias "$ALIAS" \
    -keyalg RSA \
    -keysize 4096 \
    -validity 36500 \
    -storepass "$PASSWORD" \
    -keypass "$PASSWORD" \
    -dname "$DNAME" > /dev/null 2>&1; then

    echo -e "${YELLOW}[INFO] Converting the Keystore to a Base64 string...${NC}"

    # Convert to Base64 without wrapping newlines to ensure compatibility with GitHub Secrets
    BASE64_STRING=$(base64 -w 0 "$KEYSTORE_FILE")
    echo "$BASE64_STRING" > "$BASE64_FILE"

    echo -e "\n${CYAN}=========================================${NC}"
    echo -e "${GREEN} SUCCESS! COPY THE BASE64 TEXT BELOW:${NC}"
    echo -e "${CYAN}=========================================${NC}"
    echo -e "${WHITE}$BASE64_STRING${NC}\n"
    echo -e "${YELLOW}[INFO] Please store '$KEYSTORE_FILE' in a safe location.${NC}"
    echo -e "${GREEN}[INFO] The Base64 string has also been saved to '$BASE64_FILE'.${NC}"
else
    echo -e "${RED}Failed to generate the Keystore. Ensure that Java (JDK) is correctly configured.${NC}"
    exit 1
fi
