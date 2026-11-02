#!/usr/bin/env bash
# 把 frontend/ 源文件同步到网关静态目录 server/static/
# 静态资源以相对路径引用（static/xxx），网关根路径与 nginx /piagent/ 前缀下均可运行
set -euo pipefail
cd "$(dirname "$0")/.."

cp frontend/index.html server/static/index.html
cp frontend/style.css  server/static/style.css
cp frontend/app.js     server/static/app.js
echo "已同步 frontend/ -> server/static/（index.html / style.css / app.js）"
