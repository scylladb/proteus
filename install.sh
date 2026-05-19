#!/bin/bash
set -e

# Project root directory
dir="$(cd "$(dirname "$0")" && pwd)"
venv_dir="$dir/.venv"

# Create venv if not exists
if [ ! -d "$venv_dir" ]; then
    echo "[proteus] Creating local virtual environment in $venv_dir"
    python3 -m venv "$venv_dir"
fi

# Upgrade pip
"$venv_dir/bin/pip" install --upgrade pip

# Install dependencies from pyproject.toml (PEP 517/518)
if [ -f "$dir/pyproject.toml" ]; then
    echo "[proteus] Installing dependencies from pyproject.toml (editable mode)"
    "$venv_dir/bin/pip" install -e .
else
    echo "[proteus] ERROR: pyproject.toml not found in $dir"
    exit 1
fi

zshrc="$HOME/.zshrc"
alias_line="alias px=\"$venv_dir/bin/px\""

if [ -f "$zshrc" ]; then
    if ! grep -Fqx "$alias_line" "$zshrc"; then
        echo "[proteus] Adding px alias to $zshrc"
        printf '\n# Added by proteus install.sh\n%s\n' "$alias_line" >> "$zshrc"
    else
        echo "[proteus] px alias already present in $zshrc"
    fi
else
    echo "[proteus] Creating $zshrc with px alias"
    printf '# Added by proteus install.sh\n%s\n' "$alias_line" > "$zshrc"
fi

# Link config to ~/.config/proteus/ so px works from any directory
config_dir="$HOME/.config/proteus"
config_link="$config_dir/config.yml"
config_src="$dir/config.yml"

mkdir -p "$config_dir"
if [ ! -e "$config_link" ]; then
    echo "[proteus] Linking config: $config_link -> $config_src"
    ln -sf "$config_src" "$config_link"
else
    echo "[proteus] Config link already exists: $config_link"
fi

echo "[proteus] Installation complete."
echo "[proteus] To run the CLI, use:"
echo "    $venv_dir/bin/px ..."
echo "Or restart your shell and use: px ..."
