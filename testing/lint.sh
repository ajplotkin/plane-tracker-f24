#!/usr/bin/env bash
# pyflakes over the app, minus lines the code already suppresses.
#
# pyflakes has no `# noqa` support -- that belongs to flake8, which wraps it --
# so several deliberate constructs report forever: the availability probes in
# atc_audio.py (`import pychromecast` purely to see whether it is installed) and
# the h2/hpack pre-imports in fr24_client.py, which exist to warm the module
# cache BEFORE drop_privileges removes read access to the venv. Deleting those
# would break the thing they protect, so this filters them by reading the line
# pyflakes points at and skipping it when it carries a `# noqa`.
#
# Usage: ./lint.sh [app-dir]     (default: ../its-a-plane-python)
set -euo pipefail

APP="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../its-a-plane-python" && pwd)}"
[ -d "$APP" ] || { echo "no such app dir: $APP" >&2; exit 2; }
cd "$APP"

found=0
while IFS=: read -r file line rest; do
  [ -n "${file:-}" ] || continue
  case "$(sed -n "${line}p" "$file" 2>/dev/null)" in
    *"# noqa"*) ;;
    *) echo "  $file:$line:$rest"; found=1 ;;
  esac
done < <(python -m pyflakes utilities/*.py web/app.py tests/*.py scenes/*.py setup/*.py 2>/dev/null || true)

if [ "$found" -eq 0 ]; then
  echo "lint clean ($APP)"
else
  echo "lint findings above" >&2
  exit 1
fi
