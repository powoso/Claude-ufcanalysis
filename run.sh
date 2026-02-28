#!/bin/bash
# UFC Betting Analyzer — quick launcher
# Usage:
#   ./run.sh              Launch the web dashboard
#   ./run.sh scrape       Run the data pipeline first, then launch
#   ./run.sh pipeline     Run full pipeline (scrape + train + analyze + dashboard)

set -e
cd "$(dirname "$0")"

# Find python
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "Error: Python 3 is not installed."
    echo ""
    echo "Install it with Homebrew:"
    echo "  brew install python@3.11"
    echo ""
    echo "Or download from https://www.python.org/downloads/"
    exit 1
fi

# Check version
PY_VERSION=$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Using $PY ($PY_VERSION)"

# Check if dependencies are installed
if ! $PY -c "import flask" 2>/dev/null; then
    echo "Installing dependencies..."
    $PY -m pip install -r requirements.txt
fi

case "${1:-web}" in
    scrape)
        echo "Running data pipeline..."
        $PY main.py --scrape --quick
        echo ""
        echo "Launching web dashboard..."
        $PY webapp.py
        ;;
    pipeline)
        echo "Running full pipeline..."
        $PY main.py --quick
        echo ""
        echo "Launching web dashboard..."
        $PY webapp.py
        ;;
    web|"")
        $PY webapp.py
        ;;
    *)
        echo "Usage: ./run.sh [web|scrape|pipeline]"
        exit 1
        ;;
esac
