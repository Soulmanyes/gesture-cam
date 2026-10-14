#!/bin/sh
# Publishes the committed code as a download on https://everyday-home.duckdns.org/apps/gesture-cam/.
# The page itself lives in the Everyday repo (deploy/apps/); this only uploads the archive and version.json.
set -e
cd "$(dirname "$(readlink -f "$0")")"

if [ -n "$(git status --porcelain)" ]; then
    echo "Commit your changes first." >&2; exit 1
fi

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
# git archive takes only tracked files, so .venv and __pycache__ stay out.
git archive --prefix=gesture-cam/ -o "$out/gesture-cam.tar.gz" HEAD
printf '{"version": "%s (%s)"}\n' "$(git log -1 --format=%cs)" "$(git rev-parse --short HEAD)" > "$out/version.json"

ssh oracle 'mkdir -p ~/everyday-app/deploy/apps/gesture-cam'
rsync -az "$out/gesture-cam.tar.gz" oracle:~/everyday-app/deploy/apps/gesture-cam/
rsync -az "$out/version.json" oracle:~/everyday-app/deploy/apps/gesture-cam/
echo "Published $(cat "$out/version.json")"
