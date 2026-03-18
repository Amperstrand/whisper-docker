#!/usr/bin/env bash
#
# setup.sh — Install host prerequisites for whisper-docker.
#
# Installs Docker Engine, Docker Compose plugin, and NVIDIA Container Toolkit
# from their official apt repositories. Validates GPU passthrough at the end.
#
# Targets: Ubuntu 22.04 and 24.04 (amd64).
# Idempotent — safe to run multiple times.
#
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[SKIP]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    fail "This script must be run with sudo:  sudo ./setup.sh"
fi

SUDO_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "")}"

# ---------------------------------------------------------------------------
# 1. Validate NVIDIA GPU
# ---------------------------------------------------------------------------
echo ""
echo "=== Checking NVIDIA GPU ==="
if ! command -v nvidia-smi &>/dev/null; then
    fail "nvidia-smi not found. Install the NVIDIA GPU driver first."
fi

GPU_INFO=$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | head -1)
if [ -z "$GPU_INFO" ]; then
    fail "nvidia-smi reports no GPU. Check your driver installation."
fi
info "GPU detected: $GPU_INFO"

# ---------------------------------------------------------------------------
# 2. Install Docker Engine + Compose plugin
# ---------------------------------------------------------------------------
echo ""
echo "=== Installing Docker ==="
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    warn "Docker $(docker --version | grep -oP '\d+\.\d+\.\d+') and Compose $(docker compose version --short) already installed."
else
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg apt-transport-https >/dev/null

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME")
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $CODENAME stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
    systemctl enable --now docker >/dev/null
    info "Docker $(docker --version | grep -oP '\d+\.\d+\.\d+') and Compose $(docker compose version --short) installed."
fi

# ---------------------------------------------------------------------------
# 3. Add user to docker group
# ---------------------------------------------------------------------------
if [ -n "$SUDO_USER" ] && ! id "$SUDO_USER" | grep -q '\bdocker\b'; then
    usermod -aG docker "$SUDO_USER"
    info "Added user '$SUDO_USER' to the docker group."
else
    warn "User '$SUDO_USER' already in docker group (or SUDO_USER not set)."
fi

# ---------------------------------------------------------------------------
# 4. Install NVIDIA Container Toolkit
# ---------------------------------------------------------------------------
echo ""
echo "=== Installing NVIDIA Container Toolkit ==="
if command -v nvidia-ctk &>/dev/null; then
    warn "NVIDIA Container Toolkit $(nvidia-ctk --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1) already installed."
else
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list

    apt-get update -qq
    apt-get install -y -qq nvidia-container-toolkit >/dev/null
    info "NVIDIA Container Toolkit $(nvidia-ctk --version 2>/dev/null | grep -oP '\d+\.\d+\.\d+' | head -1) installed."
fi

nvidia-ctk runtime configure --runtime=docker >/dev/null 2>&1
systemctl restart docker
info "Docker configured with NVIDIA runtime and restarted."

# ---------------------------------------------------------------------------
# 5. Validate GPU passthrough
# ---------------------------------------------------------------------------
echo ""
echo "=== Validating GPU passthrough ==="
if docker run --rm --gpus all ubuntu nvidia-smi >/dev/null 2>&1; then
    info "GPU passthrough works inside Docker containers."
else
    fail "GPU passthrough test failed. Check NVIDIA driver and Container Toolkit versions."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "  Host setup complete."
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Place an audio file in input/"
echo "  2. Run:  docker compose up --build"
echo ""
if [ -n "$SUDO_USER" ]; then
    echo "NOTE: You may need to log out and back in (or run 'newgrp docker')"
    echo "      for the docker group membership to take effect."
    echo ""
fi
