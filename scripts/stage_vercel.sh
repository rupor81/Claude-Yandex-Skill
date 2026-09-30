#!/bin/sh
# Stage the remote connector for `vercel deploy`: the four packages it needs, the
# entry point, and a requirements.txt Vercel installs from. Kept out of the
# workspace root so Vercel never sees the workspace's own pyproject.
set -eu
cd "$(dirname "$0")/.."
out=dist/vercel
rm -rf "$out" && mkdir -p "$out/packages"
cp -R deploy/vercel/. "$out/"
for p in yandex-core yandex-calendar-mcp yandex-mail-mcp yandex-remote-mcp; do
  rsync -a --exclude __pycache__ "packages/$p" "$out/packages/"
done
# A workspace of its own: the packages refer to each other as workspace members,
# and Vercel builds a uv project from whatever it finds, so it must find one.
cat > "$out/pyproject.toml" <<'TOML'
[project]
name = "yandex-mcp-vercel"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["yandex-remote-mcp"]

[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
yandex-remote-mcp = { workspace = true }
TOML
echo 3.13 > "$out/.python-version"
echo "staged in $out"
