// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

import {
  __test,
  formatSandboxBridgeUnreachableMessage,
  isSandboxBridgeGatewayReachable,
} from "../../../dist/lib/onboard/gateway-sandbox-reachability";

describe("gateway sandbox reachability route modeling", () => {
  it("parses Docker network IPAM config for subnet and gateway", () => {
    expect(
      __test.parseDockerNetworkIpamConfig(
        '[{"Subnet":"fd00::/64","Gateway":"fd00::1"},{"Subnet":"172.19.0.0/16","Gateway":"172.19.0.1"}]',
      ),
    ).toEqual({ subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" });
  });

  it("mirrors OpenShell native Linux bridge host aliases", () => {
    const route = __test.buildOpenShellDockerRoute(
      "openshell-docker",
      { subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" },
      false,
    );
    expect(route?.routeKind).toBe("bridge_gateway");
    expect(route?.addHosts).toEqual([
      "host.docker.internal:172.19.0.1",
      "host.openshell.internal:172.19.0.1",
    ]);
  });

  it("mirrors OpenShell Docker Desktop and VM-backed host-gateway routing", () => {
    const route = __test.buildOpenShellDockerRoute(
      "openshell-docker",
      { subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" },
      true,
    );
    expect(route?.routeKind).toBe("host_gateway");
    expect(route?.addHosts).toEqual(["host.openshell.internal:host-gateway"]);
  });
});

describe("isSandboxBridgeGatewayReachable", () => {
  it("returns ok when the correctly-routed helper connects", async () => {
    const result = await isSandboxBridgeGatewayReachable({
      inspectNetworkImpl: () => ({ subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" }),
      usesHostGatewayRouteImpl: () => false,
      runImpl: () => ({ status: 0 }),
    });
    expect(result).toEqual({
      ok: true,
      reason: "ok",
      networkName: "openshell-docker",
      subnet: "172.19.0.0/16",
      gatewayIp: "172.19.0.1",
      routeKind: "bridge_gateway",
    });
  });

  it("uses add-host aliases before the probe image", async () => {
    const seen: { args: readonly string[] } = { args: [] };
    await isSandboxBridgeGatewayReachable({
      networkName: "custom-net",
      port: 9090,
      timeoutSec: 7,
      inspectNetworkImpl: () => ({ subnet: "10.0.0.0/24", gatewayIp: "10.0.0.1" }),
      usesHostGatewayRouteImpl: () => false,
      runImpl: (args) => {
        seen.args = args;
        return { status: 0 };
      },
    });
    expect(seen.args).toContain("custom-net");
    expect(seen.args).toContain("--pull=missing");
    expect(seen.args).toContain("host.openshell.internal:10.0.0.1");
    const addHostIndex = seen.args.findIndex((arg) =>
      arg.includes("host.openshell.internal:10.0.0.1"),
    );
    const probeCommandIndex = seen.args.findIndex((arg) =>
      arg.includes("nc -zw7 host.openshell.internal 9090"),
    );
    expect(addHostIndex).toBeGreaterThanOrEqual(0);
    expect(probeCommandIndex).toBeGreaterThanOrEqual(0);
    expect(addHostIndex).toBeLessThan(probeCommandIndex);
    expect(seen.args.join(" ")).toContain("nc -zw7 host.openshell.internal 9090");
  });

  it("does not call a missing Docker network a firewall failure", async () => {
    const result = await isSandboxBridgeGatewayReachable({
      inspectNetworkImpl: () => undefined,
      usesHostGatewayRouteImpl: () => false,
      runImpl: () => ({ status: 0 }),
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("probe_unavailable");
    expect(result.detail).toContain("not found");
  });

  it("does not call helper DNS failures firewall failures", async () => {
    const result = await isSandboxBridgeGatewayReachable({
      inspectNetworkImpl: () => ({ subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" }),
      usesHostGatewayRouteImpl: () => false,
      runImpl: () => ({ status: 1, stderr: "nc: bad address 'host.openshell.internal'" }),
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("probe_unavailable");
  });

  it("flags veth operation-not-supported as a fatal bridge failure", async () => {
    const result = await isSandboxBridgeGatewayReachable({
      inspectNetworkImpl: () => ({ subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" }),
      usesHostGatewayRouteImpl: () => false,
      runImpl: () => ({
        status: 125,
        stderr:
          "docker: Error response from daemon: failed to add the host <=> sandbox veth pair interfaces: operation not supported.",
      }),
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("veth_unsupported");
    expect(result.detail).toContain("operation not supported");
  });

  it("flags docker probe timeouts separately from inconclusive probe failures", async () => {
    const result = await isSandboxBridgeGatewayReachable({
      inspectNetworkImpl: () => ({ subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" }),
      usesHostGatewayRouteImpl: () => false,
      runImpl: () => ({
        status: null,
        signal: "SIGTERM",
        error: "spawnSync docker ETIMEDOUT",
      }),
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("probe_timeout");
    expect(result.detail).toContain("ETIMEDOUT");
  });

  it("keeps tcp_failed for BusyBox nc connection-level 'Operation timed out' stderr (UFW remediation path)", async () => {
    const result = await isSandboxBridgeGatewayReachable({
      inspectNetworkImpl: () => ({ subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" }),
      usesHostGatewayRouteImpl: () => false,
      runImpl: () => ({
        status: 1,
        stderr: "nc: host.openshell.internal (172.19.0.1:8080): Operation timed out",
      }),
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("tcp_failed");
  });

  it("downgrades a slow-registry pre-pull timeout to probe_unavailable (not fatal probe_timeout) (#3630 codex review)", async () => {
    const result = await isSandboxBridgeGatewayReachable({
      inspectNetworkImpl: () => ({ subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" }),
      usesHostGatewayRouteImpl: () => false,
      runImpl: () => ({ status: 0 }),
      ensureImageCachedOverride: {
        ok: false,
        reason: "pull_timeout",
        details: "docker pull timed out after 60s",
      },
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("probe_unavailable");
    expect(result.detail).toContain("timed out");
  });

  it("escalates inspect_unavailable to fatal docker_daemon_unreachable (#3630 codex review)", async () => {
    const result = await isSandboxBridgeGatewayReachable({
      inspectNetworkImpl: () => ({ subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" }),
      usesHostGatewayRouteImpl: () => false,
      runImpl: () => ({ status: 0 }),
      ensureImageCachedOverride: {
        ok: false,
        reason: "inspect_unavailable",
        details: "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
      },
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("docker_daemon_unreachable");
    expect(result.detail).toContain("Cannot connect to the Docker daemon");
  });

  it("flags tcp_failed only after the OpenShell route was modeled", async () => {
    const result = await isSandboxBridgeGatewayReachable({
      inspectNetworkImpl: () => ({ subnet: "172.19.0.0/16", gatewayIp: "172.19.0.1" }),
      usesHostGatewayRouteImpl: () => false,
      runImpl: () => ({ status: 1 }),
    });
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("tcp_failed");
    expect(result.gatewayIp).toBe("172.19.0.1");
  });
});

describe("formatSandboxBridgeUnreachableMessage", () => {
  it("emits a UFW command only for bridge-gateway TCP failures", () => {
    const msg = formatSandboxBridgeUnreachableMessage({
      ok: false,
      reason: "tcp_failed",
      routeKind: "bridge_gateway",
      networkName: "openshell-docker",
      subnet: "172.19.0.0/16",
      gatewayIp: "172.19.0.1",
    });
    expect(msg).toContain("172.19.0.1:8080");
    expect(msg).toContain("ufw allow from 172.19.0.0/16 to 172.19.0.1 port 8080");
  });

  it("falls back to a subnet-only UFW command when the gateway IP is unavailable", () => {
    const msg = formatSandboxBridgeUnreachableMessage({
      ok: false,
      reason: "tcp_failed",
      routeKind: "bridge_gateway",
      networkName: "openshell-docker",
      subnet: "172.19.0.0/16",
    });
    expect(msg).toContain("ufw allow from 172.19.0.0/16 to any port 8080");
  });

  it("does not emit a UFW command when the probe is unavailable", () => {
    const msg = formatSandboxBridgeUnreachableMessage({
      ok: false,
      reason: "probe_unavailable",
      detail: "Docker network not found",
    });
    expect(msg).toContain("Could not verify sandbox bridge reachability");
    expect(msg).toContain("continuing");
    expect(msg).not.toContain("ufw allow");
  });

  it("emits a fatal veth message without treating it as inconclusive", () => {
    const msg = formatSandboxBridgeUnreachableMessage({
      ok: false,
      reason: "veth_unsupported",
      detail:
        "docker: Error response from daemon: failed to add the host <=> sandbox veth pair interfaces: operation not supported.",
    });
    expect(msg).toContain("could not create the sandbox bridge veth pair");
    expect(msg).toContain("operation not supported");
    expect(msg).not.toContain("continuing");
  });

  it("emits a fatal timeout message without treating it as inconclusive", () => {
    const msg = formatSandboxBridgeUnreachableMessage({
      ok: false,
      reason: "probe_timeout",
      detail: "spawnSync docker ETIMEDOUT",
    });
    expect(msg).toContain("probe timed out");
    expect(msg).toContain("ETIMEDOUT");
    expect(msg).not.toContain("continuing");
  });

  it("emits a fatal docker_daemon_unreachable message with daemon restart hint", () => {
    const msg = formatSandboxBridgeUnreachableMessage({
      ok: false,
      reason: "docker_daemon_unreachable",
      detail: "Cannot connect to the Docker daemon at unix:///var/run/docker.sock",
    });
    expect(msg).toContain("Docker daemon is not reachable");
    expect(msg).toContain("Cannot connect to the Docker daemon");
    expect(msg).toMatch(/Restart the Docker daemon|systemctl restart docker|Docker Desktop/);
    expect(msg).not.toContain("continuing");
  });

  it("does not emit a UFW command for host-gateway routing failures", () => {
    const msg = formatSandboxBridgeUnreachableMessage({
      ok: false,
      reason: "tcp_failed",
      routeKind: "host_gateway",
      networkName: "openshell-docker",
      subnet: "172.19.0.0/16",
    });
    expect(msg).toContain("host-gateway");
    expect(msg).not.toContain("ufw allow");
  });
});
