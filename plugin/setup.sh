#!/bin/sh
# macOS twin of setup.cmd: one-time setup for the SAP connector (called by launch.sh;
# output goes to setup.log).
# 1. Find Python 3.10+ with Tk (needed for the login window); never a uv-managed one
#    (uv renames its folders on update) and never /usr/bin/python3 (Apple's 3.9, and
#    calling it may pop up the "install developer tools" dialog).
#    If there is none, download a private standalone Python 3.12 (python-build-standalone,
#    includes Tk) into the data folder: no admin rights, nothing installed system-wide.
# 2. Install the server's packages into <DATA>/lib-<VER> (private to this connector).
# Arguments from launch.sh: version, data folder, entry script.
VER="$1"
DATA="$2"
ENTRY="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
# One package folder per version: a running Claude may still hold the previous one open.
LIB="$DATA/lib-$VER"
CHECK='import sys, os, tkinter; e = sys.executable; print(e if sys.version_info >= (3, 10) and "uv" not in e.lower().split(os.sep) else "")'
echo "=== SAP connector setup $VER $(date) ==="

findpy() {
  PY=""
  for c in "$DATA/python/bin/python3" \
           /Library/Frameworks/Python.framework/Versions/3.1[0-9]/bin/python3 \
           /opt/homebrew/bin/python3.1[0-9] /opt/homebrew/bin/python3 \
           /usr/local/bin/python3.1[0-9] /usr/local/bin/python3; do
    [ -x "$c" ] || continue
    r="$("$c" -c "$CHECK" 2>/dev/null)"
    if [ -n "$r" ]; then PY="$r"; return 0; fi
  done
  return 1
}

download_python() {
  case "$(uname -m)" in
    arm64) ARCH=aarch64 ;;
    x86_64) ARCH=x86_64 ;;
    *) echo "unsupported Mac processor $(uname -m)"; return 1 ;;
  esac
  echo "Python 3.10+ with Tk not found - downloading a private Python 3.12 ($ARCH)"
  URL="$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest \
        | grep -o "https://[^\"]*/cpython-3\.12\.[0-9]*+[0-9]*-$ARCH-apple-darwin-install_only\.tar\.gz" \
        | head -n 1)"
  if [ -z "$URL" ]; then echo "could not find a Python download"; return 1; fi
  echo "download: $URL"
  rm -rf "$DATA/python" "$DATA/python.tar.gz"
  curl -fsSL -o "$DATA/python.tar.gz" "$URL" || { echo "download failed"; return 1; }
  tar -xzf "$DATA/python.tar.gz" -C "$DATA" || { echo "unpack failed"; return 1; }   # -> $DATA/python
  rm -f "$DATA/python.tar.gz"
}

findpy || { download_python && findpy; } || {
  echo "No usable Python. Install Python 3.12 from python.org (it includes Tk), then restart Claude."
  exit 1
}
echo "python: $PY"

"$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip --user
rm -rf "$LIB"
mkdir -p "$LIB"
"$PY" -m pip install --disable-pip-version-check --no-warn-script-location --target "$LIB" \
  "mcp>=2.3.0,<3" "httpx>=0.27.0" "pydantic>=2.6.0" "keyring>=24.0.0" "openpyxl>=3.1,<4" \
  || { echo "pip install failed"; exit 1; }
SAP_MCP_LIB="$LIB" "$PY" -s "$HERE/server/$ENTRY" --check || { echo "import check failed"; exit 1; }

# Other versions' ready markers stay (an older running version must not re-run setup).
printf %s "$PY" > "$DATA/python.txt"
date > "$DATA/ready-$VER"
echo "setup complete"
exit 0
