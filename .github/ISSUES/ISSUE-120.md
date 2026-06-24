---
title: "Build Analyst Review Dashboard API with Score Explanation and Feedback Capture"
labels: ["difficulty: advanced", "area: api", "type: feature"]
assignees: []
---

## Summary
Compliance analysts reviewing flagged wallets currently use raw API responses to investigate cases. A dedicated analyst review API — providing a combined view of risk score, SHAP explanation, trade timeline, ring membership, and analyst feedback capture — powers an analyst workflow UI and enables the active learning loop (ISSUE-052) to ingest human labels.

## Objectives
- [ ] `GET /analyst/wallet/{wallet}` returns: current risk score, SHAP top-10, trade timeline (last 30 days), ring membership, historical score trend, open alert events
- [ ] `POST /analyst/wallet/{wallet}/feedback` captures analyst verdict: `{verdict: "confirmed_wash|false_positive|needs_review", notes: str}`
- [ ] Feedback stored in `analyst_feedback` table and fed to the active learning loop
- [ ] `GET /analyst/queue` returns the top 20 wallets awaiting analyst review, sorted by score
- [ ] `GET /analyst/stats` returns: cases reviewed today, false positive rate (last 30 days), average review time

## Definition of Done
- [ ] `GET /analyst/wallet/{wallet}` response includes all six data sections
- [ ] Feedback submitted via POST appears in `GET /analyst/queue` as reviewed
- [ ] Active learning loop (ISSUE-052) can consume feedback records via `GET /analyst/feedback?since=ISO_TIMESTAMP`
- [ ] Tests cover: empty queue, queue ordering, feedback submission, stats computation
