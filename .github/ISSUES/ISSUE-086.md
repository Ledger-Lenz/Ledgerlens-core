---
title: "Implement Docker Multi-Stage Build with Non-Root User and Minimal Attack Surface"
labels: ["difficulty: intermediate", "area: devops", "type: enhancement"]
assignees: []
---

## Summary
The existing `Dockerfile` is a single-stage build that runs as root and includes build tools in the final image, increasing attack surface and image size. A multi-stage build — builder stage installs dependencies, final stage copies only the runtime artifacts — reduces the image size by ~60% and eliminates root execution.

## Objectives
- [ ] Rewrite `Dockerfile` with `builder` stage (Python + build deps) and `runtime` stage (Python slim)
- [ ] Create non-root user `ledgerlens` (UID 1000) in the final stage; run the process as that user
- [ ] Use `COPY --chown` to transfer app files with correct ownership
- [ ] Reduce final image size to < 500MB (from current ~1.2GB)
- [ ] Add `.dockerignore` excluding `.git`, `tests/`, `*.md`, and dev config files
- [ ] Publish to GitHub Container Registry via existing CI workflow

## Definition of Done
- [ ] Final image runs all API tests successfully
- [ ] `docker inspect` shows process user is `ledgerlens`, not `root`
- [ ] Image size verified < 500MB via `docker image ls`
- [ ] No build tools (`gcc`, `make`) present in final image layer
