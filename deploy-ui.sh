#!/usr/bin/env bash
# Build the React interface and publish it to the directory served by FastAPI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT/frontend"

echo "==> npm run build"
npm run build

echo "==> dong bo frontend/dist sang static/"
rm -rf "$ROOT/static/assets"
cp -r "$ROOT/frontend/dist/assets" "$ROOT/static/assets"
cp "$ROOT/frontend/dist/index.html" "$ROOT/static/index.html"

echo "==> khoi dong lai fpm.service"
systemctl restart fpm.service
sleep 2
systemctl is-active --quiet fpm.service && echo "    fpm.service: active"

echo "==> kiem tra"
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9088/)
echo "    GET /            -> $code"
for asset in $(grep -o '/static/assets/[^"]*' "$ROOT/static/index.html"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:9088$asset")
    echo "    GET $asset -> $code"
    [ "$code" = "200" ] || { echo "    LOI: asset khong phai 200"; exit 1; }
done
[ "$code" = "200" ] || { echo "    LOI: trang chinh khong phai 200"; exit 1; }

echo "==> xong. Ban rollback: cp /tmp/kilo/index.legacy.html static/index.html && systemctl restart fpm.service"
