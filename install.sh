#!/bin/sh
set -eu

REPO="git+https://github.com/stanuser297/niji-agent-next.git@main"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if command -v python3 >/dev/null 2>&1; then
    PYTHON="${PYTHON:-python3}"
elif command -v python >/dev/null 2>&1; then
    PYTHON="${PYTHON:-python}"
else
    PYTHON="python3"
fi

uid=$(id -u 2>/dev/null || echo 1)
is_termux=0
python_path=$(command -v "$PYTHON" 2>/dev/null || true)
if [ -n "${PREFIX:-}" ] && [ -x "$PREFIX/bin/pkg" ]; then
    case "$python_path" in
        "$PREFIX"/*) is_termux=1 ;;
    esac
fi

# Bootstrap native tools for the current OS. Debian/proot uses apt; Termux
# requires pkg as the unprivileged Termux app user.
needs_bootstrap=0
command -v git >/dev/null 2>&1 || needs_bootstrap=1
venv_probe=$(mktemp -d 2>/dev/null || echo "${TMPDIR:-/tmp}/niji-venv-probe-$$")
if ! "$PYTHON" -m venv "$venv_probe/venv" >/dev/null 2>&1; then
    needs_bootstrap=1
fi
rm -rf "$venv_probe"

if [ "$needs_bootstrap" -eq 1 ]; then
    if [ "$is_termux" -eq 1 ]; then
        if [ "$uid" -eq 0 ]; then
            echo "Termux pkg cannot run as root. Exit the root shell and run this installer as the normal Termux user." >&2
            exit 1
        fi
        pkg install -y python git curl
    elif command -v apt-get >/dev/null 2>&1; then
        if [ "$uid" -eq 0 ]; then
            apt-get update
            DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip git curl
        elif command -v sudo >/dev/null 2>&1; then
            sudo apt-get update
            sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv python3-pip git curl
        else
            echo "Need root/sudo to install prerequisites: python3-venv, python3-pip, git, curl" >&2
            exit 1
        fi
    else
        echo "Install Python 3.10+, venv/pip, Git, and curl with your OS package manager, then rerun this command." >&2
        exit 1
    fi
fi

"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'

# Prefer an isolated venv. This also avoids Debian's externally-managed Python restriction.
if [ "$is_termux" -eq 1 ]; then
    APP_DIR="$PREFIX/var/lib/niji-agent"
    BIN_DIR="$PREFIX/bin"
elif [ "$uid" -eq 0 ]; then
    APP_DIR="/opt/niji-agent"
    BIN_DIR="/usr/local/bin"
else
    APP_DIR="${HOME}/.local/share/niji-agent"
    BIN_DIR="${HOME}/.local/bin"
fi
VENV="$APP_DIR/venv"
mkdir -p "$APP_DIR" "$BIN_DIR"
"$PYTHON" -m venv --clear "$VENV"
if [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/src/niji" ]; then
    PACKAGE_SOURCE="$SCRIPT_DIR"
else
    PACKAGE_SOURCE="$REPO"
fi
if ! "$VENV/bin/python" -m pip install --retries 10 --timeout 60 --no-cache-dir "$PACKAGE_SOURCE"; then
    echo "Could not download Python packages. This is a network/DNS issue, not an API-key problem." >&2
    echo "Check: getent hosts files.pythonhosted.org" >&2
    echo "If your network blocks PyPI, rerun with a trusted mirror via PIP_INDEX_URL." >&2
    exit 1
fi
"$VENV/bin/python" -c 'import niji, niji.setup_wizard; from niji.chat_prompt import read_chat_prompt; from niji.webui_frontend import PAGE; from importlib.metadata import version; v=version("niji-agent"); assert v == niji.__version__, f"package metadata mismatch: {v} != {niji.__version__}"; assert "NIJI AGENT" in PAGE; print("Installed niji-agent", v, "from", niji.__file__)'

printf '%s\n' '#!/bin/sh' "exec \"$VENV/bin/niji\" \"\$@\"" > "$BIN_DIR/niji"
chmod 755 "$BIN_DIR/niji"

echo "Installation complete. Launching niji setup/chat..."
if [ -r /dev/tty ]; then
    "$VENV/bin/python" -m niji </dev/tty
else
    echo "No interactive terminal detected. Run: $BIN_DIR/niji"
fi
