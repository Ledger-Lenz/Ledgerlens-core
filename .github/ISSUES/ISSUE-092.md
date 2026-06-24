---
title: "Implement Mutation Testing with Mutmut for Critical Detection Logic"
labels: ["difficulty: intermediate", "area: testing", "type: enhancement"]
assignees: []
---

## Summary
High unit test coverage on `detection/benford_engine.py` and `detection/graph_engine.py` does not guarantee tests are sensitive to logic errors — tests may pass even when core detection logic is mutated. Running `mutmut` on the detection modules and achieving > 80% mutation score gives confidence that the test suite would catch real bugs.

## Objectives
- [ ] Configure `mutmut` targeting `detection/benford_engine.py`, `detection/graph_engine.py`, and `detection/model_inference.py`
- [ ] Add `make mutation-test` target running `mutmut run` and `mutmut results`
- [ ] Fix surviving mutants by adding assertions that catch the mutated behaviour
- [ ] Achieve mutation score ≥ 80% across the three detection modules
- [ ] Add mutation score badge to README

## Definition of Done
- [ ] Mutation score ≥ 80% verified via `mutmut results --all`
- [ ] `make mutation-test` runs without manual intervention
- [ ] No surviving mutants in arithmetic operators or comparison operators in detection logic
