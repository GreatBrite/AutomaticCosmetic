#!/usr/bin/env bash
set -euo pipefail

version="${PROTON_BRIDGE_VERSION:-3.13.0-1}"
deb="protonmail-bridge_${version}_amd64.deb"
url="https://proton.me/download/bridge/${deb}"
tmp="/tmp/${deb}"

echo "Downloading Proton Mail Bridge ${version}"
wget -O "$tmp" "$url"

echo "Installing dependencies"
apt-get update
apt-get install -y pass gnome-keyring libsecret-1-0

echo "Installing Proton Mail Bridge"
apt-get install -y "$tmp"

echo
echo "Next step:"
echo "  protonmail-bridge -c"
echo
echo "Inside Bridge CLI, log in to Proton and run:"
echo "  login"
echo "  info"
echo
echo "Use the IMAP/SMTP host, ports, username, and generated mailbox password in mcp/email.mcp.json."

