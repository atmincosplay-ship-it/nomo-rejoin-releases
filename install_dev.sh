#!/data/data/com.termux/files/usr/bin/sh
set -eu
stty onlcr 2>/dev/null || true

BASE_DIR="/storage/emulated/0/Download/nomo_rejoin_dev"
APP="$BASE_DIR/nomo_rejoin_dev.py"
PREFIX_DIR="${PREFIX:-/data/data/com.termux/files/usr}"
BIN_DIR="$PREFIX_DIR/bin"
CMD="$BIN_DIR/nomo-dev"
RAW_BASE="https://api.github.com/repos/atmincosplay-ship-it/nomo-rejoin-releases/contents/nomo_rejoin_dev.py?ref=main"

mkdir -p "$BASE_DIR" "$BIN_DIR"

download_dev() {
  RAW_URL="$RAW_BASE&nocache=$(date +%s)"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --silent --show-error \
      -H "Accept: application/vnd.github.raw" \
      -H "User-Agent: nomo-dev-updater" \
      -H "Cache-Control: no-cache" \
      "$RAW_URL" -o "$APP"
  elif command -v wget >/dev/null 2>&1; then
    wget -q \
      --header="Accept: application/vnd.github.raw" \
      --header="User-Agent: nomo-dev-updater" \
      --header="Cache-Control: no-cache" \
      -O "$APP" "$RAW_URL"
  else
    echo "Missing curl/wget. Install curl first: pkg install -y curl"
    exit 1
  fi
  chmod 755 "$APP"
  grep -m1 -E '^(__version__|VERSION)' "$APP" 2>/dev/null || true
}

download_dev

cat > "$CMD" <<'EOF'
#!/data/data/com.termux/files/usr/bin/sh
set -eu
stty onlcr 2>/dev/null || true

BASE_DIR="/storage/emulated/0/Download/nomo_rejoin_dev"
APP="$BASE_DIR/nomo_rejoin_dev.py"
RAW_BASE="https://api.github.com/repos/atmincosplay-ship-it/nomo-rejoin-releases/contents/nomo_rejoin_dev.py?ref=main"

if [ "${1:-}" = "update" ]; then
  mkdir -p "$BASE_DIR"
  RAW_URL="$RAW_BASE&nocache=$(date +%s)"
  if command -v curl >/dev/null 2>&1; then
    curl -L --fail --silent --show-error \
      -H "Accept: application/vnd.github.raw" \
      -H "User-Agent: nomo-dev-updater" \
      -H "Cache-Control: no-cache" \
      "$RAW_URL" -o "$APP"
  elif command -v wget >/dev/null 2>&1; then
    wget -q \
      --header="Accept: application/vnd.github.raw" \
      --header="User-Agent: nomo-dev-updater" \
      --header="Cache-Control: no-cache" \
      -O "$APP" "$RAW_URL"
  else
    echo "Missing curl/wget. Install curl first: pkg install -y curl"
    exit 1
  fi
  chmod 755 "$APP"
  echo "NOMO dev updated: $APP"
  grep -m1 -E '^(__version__|VERSION)' "$APP" 2>/dev/null || true
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
echo "  nomo-dev version"
echo "  nomo-dev        # open source-style menu"
echo "  Choose option 1 to start the rejoin loop"
