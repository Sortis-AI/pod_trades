#!/usr/bin/env bash
# Pod The Trader — one-shot installer.
#
#   curl -LsSf https://level5.cloud/install.sh | bash
#
# Installs git + uv + Python 3.12, clones the repo, syncs dependencies,
# and drops a launcher at ~/.local/bin/pod-the-trader.
#
# Package manager priority:
#   Linux: snap  -> apt
#   macOS: brew
#
# Environment overrides:
#   POD_TRADER_REPO   git URL to clone from (default: github.com/Sortis-AI/pod_trades)
#   POD_TRADER_DIR    install directory     (default: ~/pod-the-trader)
#   POD_TRADER_REF    branch/tag/ref        (default: main)

set -euo pipefail

REPO_URL="${POD_TRADER_REPO:-https://github.com/Sortis-AI/pod_trades.git}"
INSTALL_DIR="${POD_TRADER_DIR:-$HOME/pod-the-trader}"
REF="${POD_TRADER_REF:-main}"
LOCAL_BIN="$HOME/.local/bin"

bold()   { printf '\033[1m%s\033[0m\n' "$*"; }
info()   { printf '  \033[36m›\033[0m %s\n' "$*"; }
ok()     { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()   { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail()   { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
need()   { command -v "$1" >/dev/null 2>&1; }

detect_os() {
    case "$(uname -s)" in
        Linux)  echo linux ;;
        Darwin) echo macos ;;
        *)      fail "Unsupported OS: $(uname -s). Pod The Trader installs only on Linux and macOS." ;;
    esac
}

# ---- Package install helpers ------------------------------------------------

# Installs $1 via the best available package manager for the OS, preferring
# snap then apt on Linux, brew on macOS. Uses sudo only if the caller isn't
# already root.
pkg_install() {
    local pkg="$1"
    case "$OS" in
        linux)
            if need snap; then
                info "snap install $pkg"
                if $SUDO snap install "$pkg" 2>/dev/null; then
                    ok "installed $pkg via snap"
                    return 0
                fi
                warn "snap couldn't install $pkg; falling back to apt"
            fi
            if need apt-get; then
                info "apt-get install -y $pkg"
                $SUDO apt-get update -qq
                $SUDO apt-get install -y "$pkg"
                ok "installed $pkg via apt"
                return 0
            fi
            fail "No supported package manager found (need snap or apt) to install $pkg"
            ;;
        macos)
            if ! need brew; then
                fail "Homebrew not installed. Install it from https://brew.sh and re-run this script."
            fi
            info "brew install $pkg"
            brew install "$pkg"
            ok "installed $pkg via brew"
            ;;
    esac
}

# ---- Prerequisite installers ------------------------------------------------

ensure_git() {
    if need git; then
        ok "git already installed ($(git --version | head -1))"
        return
    fi
    info "git not found — installing"
    pkg_install git
}

ensure_curl() {
    if need curl; then
        ok "curl already installed"
        return
    fi
    info "curl not found — installing"
    pkg_install curl
}

ensure_uv() {
    if need uv; then
        ok "uv already installed ($(uv --version))"
        return
    fi
    info "uv not found — installing via astral.sh installer"
    # The official installer drops uv into ~/.local/bin and prints a shell
    # hint. We export the path ourselves so the rest of this script can
    # find it in the same process.
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if ! need uv; then
        fail "uv install succeeded but the binary is not on PATH. Open a new shell and re-run."
    fi
    ok "installed uv ($(uv --version))"
}

ensure_python() {
    # uv can manage Python itself, but we still sanity-check that SOMETHING
    # usable is available so `uv sync` won't explode later.
    if need python3 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
        ok "system python3 is $(python3 --version)"
        return
    fi
    info "python 3.12+ not found — letting uv manage it"
    uv python install 3.12
    ok "installed python 3.12 via uv"
}

# ---- Repo clone + sync ------------------------------------------------------

clone_repo() {
    if [ -d "$INSTALL_DIR/.git" ]; then
        info "updating existing checkout at $INSTALL_DIR"
        git -C "$INSTALL_DIR" fetch --quiet origin "$REF"
        git -C "$INSTALL_DIR" checkout --quiet "$REF"
        git -C "$INSTALL_DIR" reset --hard --quiet "origin/$REF" 2>/dev/null || \
            git -C "$INSTALL_DIR" reset --hard --quiet "$REF"
        ok "updated $INSTALL_DIR to $REF"
    else
        info "cloning $REPO_URL → $INSTALL_DIR"
        git clone --quiet --branch "$REF" "$REPO_URL" "$INSTALL_DIR"
        ok "cloned into $INSTALL_DIR"
    fi
}

sync_deps() {
    info "installing dependencies via uv sync"
    ( cd "$INSTALL_DIR" && uv sync --quiet )
    ok "dependencies installed"
}

# ---- Launcher ---------------------------------------------------------------

install_launcher() {
    mkdir -p "$LOCAL_BIN"
    local launcher="$LOCAL_BIN/pod-the-trader"
    # Plain shim: cd into the install dir, exec uv. The `update`
    # subcommand is handled inside the Python entry point now so both
    # the Linux/macOS bash launcher and the Windows .cmd launcher can
    # stay tiny and dumb.
    cat > "$launcher" <<EOF
#!/usr/bin/env bash
# Generated by pod-the-trader install.sh — do not edit by hand.
# Re-run the installer to refresh this launcher.
set -e
cd "$INSTALL_DIR"
exec uv run pod-the-trader "\$@"
EOF
    chmod +x "$launcher"
    ok "launcher installed at $launcher"
}

# ---- PATH / shell profile ---------------------------------------------------

# If ~/.local/bin isn't on PATH yet, append an export line to the user's
# shell rc so the next shell picks it up. Idempotent: checks for an
# existing reference before writing. We still warn in the final output
# so the user knows to open a new shell or source the rc.
ensure_local_bin_on_path() {
    case ":$PATH:" in
        *":$LOCAL_BIN:"*)
            ok "$LOCAL_BIN already on PATH"
            PATH_UPDATED_RC=""
            return
            ;;
    esac

    # Pick the rc file for the user's login shell. Falls back to .profile
    # if we can't identify the shell. This is a best-effort modification;
    # the user will still see a "start a new shell" hint at the end.
    local shell_name rc
    shell_name="$(basename "${SHELL:-/bin/bash}")"
    case "$shell_name" in
        zsh)  rc="$HOME/.zshrc" ;;
        bash)
            # macOS bash uses .bash_profile for login shells; Linux bash
            # typically uses .bashrc. Prefer whichever exists, defaulting
            # to .bashrc on Linux and .bash_profile on macOS.
            if [ "$OS" = macos ] && [ -f "$HOME/.bash_profile" ]; then
                rc="$HOME/.bash_profile"
            elif [ -f "$HOME/.bashrc" ]; then
                rc="$HOME/.bashrc"
            elif [ "$OS" = macos ]; then
                rc="$HOME/.bash_profile"
            else
                rc="$HOME/.bashrc"
            fi
            ;;
        fish) rc="$HOME/.config/fish/config.fish" ;;
        *)    rc="$HOME/.profile" ;;
    esac

    # Idempotent: skip if an equivalent export is already present.
    if [ -f "$rc" ] && grep -q '\.local/bin' "$rc" 2>/dev/null; then
        ok "$rc already references .local/bin"
        PATH_UPDATED_RC="$rc"
        return
    fi

    mkdir -p "$(dirname "$rc")"
    {
        echo ""
        echo "# Added by pod-the-trader install.sh"
        if [ "$shell_name" = fish ]; then
            echo "set -gx PATH \$HOME/.local/bin \$PATH"
        else
            echo 'export PATH="$HOME/.local/bin:$PATH"'
        fi
    } >> "$rc"
    ok "added \$HOME/.local/bin to PATH in $rc"
    PATH_UPDATED_RC="$rc"
    # Export for the rest of this script process so any subsequent checks
    # see the new PATH without needing a fresh shell.
    export PATH="$LOCAL_BIN:$PATH"
}

# ---- Main -------------------------------------------------------------------

OS="$(detect_os)"
SUDO=""
if [ "$(id -u)" -ne 0 ] && need sudo; then
    SUDO="sudo"
fi

PATH_UPDATED_RC=""

bold "Pod The Trader — installer"
echo "  OS:           $OS"
echo "  install dir:  $INSTALL_DIR"
echo "  repo:         $REPO_URL"
echo "  ref:          $REF"
echo

bold "[1/6] Prerequisites"
ensure_curl
ensure_git
ensure_uv
ensure_python
echo

bold "[2/6] Source"
clone_repo
echo

bold "[3/6] Dependencies"
sync_deps
echo

bold "[4/6] Launcher"
install_launcher
echo

bold "[5/6] Shell PATH"
ensure_local_bin_on_path
echo

bold "[6/6] Done"
echo
if [ -n "$PATH_UPDATED_RC" ]; then
    echo "  Open a new terminal (or run \`source $PATH_UPDATED_RC\`) so the"
    echo "  updated PATH takes effect, then start the bot with:"
else
    echo "  Start the bot with:"
fi
echo
echo "      pod-the-trader"
echo
echo "  On first launch you will be asked to accept a disclaimer."
echo "  You must type \"I ACCEPT\" to continue."
echo
echo "  To upgrade to the latest version later, run:"
echo
echo "      pod-the-trader update"
echo
