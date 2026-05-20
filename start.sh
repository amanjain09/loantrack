#!/bin/bash
# LoanTrack — Start script
echo ""
echo "======================================================"
echo "  🏦  LoanTrack — Lending Management System"
echo "  👉  Open: http://localhost:8080"
echo "  🔑  Login: admin / admin123"
echo "  📌  Press Ctrl+C to stop"
echo "======================================================"
echo ""
cd "$(dirname "$0")"
python3 app.py
