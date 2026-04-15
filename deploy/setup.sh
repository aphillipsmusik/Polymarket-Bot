#!/usr/bin/env bash
# setup.sh — Manual setup script for the Polymarket bot on a fresh VPS.
#
# Tested on: Amazon Linux 2023, Ubuntu 22.04, Debian 12
#
# Usage (run as root or with sudo):
#   sudo bash setup.sh
#
# What this does:
#   1. Installs Python 3.11 + pip + virtualenv
#   2. Creates /opt/polymarket working directory
#   3. Clones the repo (or syncs if run again)
#   4. Installs Python dependencies in a virtualenv
#   5. Creates a skeleton .env file (you fill in the private key)
#   6. Installs the systemd service and enables it
#   7. Hardens SSH (custom port, no root login, no password auth)
#   8. Sets up log rotation
#
# After running:
#   nano /opt/polymarket/.env          # add POLYMARKET_PRIVATE_KEY
#   python simulate.py --scans 30      # test with paper trading first
#   sudo systemctl start polymarket    # start live bot

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────

REPO_URL="https://github.com/aphillipsmusik/primary.git"
INSTALL_DIR="/opt/polymarket"
VENV_DIR="${INSTALL_DIR}/venv"
LOG_DIR="${INSTALL_DIR}/logs"
SERVICE_USER="polymarket"
SSH_PORT="${SSH_PORT:-2222}"   # override with: SSH_PORT=2200 sudo bash setup.sh

# ── Detect OS ─────────────────────────────────────────────────────────────────

if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS_ID="$ID"
else
    OS_ID="unknown"
fi

print_step() { echo; echo "══════════════════════════════════════════════════"; echo "  $1"; echo "══════════════════════════════════════════════════"; }

# ── Step 1: System packages ───────────────────────────────────────────────────

print_step "Installing system packages"

case "$OS_ID" in
    amzn|fedora|rhel|centos)
        dnf update -y
        dnf install -y \
            git \
            python3.11 python3.11-pip python3.11-devel \
            gcc gcc-c++ make openssl-devel \
            jq curl wget
        ;;
    ubuntu|debian)
        apt-get update -y
        apt-get install -y \
            git \
            python3.11 python3.11-venv python3.11-dev \
            build-essential libssl-dev \
            jq curl wget
        ;;
    *)
        echo "Unsupported OS: $OS_ID — attempting generic install"
        which python3.11 || { echo "ERROR: python3.11 not found. Install manually."; exit 1; }
        ;;
esac

echo "System packages installed."

# ── Step 2: Service user ──────────────────────────────────────────────────────

print_step "Creating service user '${SERVICE_USER}'"

if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --no-create-home --shell /sbin/nologin "${SERVICE_USER}"
    echo "Created user: ${SERVICE_USER}"
else
    echo "User ${SERVICE_USER} already exists — skipping."
fi

# ── Step 3: Install directory ─────────────────────────────────────────────────

print_step "Setting up ${INSTALL_DIR}"

if [ -d "${INSTALL_DIR}/.git" ]; then
    echo "Repo exists — pulling latest..."
    git -C "${INSTALL_DIR}" pull --ff-only
else
    git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

mkdir -p "${LOG_DIR}"

# ── Step 4: Python virtualenv + dependencies ──────────────────────────────────

print_step "Creating virtualenv and installing Python dependencies"

python3.11 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip
"${VENV_DIR}/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

echo "Python dependencies installed."

# ── Step 5: Environment file ──────────────────────────────────────────────────

print_step "Creating .env (skeleton — you must fill in your private key)"

ENV_FILE="${INSTALL_DIR}/.env"
if [ ! -f "${ENV_FILE}" ]; then
    cat > "${ENV_FILE}" << 'EOF'
# Polymarket bot environment configuration
# ─────────────────────────────────────────
# 1. Generate a Polygon wallet private key (MetaMask, cast wallet new, etc.)
# 2. Fund the wallet with USDC on Polygon mainnet
# 3. Approve the Polymarket CLOB contract to spend USDC (done automatically
#    on first run by py_clob_client)
# 4. Set the key below and start the service

POLYMARKET_PRIVATE_KEY=0xyour-polygon-private-key-here

# Optional overrides (defaults are set in config.py)
# POLYMARKET_LOG_LEVEL=INFO
# POLYMARKET_CAPITAL=50
EOF
    chmod 600 "${ENV_FILE}"
    echo ".env created at ${ENV_FILE} — ADD YOUR PRIVATE KEY BEFORE GOING LIVE"
else
    echo ".env already exists — skipping (not overwriting your key)."
fi

# ── Step 6: Systemd service ───────────────────────────────────────────────────

print_step "Installing systemd service"

SERVICE_SRC="${INSTALL_DIR}/deploy/polymarket.service"
SERVICE_DST="/etc/systemd/system/polymarket.service"

if [ -f "${SERVICE_SRC}" ]; then
    # Patch paths into the service file
    sed \
        -e "s|/opt/polymarket|${INSTALL_DIR}|g" \
        -e "s|User=polymarket|User=${SERVICE_USER}|g" \
        "${SERVICE_SRC}" > "${SERVICE_DST}"

    systemctl daemon-reload
    systemctl enable polymarket
    echo "Service installed and enabled (not yet started)."
else
    echo "WARNING: ${SERVICE_SRC} not found — service not installed."
fi

# ── Step 7: Log rotation ──────────────────────────────────────────────────────

print_step "Configuring log rotation"

cat > /etc/logrotate.d/polymarket << EOF
${LOG_DIR}/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 640 ${SERVICE_USER} ${SERVICE_USER}
    postrotate
        systemctl kill --signal=USR1 polymarket 2>/dev/null || true
    endscript
}
EOF

echo "Log rotation configured (14 days)."

# ── Step 8: Permissions ───────────────────────────────────────────────────────

print_step "Setting file permissions"

chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
chmod 750 "${INSTALL_DIR}"
chmod 700 "${LOG_DIR}"
chmod 600 "${ENV_FILE}"

# Allow the ec2-user (or ubuntu) to also read logs for debugging
if id ec2-user &>/dev/null; then
    usermod -aG "${SERVICE_USER}" ec2-user 2>/dev/null || true
elif id ubuntu &>/dev/null; then
    usermod -aG "${SERVICE_USER}" ubuntu 2>/dev/null || true
fi

# ── Step 9: SSH hardening ─────────────────────────────────────────────────────

print_step "Hardening SSH (port=${SSH_PORT}, no root login, no password auth)"

SSHD_CONFIG="/etc/ssh/sshd_config"
cp "${SSHD_CONFIG}" "${SSHD_CONFIG}.bak.$(date +%Y%m%d%H%M%S)"

# Apply hardening
sed -i \
    -e "s/^#*Port .*/Port ${SSH_PORT}/" \
    -e "s/^#*PermitRootLogin .*/PermitRootLogin no/" \
    -e "s/^#*PasswordAuthentication .*/PasswordAuthentication no/" \
    -e "s/^#*PubkeyAuthentication .*/PubkeyAuthentication yes/" \
    "${SSHD_CONFIG}"

# Validate config before restarting
if sshd -t 2>/dev/null; then
    systemctl restart sshd
    echo "SSH restarted on port ${SSH_PORT}."
    echo "IMPORTANT: Open port ${SSH_PORT} in your firewall/security group!"
else
    echo "WARNING: sshd config validation failed — reverting SSH changes."
    cp "${SSHD_CONFIG}.bak."* "${SSHD_CONFIG}" 2>/dev/null || true
fi

# ── Done ──────────────────────────────────────────────────────────────────────

print_step "Setup complete"

PYTHON="${VENV_DIR}/bin/python"

echo ""
echo "  Next steps:"
echo ""
echo "  1. Add your Polygon private key:"
echo "       nano ${ENV_FILE}"
echo ""
echo "  2. Test with paper trading (no key needed):"
echo "       cd ${INSTALL_DIR}"
echo "       source venv/bin/activate"
echo "       python simulate.py --scans 30"
echo ""
echo "  3. When ready for live trading:"
echo "       sudo systemctl start polymarket"
echo "       journalctl -u polymarket -f"
echo ""
echo "  4. SSH connection (new port):"
echo "       ssh -p ${SSH_PORT} ec2-user@<your-ip>"
echo ""
echo "  Logs: ${LOG_DIR}/bot.log"
echo ""
