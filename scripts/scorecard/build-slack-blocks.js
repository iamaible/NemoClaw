// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Pure builder for the nightly scorecard Slack Block Kit payload.
 *
 * Consumed by the `Post scorecard to Slack` step in
 * `.github/workflows/nightly-e2e.yaml`. Kept as a plain CommonJS module so the
 * GitHub Actions `actions/github-script` step can `require()` it directly and
 * Vitest can exercise it as a unit (see `test/scorecard-blocks.test.ts`).
 *
 * Input `data` mirrors the `scorecardData` step output emitted by the
 * scorecard generation step. See that step for the field contract.
 */

/**
 * @typedef {Object} ScorecardData
 * @property {string} today             Display date, e.g. "May 25".
 * @property {string} runMode           "Scheduled full nightly" | "Manual full run" | "Selective dispatch".
 * @property {boolean} isSelectiveDispatch
 * @property {string[]} requestedJobs   Populated only when isSelectiveDispatch is true.
 * @property {number} total             Total jobs considered (excludes meta jobs).
 * @property {number} ran               total - skipped.
 * @property {number} success
 * @property {number} failure
 * @property {number} cancelled
 * @property {number} skipped
 * @property {boolean} perfect          ran > 0 && failure === 0 && cancelled === 0.
 * @property {string[]} failedJobs      Sorted list of failed job names.
 * @property {string} trendLine         Pre-rendered trend line, prefixed with "Trend: ".
 * @property {string} runUrl            Direct link to the current run.
 */

/**
 * @param {ScorecardData} data
 * @returns {Array<object>} Slack Block Kit blocks
 */
function buildBlocks(data) {
  const blocks = [];

  // ── 1. Header ───────────────────────────────────────────────────────────
  blocks.push({
    type: "header",
    text: {
      type: "plain_text",
      text: `🌅 NemoClaw Nightly Scorecard — ${data.today}`,
      emoji: true,
    },
  });

  // ── 2. Run mode (+ requested jobs when selective) ───────────────────────
  const contextElements = [{ type: "mrkdwn", text: `*Run mode:* ${data.runMode}` }];
  if (data.isSelectiveDispatch && data.requestedJobs.length > 0) {
    const jobList = data.requestedJobs.map((name) => `\`${name}\``).join(", ");
    contextElements.push({ type: "mrkdwn", text: `*Requested:* ${jobList}` });
  }
  blocks.push({ type: "context", elements: contextElements });

  // ── 3. Counts grid ──────────────────────────────────────────────────────
  const statusLabel = data.perfect
    ? ":white_check_mark: All passed"
    : data.failure > 0
      ? `:x: ${data.failure} failed`
      : ":no_entry_sign: Incomplete";

  blocks.push({
    type: "section",
    fields: [
      { type: "mrkdwn", text: `*Jobs run*\n${data.ran} of ${data.total}` },
      { type: "mrkdwn", text: `*Status*\n${statusLabel}` },
      { type: "mrkdwn", text: `:white_check_mark: *Passed*\n${data.success}` },
      { type: "mrkdwn", text: `:x: *Failed*\n${data.failure}` },
      { type: "mrkdwn", text: `:no_entry_sign: *Cancelled*\n${data.cancelled}` },
      { type: "mrkdwn", text: `:fast_forward: *Skipped*\n${data.skipped}` },
    ],
  });

  // ── 4. Perfect banner OR failed-jobs list ───────────────────────────────
  if (data.perfect) {
    blocks.push({
      type: "section",
      text: { type: "mrkdwn", text: ":tada: *All jobs passed!*" },
    });
  } else if (data.failedJobs.length > 0) {
    const list = data.failedJobs.map((name) => `• \`${name}\``).join("\n");
    blocks.push({
      type: "section",
      text: {
        type: "mrkdwn",
        text: `*Failed jobs (${data.failedJobs.length}):*\n${list}`,
      },
    });
  }

  // ── 5. Divider + trend ──────────────────────────────────────────────────
  blocks.push({ type: "divider" });
  blocks.push({
    type: "context",
    elements: [
      {
        type: "mrkdwn",
        text: data.trendLine.replace(/^Trend:\s*/, "*Trend:* "),
      },
    ],
  });

  // ── 6. Action buttons ───────────────────────────────────────────────────
  const workflowUrl = data.runUrl.replace(/\/runs\/\d+$/, "/workflows/nightly-e2e.yaml");
  blocks.push({
    type: "actions",
    elements: [
      {
        type: "button",
        text: { type: "plain_text", text: "View this run", emoji: true },
        url: data.runUrl,
        style: data.perfect ? "primary" : "danger",
      },
      {
        type: "button",
        text: { type: "plain_text", text: "All nightly-e2e runs", emoji: true },
        url: workflowUrl,
      },
    ],
  });

  return blocks;
}

/**
 * Short fallback text shown in Slack notification previews and to screen
 * readers when blocks cannot be rendered. Required by Slack — missing `text`
 * triggers a warning in the API response.
 *
 * @param {ScorecardData} data
 * @returns {string}
 */
function buildFallbackText(data) {
  const status = data.perfect
    ? "all passed"
    : data.failure > 0
      ? `${data.failure} failed`
      : "incomplete";
  return `NemoClaw Nightly Scorecard — ${data.today}: ${status}`;
}

module.exports = { buildBlocks, buildFallbackText };
