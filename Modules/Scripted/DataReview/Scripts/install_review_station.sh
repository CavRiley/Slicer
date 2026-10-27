#!/usr/bin/env bash
# Bootstrap a Data Review station on macOS or Linux in one go:
#   1. fetch (or update) only the DataReview module folder from the git repository (sparse clone, ~1 MB),
#   2. find the installed Slicer (optionally download and install it on macOS),
#   3. run setup_review_station.py so Slicer starts directly in Data Review with the given manifest.
#
# One-liner on a fresh machine (the manifest must already exist, see README "Create the manifest"):
#   curl -fsSL https://raw.githubusercontent.com/CavRiley/Slicer/data-review-module/Modules/Scripted/DataReview/Scripts/install_review_station.sh \
#     | bash -s -- --manifest /path/to/dataset/review_manifest.json --reviewer "JD"
#
# Re-running the script updates the module (git pull) and re-applies the settings.
# From an existing full clone of the repository, pass it with --dest; it is used as is (no pull, no sparse checkout):
#   bash Modules/Scripted/DataReview/Scripts/install_review_station.sh --dest /path/to/Slicer --manifest ...
set -euo pipefail

REPO_URL="https://github.com/CavRiley/Slicer.git"
BRANCH="data-review-module"
MODULE_SUBDIR="Modules/Scripted/DataReview"
DEST="$HOME/DataReview"
MANIFEST=""
REVIEWER=""
SLICER_EXE=""
FULL_UI=""
SKIP_SETUP=""
INSTALL_SLICER=""
RESET=""

usage() {
  cat <<EOF
Usage: install_review_station.sh --manifest PATH [options]

  --manifest PATH     review_manifest.json to open automatically at startup (required unless --skip-setup/--reset)
  --reviewer NAME     reviewer name stored with each decision
  --dest DIR          where to keep the module checkout (default: $DEST); an existing full clone of the
                      repository is used as is
  --repo URL          git repository (default: $REPO_URL)
  --branch NAME       git branch (default: $BRANCH)
  --slicer PATH       Slicer executable (default: auto-detect)
  --install-slicer    macOS only: download the latest stable Slicer into /Applications if none is found
  --full-ui           keep the regular Slicer menus and toolbars
  --skip-setup        only fetch/update the module, do not change Slicer settings
  --reset             remove the Data Review startup settings from Slicer
  -h, --help          this text
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest) MANIFEST="$2"; shift 2 ;;
    --reviewer) REVIEWER="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --slicer) SLICER_EXE="$2"; shift 2 ;;
    --install-slicer) INSTALL_SLICER=1; shift ;;
    --full-ui) FULL_UI=1; shift ;;
    --skip-setup) SKIP_SETUP=1; shift ;;
    --reset) RESET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ -z "$MANIFEST" && -z "$SKIP_SETUP" && -z "$RESET" ]]; then
  echo "ERROR: --manifest is required (or use --skip-setup / --reset)" >&2
  exit 2
fi
if [[ -n "$MANIFEST" && ! -f "$MANIFEST" ]]; then
  echo "ERROR: manifest not found: $MANIFEST" >&2
  exit 2
fi
command -v git >/dev/null || { echo "ERROR: git is required (macOS: run 'xcode-select --install')" >&2; exit 2; }

# --- 1. Fetch or update the module folder ---------------------------------------------------------
echo "==> Module checkout: $DEST"
if [[ -d "$DEST/.git" ]] || git -C "$DEST" rev-parse --show-toplevel >/dev/null 2>&1; then
  if [[ "$(git -C "$DEST" config --get core.sparseCheckout || true)" == "true" ]]; then
    git -C "$DEST" sparse-checkout set "$MODULE_SUBDIR"
    git -C "$DEST" pull --ff-only -q
    echo "    updated to $(git -C "$DEST" rev-parse --short HEAD)"
  else
    # A full clone of the repository (e.g. a development checkout): use it as it is, never pull or
    # convert it to a sparse checkout.
    echo "    using existing clone as is ($(git -C "$DEST" rev-parse --short HEAD), branch $(git -C "$DEST" rev-parse --abbrev-ref HEAD))"
  fi
else
  mkdir -p "$(dirname "$DEST")"
  git clone -q --depth 1 --filter=blob:none --sparse -b "$BRANCH" "$REPO_URL" "$DEST"
  git -C "$DEST" sparse-checkout set "$MODULE_SUBDIR"
  echo "    cloned $(git -C "$DEST" rev-parse --short HEAD) of branch $BRANCH"
fi
MODULE_DIR="$DEST/$MODULE_SUBDIR"
[[ -f "$MODULE_DIR/DataReview.py" ]] || { echo "ERROR: module not found at $MODULE_DIR" >&2; exit 1; }

# --- 2. Find Slicer -------------------------------------------------------------------------------
find_slicer() {
  if [[ -n "$SLICER_EXE" ]]; then echo "$SLICER_EXE"; return; fi
  if [[ "$(uname)" == "Darwin" ]]; then
    for app in /Applications/Slicer.app "$HOME/Applications/Slicer.app" /Applications/Slicer*.app; do
      [[ -x "$app/Contents/MacOS/Slicer" ]] && { echo "$app/Contents/MacOS/Slicer"; return; }
    done
  else
    command -v Slicer 2>/dev/null && return
    for dir in "$HOME"/Slicer* /opt/Slicer* /opt/slicer*; do
      [[ -x "$dir/Slicer" ]] && { echo "$dir/Slicer"; return; }
    done
  fi
}

install_slicer_macos() {
  local dmg="/tmp/Slicer-install.dmg" mount
  echo "==> Downloading the latest stable Slicer for macOS (about 450 MB)"
  curl -fL --progress-bar -o "$dmg" "https://download.slicer.org/download?os=macosx&stability=release"
  mount=$(hdiutil attach -nobrowse -readonly "$dmg" | awk -F'\t' '/\/Volumes\//{print $NF}' | tail -1)
  [[ -d "$mount" ]] || { echo "ERROR: could not mount $dmg" >&2; exit 1; }
  local app
  app=$(find "$mount" -maxdepth 1 -name "*.app" | head -1)
  echo "==> Installing $(basename "$app") into /Applications"
  cp -R "$app" /Applications/Slicer.app
  hdiutil detach -quiet "$mount"
  rm -f "$dmg"
  xattr -dr com.apple.quarantine /Applications/Slicer.app 2>/dev/null || true
}

SLICER=$(find_slicer || true)
if [[ -z "$SLICER" && -n "$INSTALL_SLICER" && "$(uname)" == "Darwin" ]]; then
  install_slicer_macos
  SLICER=/Applications/Slicer.app/Contents/MacOS/Slicer
fi
if [[ -z "$SLICER" || ! -x "$SLICER" ]]; then
  echo "ERROR: Slicer not found. Install it from https://download.slicer.org (or pass --slicer PATH," >&2
  echo "       or on macOS add --install-slicer), then run this script again." >&2
  exit 1
fi
echo "==> Slicer: $SLICER"

# --- 3. Configure Slicer ---------------------------------------------------------------------------
if [[ -n "$SKIP_SETUP" ]]; then
  echo "==> Skipping Slicer setup (--skip-setup). Module folder: $MODULE_DIR"
  exit 0
fi
SETUP="$MODULE_DIR/Scripts/setup_review_station.py"
args=()
if [[ -n "$RESET" ]]; then
  args+=(--reset)
else
  args+=(--manifest "$MANIFEST")
  [[ -n "$REVIEWER" ]] && args+=(--reviewer "$REVIEWER")
  [[ -n "$FULL_UI" ]] && args+=(--full-ui)
fi
echo "==> Configuring Slicer"
set +e
"$SLICER" --no-splash --no-main-window --python-script "$SETUP" "${args[@]}" 2>&1 \
  | grep -v "Error #1 while writing setting\|Switch to module\|Failed to create extensions directory\|no main window is available"
status=${PIPESTATUS[0]}
set -e
if [[ $status -ne 0 ]]; then
  echo "ERROR: Slicer setup did not complete (exit code $status); see the message above." >&2
  exit "$status"
fi

if [[ -z "$RESET" ]]; then
  echo
  echo "Done. Start Slicer normally to begin reviewing:"
  echo "  $SLICER"
  echo "Ctrl+Shift+U shows the full interface; re-run with --reset to restore a regular Slicer."
fi
