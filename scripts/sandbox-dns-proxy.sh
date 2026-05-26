#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Standalone DNS proxy setup for NemoClaw sandbox pods.
# Run directly inside the sandbox pod — no nemoclaw CLI or kubectl required.
#
# What it does:
#   1. Reads the kube-dns upstream from the pod's own /etc/resolv.conf
#   2. Detects the veth gateway (10.200.0.x) bridging the pod to the sandbox netns
#   3. Starts a Python UDP forwarder on <veth-gateway>:53 -> kube-dns:53
#   4. Inside the sandbox netns: adds an iptables ACCEPT rule and rewrites resolv.conf
#   5. Verifies the setup with four checks
#
# Usage: bash sandbox-dns-proxy.sh

set -euo pipefail

DEFAULT_DNS_UPSTREAM="8.8.8.8"
DEFAULT_VETH_GATEWAY="10.200.0.1"

log() { echo "[dns-proxy-setup] $*"; }

# ---------------------------------------------------------------------------
# 1. Discover kube-dns upstream from the pod's resolv.conf
# ---------------------------------------------------------------------------
dns_upstream=""
if [ -f /etc/resolv.conf ]; then
  dns_upstream=$(grep -m1 '^nameserver' /etc/resolv.conf | awk '{print $2}' || true)
fi
if [ -z "$dns_upstream" ]; then
  log "WARNING: Could not read nameserver from /etc/resolv.conf. Falling back to ${DEFAULT_DNS_UPSTREAM}."
  log "WARNING: k8s-internal names (inference.local routing) will NOT work."
  dns_upstream="$DEFAULT_DNS_UPSTREAM"
fi
log "DNS upstream: $dns_upstream"

# Safety check — same regex as isSafeDnsAddress in the TypeScript source
if ! echo "$dns_upstream" | grep -qE '^[a-zA-Z0-9.:_-]+$'; then
  log "ERROR: DNS upstream '${dns_upstream}' contains invalid characters. Aborting."
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Detect veth gateway (10.200.0.x interface in the pod netns)
# ---------------------------------------------------------------------------
veth_gateway=$(ip addr show | grep 'inet 10\.200\.0\.' | awk '{print $2}' | cut -d/ -f1 | head -1 || true)
if [ -z "$veth_gateway" ]; then
  log "WARNING: Could not detect 10.200.0.x veth interface. Using default ${DEFAULT_VETH_GATEWAY}."
  veth_gateway="$DEFAULT_VETH_GATEWAY"
fi
if ! echo "$veth_gateway" | grep -qE '^[a-zA-Z0-9.:_-]+$'; then
  log "ERROR: Veth gateway '${veth_gateway}' contains invalid characters. Aborting."
  exit 1
fi
log "Veth gateway: $veth_gateway"

# ---------------------------------------------------------------------------
# 3. Find sandbox network namespace
# ---------------------------------------------------------------------------
sandbox_ns=$(ls /run/netns/ 2>/dev/null | grep -i sandbox | head -1 || true)
if [ -z "$sandbox_ns" ]; then
  log "WARNING: Could not find sandbox network namespace in /run/netns/. DNS routing into sandbox will not be configured."
fi

# ---------------------------------------------------------------------------
# 4. Write and start Python DNS forwarder (runs in pod netns)
# ---------------------------------------------------------------------------
cat > /tmp/dns-proxy.py << 'DNSPROXY'
import socket, threading, os, sys

UPSTREAM = (sys.argv[1] if len(sys.argv) > 1 else '8.8.8.8', 53)
BIND_IP = sys.argv[2] if len(sys.argv) > 2 else '0.0.0.0'

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((BIND_IP, 53))

with open('/tmp/dns-proxy.pid', 'w') as pf:
    pf.write(str(os.getpid()))

msg = 'dns-proxy: {}:53 -> {}:{} pid={}'.format(BIND_IP, UPSTREAM[0], UPSTREAM[1], os.getpid())
print(msg, flush=True)
with open('/tmp/dns-proxy.log', 'w') as log:
    log.write(msg + '\n')

def forward(data, addr):
    try:
        f = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        f.settimeout(5)
        f.sendto(data, UPSTREAM)
        r, _ = f.recvfrom(4096)
        sock.sendto(r, addr)
        f.close()
    except Exception:
        pass

while True:
    d, a = sock.recvfrom(4096)
    threading.Thread(target=forward, args=(d, a), daemon=True).start()
DNSPROXY

# Kill previous instance if any
if [ -f /tmp/dns-proxy.pid ]; then
  old_pid=$(cat /tmp/dns-proxy.pid)
  if [ -n "$old_pid" ]; then
    kill "$old_pid" 2>/dev/null || true
    sleep 1
  fi
fi

log "Starting DNS forwarder (${veth_gateway}:53 -> ${dns_upstream}:53)..."
nohup python3 -u /tmp/dns-proxy.py "$dns_upstream" "$veth_gateway" > /tmp/dns-proxy.log 2>&1 &

# Wait up to 10s for forwarder to be ready
dns_ready=false
for _ in $(seq 1 10); do
  result=$(python3 - "$veth_gateway" << 'PROBE'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.settimeout(1)
try:
    s.sendto(b'\x00\x1e\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06google\x03com\x00\x00\x01\x00\x01',
             (sys.argv[1], 53))
    data, _ = s.recvfrom(4096)
    print('ok' if data else '', end='')
except Exception:
    pass
PROBE
  )
  if [ "$result" = "ok" ]; then
    dns_ready=true
    break
  fi
  sleep 1
done
if [ "$dns_ready" != "true" ]; then
  log "WARNING: DNS forwarder not responding after 10s — verification may fail."
fi

# ---------------------------------------------------------------------------
# 5. Configure sandbox netns (iptables + resolv.conf)
# ---------------------------------------------------------------------------
iptables_bin=""

if [ -n "$sandbox_ns" ]; then
  # Back up original resolv.conf (once only)
  ip netns exec "$sandbox_ns" sh -c '[ -f /tmp/resolv.conf.orig ] || cp /etc/resolv.conf /tmp/resolv.conf.orig'

  # Find iptables binary
  for candidate in iptables /sbin/iptables /usr/sbin/iptables; do
    if ip netns exec "$sandbox_ns" sh -c "test -x \"\$(command -v ${candidate} 2>/dev/null || echo ${candidate})\"" 2>/dev/null; then
      iptables_bin="$candidate"
      break
    fi
  done

  if [ -n "$iptables_bin" ]; then
    # Allow UDP port 53 to the veth gateway (idempotent)
    if ! ip netns exec "$sandbox_ns" "$iptables_bin" -C OUTPUT -p udp -d "$veth_gateway" --dport 53 -j ACCEPT 2>/dev/null; then
      ip netns exec "$sandbox_ns" "$iptables_bin" -I OUTPUT 1 -p udp -d "$veth_gateway" --dport 53 -j ACCEPT
    fi

    # Point resolv.conf at the veth gateway (same format as buildResolvConf in TS source)
    ip netns exec "$sandbox_ns" sh -c "printf 'nameserver ${veth_gateway}\noptions ndots:5\n' > /etc/resolv.conf"
  else
    log "WARNING: iptables not found in sandbox netns (checked PATH, /sbin, /usr/sbin)."
    log "WARNING: Cannot add UDP DNS exception. Sandbox DNS resolution will not work."
    # Restore resolv.conf to avoid a broken state
    ip netns exec "$sandbox_ns" sh -c '[ -f /tmp/resolv.conf.orig ] && cp /tmp/resolv.conf.orig /etc/resolv.conf'
  fi
fi

# ---------------------------------------------------------------------------
# 6. Verify
# ---------------------------------------------------------------------------
pass=0
fail=0

pid=$(cat /tmp/dns-proxy.pid 2>/dev/null || true)
dns_log=$(cat /tmp/dns-proxy.log 2>/dev/null || true)
if [ -n "$pid" ] && echo "$dns_log" | grep -q 'dns-proxy:'; then
  log "  [PASS] DNS forwarder running (pid=${pid}): ${dns_log}"
  pass=$((pass + 1))
else
  log "  [FAIL] DNS forwarder not running. PID=${pid:-none} Log: ${dns_log:-empty}"
  fail=$((fail + 1))
fi

if [ -n "$sandbox_ns" ]; then
  resolv=$(ip netns exec "$sandbox_ns" cat /etc/resolv.conf 2>/dev/null || true)
  if echo "$resolv" | grep -q "nameserver ${veth_gateway}"; then
    log "  [PASS] resolv.conf -> nameserver ${veth_gateway}"
    pass=$((pass + 1))
  else
    log "  [FAIL] resolv.conf does not point to ${veth_gateway}: ${resolv}"
    fail=$((fail + 1))
  fi

  if [ -n "$iptables_bin" ]; then
    if ip netns exec "$sandbox_ns" "$iptables_bin" -C OUTPUT -p udp -d "$veth_gateway" --dport 53 -j ACCEPT 2>/dev/null; then
      log "  [PASS] iptables: UDP ${veth_gateway}:53 ACCEPT rule present"
      pass=$((pass + 1))
    else
      log "  [FAIL] iptables: UDP DNS ACCEPT rule missing"
      fail=$((fail + 1))
    fi
  fi

  dns_result=""
  for attempt in 1 2 3; do
    dns_result=$(ip netns exec "$sandbox_ns" getent hosts github.com 2>/dev/null || true)
    if [ -n "$dns_result" ]; then break; fi
    [ "$attempt" -lt 3 ] && sleep 2
  done
  if [ -n "$dns_result" ]; then
    log "  [PASS] getent hosts github.com -> ${dns_result}"
    pass=$((pass + 1))
  else
    log "  [FAIL] getent hosts github.com returned empty after 3 attempts"
    fail=$((fail + 1))
  fi
else
  log "  [SKIP] Sandbox namespace not found; cannot verify resolv.conf, iptables, or DNS."
fi

log "DNS verification: ${pass} passed, ${fail} failed"
if [ "$fail" -gt 0 ]; then
  log "WARNING: DNS setup incomplete. Sandbox DNS resolution may not work. See issue #626, #557."
  exit 1
fi
