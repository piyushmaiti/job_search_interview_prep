#!/usr/bin/env bash
# Start the web interface. By default it is reachable from this computer only (http://127.0.0.1:8503).
#   ./run_ui.sh --lan    also lets other devices on your Wi-Fi in, for example your phone. It needs a passcode:
#                        put INTERVIEW_PREP_PASSCODE=... in .env (copy .env.example). Only use it on a network you trust.
# Keys are read from .env in this folder, or from your terminal environment.
cd "$(dirname "$0")"
export HF_HOME="$PWD/.cache/hf" SENTENCE_TRANSFORMERS_HOME="$PWD/.cache/st" STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ADDR=127.0.0.1
PORT=8503
ARGS=()
PREV=""
for a in "$@"; do
  case "$a" in --server.address|--server.address=*) echo "Use --lan to open the app to other devices; the address cannot be set directly."; exit 1;; esac
  if [ "$a" = "--lan" ]; then ADDR=0.0.0.0; export INTERVIEW_PREP_LAN=1; else ARGS+=("$a"); fi
  if [ "$PREV" = "--server.port" ]; then PORT="$a"; fi
  PREV="$a"
done
if [ "$ADDR" = "0.0.0.0" ]; then
  if [ -z "${INTERVIEW_PREP_PASSCODE:-}" ] && ! grep -Eq '^(export )?INTERVIEW_PREP_PASSCODE=.+' .env 2>/dev/null; then
    echo "Refusing to start for other devices without a passcode."
    echo "Add a line  INTERVIEW_PREP_PASSCODE=your-own-passcode  to .env (copy .env.example), then run  ./run_ui.sh --lan  again."
    exit 1
  fi
  echo "On your phone (same Wi-Fi), open:  http://$(hostname -I 2>/dev/null | awk '{print $1}'):$PORT"
fi
exec .venv/bin/streamlit run app.py --server.address "$ADDR" --server.port "$PORT" "${ARGS[@]}"
