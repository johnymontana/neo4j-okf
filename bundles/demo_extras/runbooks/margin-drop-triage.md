---
type: Playbook
title: "Triage: sudden gross-margin drop"
description: First-response steps when the daily margin dashboard drops more than 2 points day-over-day.
tags: [oncall, finance, margin]
generated: { by: runbook_agent/gemini-2.5-pro, at: 2026-07-20T08:00:00Z }
verified:
  - { by: process:nightly-runbook-check, at: 2026-08-03T02:00:00Z }
status: stable
stale_after: 2026-10-01
---

# Trigger

The margin dashboard alerts when day-over-day gross margin drops > 2 points.
Confirm against the [daily margin dashboard](/dashboards/margin-daily.md)
before paging anyone.

# Steps

1. Check for an FX rate gap first (most false alarms).
2. If real, follow the [warehouse failover runbook](/runbooks/failover.md).
3. Escalate per the [escalation matrix](/references/escalation-matrix.md).
