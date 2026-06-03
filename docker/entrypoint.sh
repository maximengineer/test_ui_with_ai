#!/bin/sh
set -eu

python -m test_ui.network_sandbox

case "${1:-}" in
  python|tail|sh|bash|uvicorn|/*)
    exec "$@"
    ;;
  *)
    exec python -m test_ui "$@"
    ;;
esac
