#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "🚀 מפעיל BTC Advisor..."
echo ""

# Start backend
cd "$SCRIPT_DIR/backend"
python3 -m uvicorn main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

echo "✅ שרת בקאנד פועל על http://localhost:8000"
echo ""
echo "📊 פותח את האתר בדפדפן..."
sleep 1

# Open frontend
open "$SCRIPT_DIR/frontend/index.html"

echo ""
echo "לעצור: לחץ Ctrl+C"
echo ""

trap "kill $BACKEND_PID 2>/dev/null; echo 'שרת נסגר.'; exit 0" INT TERM
wait $BACKEND_PID
