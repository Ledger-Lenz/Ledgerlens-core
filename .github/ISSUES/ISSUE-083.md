---
title: "Add CSV and Parquet Export Endpoints for Historical Risk Score Data"
labels: ["difficulty: intermediate", "area: api", "type: feature"]
assignees: []
---

## Summary
Compliance analysts and data scientists need to export historical risk score data for offline analysis, regulatory reporting, and model evaluation. Adding `GET /export/scores.csv` and `GET /export/scores.parquet` endpoints with date-range and wallet filters enables self-service data extraction without direct database access.

## Objectives
- [ ] `GET /export/scores.csv?from=YYYY-MM-DD&to=YYYY-MM-DD&min_score=N` streams CSV response
- [ ] `GET /export/scores.parquet` streams Parquet response (columnar, compressed with snappy)
- [ ] Both endpoints require admin key
- [ ] Max export window: 90 days; return 400 for wider ranges
- [ ] Streaming response to avoid memory issues for large exports (use `StreamingResponse`)
- [ ] Add `Content-Disposition: attachment` header with auto-generated filename

## Definition of Done
- [ ] 1M-row export completes without OOM on 2GB RAM server
- [ ] Parquet file readable by pandas and DuckDB
- [ ] Rate limit: 10 exports/hour per admin key
- [ ] Tests validate CSV headers, date filter correctness, and streaming for large result sets
