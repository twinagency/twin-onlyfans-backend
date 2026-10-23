#!/usr/bin/env bash
set -euo pipefail

if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
  exit 0
fi

# Ubuntu supplies ffmpeg. The disposable runner's unrelated Chrome feed can
# stall apt or publish inconsistent index hashes; it is not needed here.
# Keep all package signature/hash verification intact.
sudo mkdir -p /tmp/ci-unused-chrome-feed
while IFS= read -r source; do
  sudo mv "$source" "/tmp/ci-unused-chrome-feed/${source##*/}"
done < <(sudo grep -rl 'dl.google.com/linux/chrome' /etc/apt/sources.list.d 2>/dev/null || true)

# Use the same primary HTTPS mirror recovery as browser CI and deployment.
# The regional Azure mirror can remain connected while downloading too slowly
# to complete the bounded encoder setup on an otherwise healthy runner.
for mirror_list in /etc/apt/apt-mirrors.txt /etc/apt/apt-mirrors-security.txt; do
  if [ -f "$mirror_list" ]; then
    sudo sed -i 's|http://azure.archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g; s|https://azure.archive.ubuntu.com/ubuntu|https://archive.ubuntu.com/ubuntu|g' "$mirror_list"
  fi
done

apt_options=(
  -o Acquire::Retries=1
  -o Acquire::http::Timeout=20
  -o Acquire::https::Timeout=20
  -o DPkg::Lock::Timeout=30
)

# Bound setup even if a package mirror or subprocess becomes unresponsive.
sudo timeout --kill-after=10s 150s apt-get "${apt_options[@]}" update
sudo timeout --kill-after=10s 210s apt-get "${apt_options[@]}" install -y --no-install-recommends ffmpeg
command -v ffmpeg
command -v ffprobe
