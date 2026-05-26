// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";

// eslint-disable-next-line @typescript-eslint/no-require-imports
const {
  buildBlocks,
  buildFallbackText,
} = require("../scripts/scorecard/build-slack-blocks.js");

type ScorecardData = {
  today: string;
  runMode: string;
  isSelectiveDispatch: boolean;
  requestedJobs: string[];
  total: number;
  ran: number;
  success: number;
  failure: number;
  cancelled: number;
  skipped: number;
  perfect: boolean;
  failedJobs: string[];
  trendLine: string;
  runUrl: string;
};

function makeData(overrides: Partial<ScorecardData> = {}): ScorecardData {
  return {
    today: "May 25",
    runMode: "Scheduled full nightly",
    isSelectiveDispatch: false,
    requestedJobs: [],
    total: 51,
    ran: 50,
    success: 50,
    failure: 0,
    cancelled: 0,
    skipped: 1,
    perfect: true,
    failedJobs: [],
    trendLine:
      "Trend: ↗️ Improving (yesterday had failures → today perfect)",
    runUrl: "https://github.com/NVIDIA/NemoClaw/actions/runs/12345678",
    ...overrides,
  };
}

describe("buildBlocks — perfect scheduled run", () => {
  const blocks = buildBlocks(makeData());

  it("starts with a header containing the date", () => {
    expect(blocks[0]).toMatchObject({
      type: "header",
      text: { type: "plain_text" },
    });
    expect(blocks[0].text.text).toContain("May 25");
  });

  it("renders run mode in a single context line (no requested jobs)", () => {
    expect(blocks[1].type).toBe("context");
    expect(blocks[1].elements).toHaveLength(1);
    expect(blocks[1].elements[0].text).toContain("Scheduled full nightly");
  });

  it("includes the perfect-run banner instead of a failed-jobs list", () => {
    const texts = blocks
      .filter((b: { type: string }) => b.type === "section")
      .flatMap((b: { text?: { text: string } }) => (b.text ? [b.text.text] : []));
    expect(texts.join("\n")).toContain("All jobs passed");
    expect(JSON.stringify(blocks)).not.toContain("Failed jobs");
  });

  it("uses primary style for the 'View this run' button on perfect runs", () => {
    const actions = blocks.find((b: { type: string }) => b.type === "actions");
    expect(actions.elements[0].style).toBe("primary");
    expect(actions.elements[0].url).toBe(
      "https://github.com/NVIDIA/NemoClaw/actions/runs/12345678",
    );
  });

  it("links the second button to the workflow file (derived from runUrl)", () => {
    const actions = blocks.find((b: { type: string }) => b.type === "actions");
    expect(actions.elements[1].url).toBe(
      "https://github.com/NVIDIA/NemoClaw/actions/workflows/nightly-e2e.yaml",
    );
  });

  it("strips the 'Trend: ' prefix and re-bolds it", () => {
    const trendCtx = blocks
      .filter((b: { type: string }) => b.type === "context")
      .pop();
    expect(trendCtx.elements[0].text).toMatch(/^\*Trend:\*/);
    expect(trendCtx.elements[0].text).not.toMatch(/^Trend: /);
  });
});

describe("buildBlocks — run with failures", () => {
  const blocks = buildBlocks(
    makeData({
      success: 47,
      failure: 3,
      perfect: false,
      failedJobs: [
        "cloud-e2e",
        "issue-2478-crash-loop-recovery-e2e",
        "sandbox-operations-e2e",
      ],
      trendLine:
        "Trend: ↘️ Degrading (yesterday perfect → today has failures)",
    }),
  );

  it("renders a failed-jobs section listing each job", () => {
    const failedSection = blocks.find(
      (b: { type: string; text?: { text: string } }) =>
        b.type === "section" && b.text?.text?.includes("Failed jobs"),
    );
    expect(failedSection).toBeDefined();
    expect(failedSection.text.text).toContain("Failed jobs (3)");
    expect(failedSection.text.text).toContain("`cloud-e2e`");
    expect(failedSection.text.text).toContain("`sandbox-operations-e2e`");
  });

  it("does not include the perfect-run banner", () => {
    expect(JSON.stringify(blocks)).not.toContain("All jobs passed");
  });

  it("uses danger style for the 'View this run' button", () => {
    const actions = blocks.find((b: { type: string }) => b.type === "actions");
    expect(actions.elements[0].style).toBe("danger");
  });

  it("shows the failure count in the status field", () => {
    const grid = blocks.find(
      (b: { type: string; fields?: unknown[] }) =>
        b.type === "section" && Array.isArray(b.fields),
    );
    const statusField = grid.fields.find((f: { text: string }) =>
      f.text.startsWith("*Status*"),
    );
    expect(statusField.text).toContain("3 failed");
  });
});

describe("buildBlocks — selective dispatch", () => {
  const blocks = buildBlocks(
    makeData({
      runMode: "Selective dispatch",
      isSelectiveDispatch: true,
      requestedJobs: ["cloud-e2e", "hermes-slack-e2e"],
      total: 2,
      ran: 2,
      success: 2,
      skipped: 0,
      trendLine: "Trend: ⊘ Not shown for selective dispatches",
    }),
  );

  it("adds a second context element listing the requested jobs", () => {
    expect(blocks[1].elements).toHaveLength(2);
    expect(blocks[1].elements[1].text).toContain("`cloud-e2e`");
    expect(blocks[1].elements[1].text).toContain("`hermes-slack-e2e`");
  });

  it("keeps the 'not shown' trend text from the generator", () => {
    const trendCtx = blocks
      .filter((b: { type: string }) => b.type === "context")
      .pop();
    expect(trendCtx.elements[0].text).toContain("Not shown");
  });
});

describe("buildFallbackText", () => {
  it("summarises a perfect run as 'all passed'", () => {
    expect(buildFallbackText(makeData())).toBe(
      "NemoClaw Nightly Scorecard — May 25: all passed",
    );
  });

  it("summarises failures with the count", () => {
    expect(
      buildFallbackText(makeData({ perfect: false, failure: 3 })),
    ).toContain("3 failed");
  });

  it("falls back to 'incomplete' when no failures but not perfect (e.g. cancelled only)", () => {
    expect(
      buildFallbackText(
        makeData({ perfect: false, failure: 0, cancelled: 2 }),
      ),
    ).toContain("incomplete");
  });
});
