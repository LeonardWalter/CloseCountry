#! /bin/bash
cd "$(dirname "$0")" || exit

echo "Starting Gunicorn..."
gunicorn --workers 4 --bind 127.0.0.1:5000 app:app