#!/bin/bash
# MeshCore Bot Service Installation Script
# This script installs the MeshCore Bot as a system service
# Supports both Linux (systemd) and macOS (launchd)
#
# This script will:
#   1. Create a dedicated system user for the bot (Linux only)
#   2. Copy bot files to installation directory
#   3. Set up proper file permissions
#   4. Install and enable the service (systemd or launchd)
#   5. Create a Python virtual environment with dependencies
#
# Prerequisites:
#   - Linux system with systemd OR macOS
#   - Python 3.7+ installed
#   - sudo access (script will prompt if needed)
#   - Run from the meshcore-bot directory

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Detect operating system
OS="$(uname -s)"
IS_MACOS=false
IS_LINUX=false

if [[ "$OS" == "Darwin" ]]; then
    IS_MACOS=true
    SERVICE_TYPE="launchd"
elif [[ "$OS" == "Linux" ]]; then
    IS_LINUX=true
    SERVICE_TYPE="systemd"
else
    echo "Error: Unsupported operating system: $OS"
    echo "This script supports Linux (systemd) and macOS (launchd)"
    exit 1
fi

# Configuration - OS-specific paths
SERVICE_NAME="meshcore-bot"
PLIST_NAME="com.meshcore.bot"

if [[ "$IS_MACOS" == true ]]; then
    SERVICE_USER="$(whoami)"  # macOS: use current user or _meshcore
    SERVICE_GROUP="staff"
    INSTALL_DIR="/usr/local/meshcore-bot"
    LOG_DIR="/usr/local/var/log/meshcore-bot"
    SERVICE_FILE="com.meshcore.bot.plist"
    LAUNCHD_DIR="/Library/LaunchDaemons"
else
    SERVICE_USER="meshcore"
    SERVICE_GROUP="meshcore"
    INSTALL_DIR="/opt/meshcore-bot"
    LOG_DIR="/var/log/meshcore-bot"
    SERVICE_FILE="meshcore-bot.service"
    SYSTEMD_DIR="/etc/systemd/system"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Function to print section headers
print_section() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

# Function to print info messages
print_info() {
    echo -e "${CYAN}ℹ${NC}  $1"
}

# Function to print success messages
print_success() {
    echo -e "${GREEN}✓${NC}  $1"
}

# Function to print warning messages
print_warning() {
    echo -e "${YELLOW}⚠${NC}  $1"
}

# Function to print error messages
print_error() {
    echo -e "${RED}✗${NC}  $1"
}

print_section "MeshCore Bot Service Installer"
echo ""
if [[ "$IS_MACOS" == true ]]; then
    print_info "Detected macOS - will install as launchd service"
    print_info "The bot will start automatically on boot using launchd"
else
    print_info "Detected Linux - will install as systemd service"
    print_info "The bot will run as a dedicated user and start automatically on boot"
fi
echo ""

# Check if script has execute permissions
if [ ! -x "$0" ]; then
    print_warning "Script does not have execute permissions. Attempting to set them..."
    chmod +x "$0" 2>/dev/null || {
        print_error "Could not set execute permissions. Please run: chmod +x install-service.sh"
        exit 1
    }
    print_success "Execute permissions set"
fi

# Capture original user before sudo (for macOS)
ORIGINAL_USER="${SUDO_USER:-$USER}"

# Check if running as root, if not re-execute with sudo
if [[ $EUID -ne 0 ]]; then
    print_warning "This script requires root privileges to install system services"
    print_info "Re-executing with sudo..."
    echo ""
    exec sudo "$0" "$@"
fi

# Verify we're in the right directory
if [ ! -f "meshcore_bot.py" ]; then
    print_error "This script must be run from the meshcore-bot directory"
    print_error "Expected file not found: meshcore_bot.py"
    print_info "Please cd to the meshcore-bot directory and run this script again"
    exit 1
fi

# Check for service file
if [ ! -f "$SERVICE_FILE" ]; then
    print_error "Service file not found: $SERVICE_FILE"
    if [[ "$IS_MACOS" == true ]]; then
        print_info "Expected: com.meshcore.bot.plist"
    else
        print_info "Expected: meshcore-bot.service"
    fi
    exit 1
fi

# OS-specific service manager checks
if [[ "$IS_MACOS" == true ]]; then
    if ! command -v launchctl &> /dev/null; then
        print_error "launchctl is not available on this system"
        print_error "This script requires macOS with launchd"
        exit 1
    fi
else
    if ! command -v systemctl &> /dev/null; then
        print_error "systemd is not available on this system"
        print_error "This script requires a Linux system with systemd"
        exit 1
    fi
fi

# Check if Python 3 is available
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 is not installed or not in PATH"
    print_error "Please install Python 3.7 or higher before running this script"
    exit 1
fi

print_section "Step 1: Setting Up Service User"
if [[ "$IS_MACOS" == true ]]; then
    # Use original user if available, otherwise root
    if [[ -n "$ORIGINAL_USER" && "$ORIGINAL_USER" != "root" ]]; then
        SERVICE_USER="$ORIGINAL_USER"
        SERVICE_GROUP="$(id -gn "$ORIGINAL_USER" 2>/dev/null || echo "staff")"
    else
        SERVICE_USER="root"
        SERVICE_GROUP="wheel"
    fi
    print_info "macOS: Service will run as user '$SERVICE_USER'"
    print_info "On macOS, launchd services run as the specified user"
    print_success "Using user: $SERVICE_USER"
else
    print_info "Creating a dedicated system user '$SERVICE_USER' for security"
    print_info "This user will run the bot service with minimal privileges"
    # Create service user and group
    if ! id "$SERVICE_USER" &>/dev/null; then
        useradd --system --no-create-home --shell /bin/false "$SERVICE_USER"
        print_success "Created system user: $SERVICE_USER"
    else
        print_warning "User $SERVICE_USER already exists (skipping creation)"
    fi
    
    # Add user to dialout group for serial port access (Linux)
    print_info "Configuring serial port access permissions"
    if getent group dialout > /dev/null 2>&1; then
        if groups "$SERVICE_USER" | grep -q "\bdialout\b"; then
            print_warning "User $SERVICE_USER is already in dialout group"
        else
            usermod -a -G dialout "$SERVICE_USER"
            print_success "Added $SERVICE_USER to dialout group for serial port access"
        fi
    else
        print_warning "dialout group not found - serial port access may require manual configuration"
        print_info "If using serial connection, you may need to: sudo usermod -a -G dialout $SERVICE_USER"
    fi
    
    # Also check for other common serial port groups (tty, uucp, lock)
    for group in tty uucp lock; do
        if getent group "$group" > /dev/null 2>&1; then
            if ! groups "$SERVICE_USER" | grep -q "\b$group\b"; then
                usermod -a -G "$group" "$SERVICE_USER" 2>/dev/null && print_info "Added $SERVICE_USER to $group group" || true
            fi
        fi
    done
fi

print_section "Step 2: Creating Installation Directories"
print_info "Creating directory structure for bot installation"
# Create installation directory
if [ -d "$INSTALL_DIR" ]; then
    print_warning "Installation directory $INSTALL_DIR already exists"
    print_info "Existing files will be backed up and replaced"
    BACKUP_DIR="${INSTALL_DIR}.backup.$(date +%Y%m%d_%H%M%S)"
    mv "$INSTALL_DIR" "$BACKUP_DIR" 2>/dev/null || {
        print_error "Could not backup existing installation. Please remove $INSTALL_DIR manually"
        exit 1
    }
    print_success "Backed up existing installation to $BACKUP_DIR"
fi
mkdir -p "$INSTALL_DIR"
print_success "Created installation directory: $INSTALL_DIR"

# Create log directory
mkdir -p "$LOG_DIR"
print_success "Created log directory: $LOG_DIR"

print_section "Step 3: Copying Bot Files"
print_info "Copying all bot files to $INSTALL_DIR"
print_info "This includes Python scripts, modules, configuration templates, and documentation"
# Copy bot files to installation directory
cp -r . "$INSTALL_DIR/" 2>/dev/null || {
    print_error "Failed to copy files. Check permissions and disk space"
    exit 1
}
print_success "Copied bot files to $INSTALL_DIR"

print_section "Step 4: Setting File Permissions"
print_info "Configuring file ownership and permissions for security"
print_info "The service user will own all files, with appropriate read/write permissions"
# Set ownership
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$LOG_DIR"
print_success "Set ownership to $SERVICE_USER:$SERVICE_GROUP"

# Set permissions
chmod 755 "$INSTALL_DIR"
find "$INSTALL_DIR" -type f -name "*.py" -exec chmod 644 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type f -name "*.ini" -exec chmod 644 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type f -name "*.txt" -exec chmod 644 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type f -name "*.json" -exec chmod 644 {} \; 2>/dev/null || true
find "$INSTALL_DIR" -type d -exec chmod 755 {} \; 2>/dev/null || true

# Make main script executable
chmod 755 "$INSTALL_DIR/meshcore_bot.py"
print_success "Configured file permissions"

print_section "Step 5: Installing Service"
if [[ "$IS_MACOS" == true ]]; then
    print_info "Installing launchd plist file to enable automatic startup"
    print_info "The service will be configured to start on boot and restart on failure"
    # Create LaunchDaemons directory if it doesn't exist
    mkdir -p "$LAUNCHD_DIR"
    
    # Update plist with actual installation paths and copy to LaunchDaemons
    print_info "Updating plist file with installation paths"
    # Use a more portable approach for path substitution
    if command -v python3 &> /dev/null; then
        python3 -c "
import sys
import re
with open('$SERVICE_FILE', 'r') as f:
    content = f.read()
content = content.replace('/usr/local/meshcore-bot', '$INSTALL_DIR')
content = content.replace('/usr/local/var/log/meshcore-bot', '$LOG_DIR')
with open('$LAUNCHD_DIR/$SERVICE_FILE', 'w') as f:
    f.write(content)
"
    else
        # Fallback to sed (works on both macOS and Linux)
        sed "s|/usr/local/meshcore-bot|$INSTALL_DIR|g; s|/usr/local/var/log/meshcore-bot|$LOG_DIR|g" "$SERVICE_FILE" > "$LAUNCHD_DIR/$SERVICE_FILE"
    fi
    print_success "Copied and configured plist file to $LAUNCHD_DIR/"
    
    # Set ownership
    chown root:wheel "$LAUNCHD_DIR/$SERVICE_FILE"
    chmod 644 "$LAUNCHD_DIR/$SERVICE_FILE"
    print_success "Set plist permissions"
    
    print_section "Step 6: Loading Service"
    print_info "Loading service into launchd"
    # Unload if already loaded
    launchctl list "$PLIST_NAME" &>/dev/null && launchctl unload "$LAUNCHD_DIR/$SERVICE_FILE" 2>/dev/null || true
    # Load the service
    launchctl load "$LAUNCHD_DIR/$SERVICE_FILE" 2>/dev/null || {
        print_error "Failed to load service. Check plist syntax and permissions."
        exit 1
    }
    print_success "Service '$PLIST_NAME' loaded into launchd"
    print_info "Note: The service is loaded but not started yet. You'll start it after configuration."
else
    print_info "Installing systemd service file to enable automatic startup"
    print_info "The service will be configured to start on boot and restart on failure"
    # Copy service file to systemd directory
    cp "$SERVICE_FILE" "$SYSTEMD_DIR/"
    print_success "Copied service file to $SYSTEMD_DIR/"
    
    # Reload systemd
    print_info "Reloading systemd to recognize the new service"
    systemctl daemon-reload
    print_success "Systemd configuration reloaded"
    
    print_section "Step 6: Enabling Service"
    print_info "Enabling service to start automatically on system boot"
    # Enable service to start on boot
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
    print_success "Service '$SERVICE_NAME' enabled for automatic startup"
    print_info "Note: The service is enabled but not started yet. You'll start it after configuration."
fi

print_section "Step 7: Setting Up Python Virtual Environment"
print_info "Creating an isolated Python environment for the bot"
print_info "This ensures dependencies don't conflict with system Python packages"
# Create virtual environment
if [ -d "$INSTALL_DIR/venv" ]; then
    print_warning "Virtual environment already exists, removing it..."
    rm -rf "$INSTALL_DIR/venv"
fi
python3 -m venv "$INSTALL_DIR/venv"
print_success "Created virtual environment at $INSTALL_DIR/venv"

# Upgrade pip first
print_info "Upgrading pip to latest version"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true

# Install dependencies in venv
print_info "Installing Python dependencies from requirements.txt"
print_info "This may take a few minutes depending on your internet connection..."
if [ ! -f "$INSTALL_DIR/requirements.txt" ]; then
    print_error "requirements.txt not found in installation directory"
    exit 1
fi
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt" || {
    print_error "Failed to install Python dependencies"
    print_info "You may need to check your internet connection or Python version"
    exit 1
}
print_success "Installed all Python dependencies"

# Update ownership of venv
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/venv"
print_success "Set ownership of virtual environment"

print_section "Installation Complete!"
echo ""
print_success "MeshCore Bot has been successfully installed as a systemd service!"
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📋 Next Steps${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${CYAN}1. Configure the bot:${NC}"
echo "   ${YELLOW}sudo nano $INSTALL_DIR/config.ini${NC}"
echo "   Edit the configuration file with your bot settings, API keys, and device information"
echo ""

if [[ "$IS_MACOS" == true ]]; then
    echo -e "${CYAN}2. Start the service:${NC}"
    echo "   ${YELLOW}sudo launchctl load -w $LAUNCHD_DIR/$SERVICE_FILE${NC}"
    echo "   Or: ${YELLOW}sudo launchctl start $PLIST_NAME${NC}"
    echo ""
    echo -e "${CYAN}3. Verify it's running:${NC}"
    echo "   ${YELLOW}sudo launchctl list | grep $PLIST_NAME${NC}"
    echo "   Or check logs: ${YELLOW}tail -f $LOG_DIR/meshcore-bot.log${NC}"
    echo ""
    echo -e "${CYAN}4. View live logs (optional):${NC}"
    echo "   ${YELLOW}tail -f $LOG_DIR/meshcore-bot.log${NC}"
    echo "   Press Ctrl+C to exit log view"
else
    echo -e "${CYAN}2. Start the service:${NC}"
    echo "   ${YELLOW}sudo systemctl start $SERVICE_NAME${NC}"
    echo ""
    echo -e "${CYAN}3. Verify it's running:${NC}"
    echo "   ${YELLOW}sudo systemctl status $SERVICE_NAME${NC}"
    echo "   You should see 'active (running)' in green"
    echo ""
    echo -e "${CYAN}4. View live logs (optional):${NC}"
    echo "   ${YELLOW}sudo journalctl -u $SERVICE_NAME -f${NC}"
    echo "   Press Ctrl+C to exit log view"
fi
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}🔧 Service Management Commands${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

if [[ "$IS_MACOS" == true ]]; then
    echo -e "  ${CYAN}Start service:${NC}     ${YELLOW}sudo launchctl start $PLIST_NAME${NC}"
    echo -e "  ${CYAN}Stop service:${NC}       ${YELLOW}sudo launchctl stop $PLIST_NAME${NC}"
    echo -e "  ${CYAN}Restart service:${NC}   ${YELLOW}sudo launchctl stop $PLIST_NAME && sudo launchctl start $PLIST_NAME${NC}"
    echo -e "  ${CYAN}Check status:${NC}      ${YELLOW}sudo launchctl list | grep $PLIST_NAME${NC}"
    echo -e "  ${CYAN}View logs:${NC}         ${YELLOW}tail -f $LOG_DIR/meshcore-bot.log${NC}"
    echo -e "  ${CYAN}View error logs:${NC}   ${YELLOW}tail -f $LOG_DIR/meshcore-bot.error.log${NC}"
    echo -e "  ${CYAN}Unload service:${NC}    ${YELLOW}sudo launchctl unload $LAUNCHD_DIR/$SERVICE_FILE${NC}"
    echo -e "  ${CYAN}Load service:${NC}       ${YELLOW}sudo launchctl load $LAUNCHD_DIR/$SERVICE_FILE${NC}"
else
    echo -e "  ${CYAN}Start service:${NC}     ${YELLOW}sudo systemctl start $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}Stop service:${NC}      ${YELLOW}sudo systemctl stop $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}Restart service:${NC}   ${YELLOW}sudo systemctl restart $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}Check status:${NC}      ${YELLOW}sudo systemctl status $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}View logs:${NC}         ${YELLOW}sudo journalctl -u $SERVICE_NAME -f${NC}"
    echo -e "  ${CYAN}View recent logs:${NC}  ${YELLOW}sudo journalctl -u $SERVICE_NAME -n 100${NC}"
    echo -e "  ${CYAN}Disable auto-start:${NC} ${YELLOW}sudo systemctl disable $SERVICE_NAME${NC}"
    echo -e "  ${CYAN}Enable auto-start:${NC}  ${YELLOW}sudo systemctl enable $SERVICE_NAME${NC}"
fi
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}📁 Important File Locations${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  ${CYAN}Configuration file:${NC}  ${YELLOW}$INSTALL_DIR/config.ini${NC}"
echo -e "  ${CYAN}Log directory:${NC}        ${YELLOW}$LOG_DIR${NC}"
echo -e "  ${CYAN}Installation directory:${NC} ${YELLOW}$INSTALL_DIR${NC}"
if [[ "$IS_MACOS" == true ]]; then
    echo -e "  ${CYAN}Service plist:${NC}        ${YELLOW}$LAUNCHD_DIR/$SERVICE_FILE${NC}"
else
    echo -e "  ${CYAN}Service file:${NC}        ${YELLOW}$SYSTEMD_DIR/$SERVICE_NAME.service${NC}"
fi
echo ""

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}ℹ️  Additional Information${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
print_info "The service is configured to:"
echo "  • Start automatically on system boot"
if [[ "$IS_MACOS" == true ]]; then
    echo "  • Restart automatically if it crashes (with 10 second throttle)"
    echo "  • Run as user '$SERVICE_USER'"
    echo "  • Log to: $LOG_DIR/meshcore-bot.log"
    echo ""
    print_info "After editing config.ini, restart the service for changes to take effect:"
    echo "  ${YELLOW}sudo launchctl stop $PLIST_NAME && sudo launchctl start $PLIST_NAME${NC}"
else
    echo "  • Restart automatically if it crashes (with 10 second delay)"
    echo "  • Run as user '$SERVICE_USER' for security"
    echo "  • Log to systemd journal (view with journalctl)"
    echo ""
    print_info "Serial port access:"
    echo "  • User '$SERVICE_USER' has been added to dialout group for serial port access"
    echo "  • If using serial connection, ensure the service is restarted after installation"
    echo "  • Group membership changes take effect after service restart"
    echo ""
    print_info "After editing config.ini, restart the service for changes to take effect:"
    echo "  ${YELLOW}sudo systemctl restart $SERVICE_NAME${NC}"
fi
echo ""
print_success "Installation complete! The bot is ready to configure and start."
echo ""