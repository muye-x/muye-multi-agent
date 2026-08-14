#!/bin/sh
set -eu

mode="${MUYE_GATEWAY_MODE:-https}"
case "${mode}" in
  http)
    template=/etc/nginx/muye-gateway/http.conf.template
    ;;
  https)
    template=/etc/nginx/muye-gateway/https.conf.template
    ;;
  *)
    echo "Unsupported MUYE_GATEWAY_MODE: ${mode}" >&2
    exit 1
    ;;
esac

cp "${template}" /etc/nginx/templates/default.conf.template
