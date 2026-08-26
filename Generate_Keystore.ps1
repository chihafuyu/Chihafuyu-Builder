<#
.SYNOPSIS
    Generates a Java Keystore (.keystore) file and outputs its Base64 string representation.

.DESCRIPTION
    This script prompts the user interactively for an alias, password, and individual
    Distinguished Name (DNAME) components. It utilizes the Java 'keytool' utility to
    generate a new high-security Keystore (RSA 4096-bit, 100 years validity), and then
    converts the generated file into a Base64 encoded string for use in CI/CD pipelines
    (e.g., GitHub Actions Secrets). The Base64 string is displayed on the console and
    saved to a text file.

.NOTES
    Requires Java (JDK) to be installed and 'keytool' to be available in the system PATH.
#>

[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification='Interactive script requires console formatting')]
[CmdletBinding()]
param()

# Clear the console for better readability
Clear-Host

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "       CUSTOM KEYSTORE GENERATOR         " -ForegroundColor Yellow
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""

# Verify if the Java keytool utility is available in the system environment
if (-not (Get-Command "keytool" -ErrorAction SilentlyContinue)) {
    Write-Error "The 'keytool' command was not found. Please ensure Java (JDK) is installed and added to your system PATH."
    exit
}

$aliasName = Read-Host "1. Enter the Keystore alias (e.g., myalias) [Default: myalias]"
if ([string]::IsNullOrWhiteSpace($aliasName)) {
    $aliasName = "myalias"
}

$password = Read-Host "2. Enter the Keystore password (minimum 6 characters)"
if ($password.Length -lt 6) {
    Write-Error "The password must be at least 6 characters long."
    exit
}

Write-Host "`n3. Enter Distinguished Name (DNAME) Information:" -ForegroundColor White

$cn = Read-Host "   a. First and Last Name (CN) [Default: Android Debug]"
if ([string]::IsNullOrWhiteSpace($cn)) { $cn = "Android Debug" }

$ou = Read-Host "   b. Organizational Unit (OU) [Default: Patcher]"
if ([string]::IsNullOrWhiteSpace($ou)) { $ou = "Patcher" }

$o = Read-Host "   c. Organization Name (O) [Default: Android]"
if ([string]::IsNullOrWhiteSpace($o)) { $o = "Android" }

$l = Read-Host "   d. City or Locality (L) [Default: Unknown]"
if ([string]::IsNullOrWhiteSpace($l)) { $l = "Unknown" }

$s = Read-Host "   e. State or Province (S) [Default: Unknown]"
if ([string]::IsNullOrWhiteSpace($s)) { $s = "Unknown" }

$c = Read-Host "   f. Two-Letter Country Code (C) (e.g., US) [Default: US]"
if ([string]::IsNullOrWhiteSpace($c)) { $c = "US" }

# Assemble the final DNAME string
$dname = "CN=$cn, OU=$ou, O=$o, L=$l, S=$s, C=$c"

$keystoreFile = "custom_keystore.keystore"
$base64File = "custom_keystore_base64.txt"

# Remove the target files if they already exist to prevent keytool prompt hangs
if (Test-Path $keystoreFile) {
    Remove-Item $keystoreFile -Force
}
if (Test-Path $base64File) {
    Remove-Item $base64File -Force
}

Write-Host "`n[INFO] Generating the new Keystore file..." -ForegroundColor Yellow

# Execute keytool using an array for cleaner argument passing
try {
    $keytoolArgs = @(
        "-genkey", "-v",
        "-keystore", $keystoreFile,
        "-alias", $aliasName,
        "-keyalg", "RSA",
        "-keysize", "4096",
        "-validity", "36500",
        "-storepass", $password,
        "-keypass", $password,
        "-dname", $dname
    )

    & keytool $keytoolArgs | Out-Null
}
catch {
    Write-Error "An error occurred while executing keytool: $_"
    exit
}

if (Test-Path $keystoreFile) {
    Write-Host "[INFO] Converting the Keystore to a Base64 string..." -ForegroundColor Yellow

    try {
        $fileBytes = [System.IO.File]::ReadAllBytes((Resolve-Path $keystoreFile).Path)
        $base64String = [System.Convert]::ToBase64String($fileBytes)

        # Save the Base64 string to a text file securely
        $base64FilePath = Join-Path (Get-Location) $base64File
        [System.IO.File]::WriteAllText($base64FilePath, $base64String)

        Write-Host "`n=========================================" -ForegroundColor Cyan
        Write-Host " SUCCESS! COPY THE BASE64 TEXT BELOW:" -ForegroundColor Green
        Write-Host "=========================================" -ForegroundColor Cyan
        Write-Host $base64String -ForegroundColor White
        Write-Host "`n[INFO] Please store '$keystoreFile' in a safe location." -ForegroundColor Yellow
        Write-Host "[INFO] The Base64 string has also been saved to '$base64File'." -ForegroundColor Green
    }
    catch {
        Write-Error "Failed to read, convert, or save the Keystore file: $_"
    }
}
else {
    Write-Error "Failed to generate the Keystore. Ensure that Java (JDK) is correctly configured."
}

Write-Host "`nPress Enter to exit..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
