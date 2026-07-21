#!/data/data/com.termux/files/usr/bin/sh
set -eu

BASE_DIR="/storage/emulated/0/Download/nomo_rejoin_dev"
APP="$BASE_DIR/nomo_rejoin_dev.py"
PREFIX_DIR="${PREFIX:-/data/data/com.termux/files/usr}"
BIN_DIR="$PREFIX_DIR/bin"
CMD="$BIN_DIR/nomo-dev"
RAW_BASE="https://raw.githubusercontent.com/atmincosplay-ship-it/nomo-rejoin-releases/main/nomo_rejoin_dev.py"

mkdir -p "$BASE_DIR" "$BIN_DIR"

download_dev() {
  RAW_URL="$RAW_BASE?nocache=$(date +%s)"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --silent --show-error -H "Cache-Control: no-cache" "$RAW_URL" -o "$APP"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$APP" "$RAW_URL"
  else
    echo "Missing curl/wget. Install curl first: pkg install -y curl"
    exit 1
  fi
  chmod 755 "$APP"
  grep -m1 '^VERSION' "$APP" 2>/dev/null || true
}

download_dev

cat > "$CMD" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
set -eu

BASE_DIR="/storage/emulated/0/Download/nomo_rejoin_dev"
APP="$BASE_DIR/nomo_rejoin_dev.py"
RAW_BASE="https://raw.githubusercontent.com/atmincosplay-ship-it/nomo-rejoin-releases/main/nomo_rejoin_dev.py"

if [ "${1:-}" = "update" ]; then
  mkdir -p "$BASE_DIR"
  RAW_URL="$RAW_BASE?nocache=$(date +%s)"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --silent --show-error -H "Cache-Control: no-cache" "$RAW_URL" -o "$APP"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$APP" "$RAW_URL"
  else
    echo "Missing curl/wget. Install curl first: pkg install -y curl"
    exit 1
  fi
  chmod 755 "$APP"
  echo "NOMO dev updated: $APP"
  grep -m1 '^VERSION' "$APP" 2>/dev/null || true
  exit 0
fi

if [ ! -f "$APP" ]; then
  echo "NOMO dev missing. Run: nomo-dev update"
  exit 1
fi

PYTHON="${PREFIX:-/data/data/com.termux/files/usr}/bin/python"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python)"
fi

exec "$PYTHON" "$APP" "$@"
EOF

chmod 755 "$CMD"

echo "NOMO dev installed."
echo "Command: nomo-dev"
echo ""
echo "First tests:"
echo "  nomo-dev doctor"
echo "  nomo-dev init"
echo "  nomo-dev list"
echo "  nomo-dev        # open menu"
