#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Fetch external benchmark designs at pinned commits into bench/external/.
# Re-run to refresh; existing checkouts at the right commit are left alone.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p external

fetch() {  # name url sha
  local name=$1 url=$2 sha=$3 dir=external/$1
  if [ -d "$dir/.git" ] && [ "$(git -C "$dir" rev-parse HEAD 2>/dev/null)" = "$sha" ]; then
    echo "ok       $name @ ${sha:0:7}"; return
  fi
  rm -rf "$dir"; mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" remote add origin "$url"
  git -C "$dir" fetch -q --depth 1 origin "$sha"
  git -C "$dir" checkout -q FETCH_HEAD
  echo "fetched  $name @ ${sha:0:7}"
}

fetch zxip            https://github.com/shalan/zxip.git            3964ddb63e249e55afffe990ef837d14a2769252
fetch zx16            https://github.com/shalan/zx16.git            64839e48ade02ac83e86b4ccadbfbd16e6900785
fetch ms_psram_ahb    https://github.com/shalan/ms_psram_ahb.git    18851332589a53e3d60ab987cbf656a217c60273
fetch uart_apb_master https://github.com/shalan/uart_apb_master.git 2866672680e7ad25781d1dbf8e74e451e249e13c
