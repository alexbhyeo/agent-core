#!/usr/bin/env bash
# Regenerates the self-signed TLS key pair used to serve the A2UI agent over
# wss://. The private key (server.key) must never leave this machine -- only
# server.crt (the public certificate) gets copied to clients for pinning.
#
# Edit certs/san.cnf's [alt_names] section if the backend's IP/hostname
# changes, then rerun this script.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout server.key -out server.crt \
  -days 3650 \
  -config san.cnf -extensions v3_req

chmod 600 server.key
echo "Generated server.crt / server.key ($(openssl x509 -in server.crt -noout -enddate))"
