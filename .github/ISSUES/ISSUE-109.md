---
title: "Build Feature Drift Detection with ADWIN and Page-Hinkley Change-Point Tests"
labels: ["difficulty: advanced", "area: ml", "type: feature"]
assignees: []
---

## Summary
The existing PSI-based drift monitor (ISSUE-030) detects gradual distribution drift but is slow to respond to sudden concept drift (abrupt market regime changes or new attack patterns). Supplementing PSI with ADWIN (ADaptive WINdowing) and Page-Hinkley tests provides online, low-latency detection of sudden distribution changes in the feature stream.

## Objectives
- [ ] Implement `ADWINDriftDetector` and `PageHinkleyDetector` in `detection/drift_detectors.py` using `river` library
- [ ] Run both detectors on the real-time feature stream in `detection/model_inference.py`
- [ ] Emit `drift.detected` event with: feature name, detector type, change-point timestamp, and magnitude
- [ ] Expose drift detector state via `GET /health/drift`
- [ ] Configure drift sensitivity via `ADWIN_DELTA` and `PAGE_HINKLEY_THRESHOLD` env vars

## Definition of Done
- [ ] ADWIN detects synthetic drift injected in test within 100 observations
- [ ] Page-Hinkley detects mean shift of 0.5 standard deviations within 200 observations
- [ ] Drift event emitted within 1 second of change-point detection
- [ ] Tests inject drift into feature stream and verify both detectors fire
