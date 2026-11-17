#!/usr/bin/env bash
set -e
if [ -f data/item_master.csv ]; then
  python -m backend.test_engine
else
  echo "Task data not present; skipping data-dependent tests. Import the task kit in the app before processing a batch."
fi
uvicorn backend.main:app --reload --port 8000
