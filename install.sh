#!/bin/bash
# Interactive macOS setup. Configuration is never sourced as shell code.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd -- "$project_dir"

setup_only=false
for argument in "$@"; do
    case "$argument" in
        --uninstall|--help|-h) setup_only=true ;;
    esac
done

python_bin=""
for candidate in python3.14 "$project_dir/.venv/bin/python" /opt/homebrew/bin/python3.14 /usr/local/bin/python3.14 python3; do
    candidate_path="$(command -v "$candidate" 2>/dev/null || true)"
    if [[ -n "$candidate_path" ]] && "$candidate_path" -c 'import sys; raise SystemExit(not (sys.version_info >= (3, 10) if sys.argv[1] == "true" else sys.version_info[:2] == (3, 14)))' "$setup_only" 2>/dev/null; then
        python_bin="$candidate_path"
        break
    fi
done
if [[ -z "$python_bin" ]]; then
    echo 'Python 3.14 is required for installation (Python 3.10+ suffices for uninstall/help).' >&2
    echo 'Install Python 3.14, then run this script again.' >&2
    exit 1
fi
if "$setup_only"; then
    exec "$python_bin" "$project_dir/scripts/install.py" "$@"
fi
if [[ "$(uname -s)" != Darwin ]]; then
    echo 'This installer supports macOS launchd only.' >&2
    exit 1
fi
if [[ "$(id -u)" == 0 ]]; then
    echo 'Run ./install.sh as your normal user; daemon operations request sudo when needed.' >&2
    exit 1
fi

uv_bin="$(command -v uv 2>/dev/null || true)"
if [[ -z "$uv_bin" && -x "$project_dir/.tools/bin/uv" ]]; then
    uv_bin="$project_dir/.tools/bin/uv"
fi
if [[ -z "$uv_bin" ]]; then
    echo 'uv is required: https://docs.astral.sh/uv/getting-started/installation/' >&2
    exit 1
fi
# Keep the lockfile unchanged and require the installed Python; never download Python.
"$uv_bin" sync --locked --no-dev --no-python-downloads --python "$python_bin"
exec "$project_dir/.venv/bin/python" "$project_dir/scripts/install.py" "$@"
