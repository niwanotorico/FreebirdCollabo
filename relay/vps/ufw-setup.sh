#!/bin/sh
# Firewall for the relay VPS: only SSH, HTTP (certificate) and HTTPS (wss://) are open.
# The relay itself (7788) listens on 127.0.0.1 only and is never opened here.
#
#   sudo sh ufw-setup.sh
#
# SSH is allowed FIRST so enabling the firewall cannot lock you out.
# If you changed the SSH port, run   sudo ufw allow <port>/tcp   before this script.
set -e
ufw limit 22/tcp           # SSH remote login (limit = blocks password-guessing bursts)
ufw allow 80/tcp           # Let's Encrypt HTTP challenge + redirect to https
ufw allow 443/tcp          # wss:// (HTTPS)
ufw default deny incoming
ufw default allow outgoing
ufw --force enable
ufw status verbose
