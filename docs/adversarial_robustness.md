Adversarial Robustness
======================

Threat model
-------------
- Attacker can perturb feature values of a wallet's feature vector within
  semantic constraints (immutable features, directional bounds, min/max).
- Attacker has white-box access to feature values but not necessarily model
  internals; we use transfer/approximation methods to craft attacks.

Attacks implemented
-------------------
- Finite-Difference Gradient Estimation + FGSM/PGD (model-agnostic). Pseudocode:

  1. For each feature i, estimate gradient g_i ≈ (f(x+ε e_i) - f(x)) / ε
  2. FGSM: x' = x + ε * sign(g) (projected to constraints)
  3. PGD: iterate small steps in -g direction, project to L2 ball and constraints

Constraints
-----------
- Features marked `mutable=False` are never changed.
- Directional constraints (`increase` / `decrease`) are enforced per-feature.
- Per-feature min/max bounds are respected and projection is applied.

Robustness metrics
------------------
- ASR: Attack Success Rate — fraction of true positives (label=1) flipped by PGD.
- MAP: Minimum Adversarial Perturbation found by binary search using PGD.
- Certified radius: probabilistic lower bound via randomized smoothing (Monte Carlo).

Disclaimer
----------
Certificates are probabilistic (Monte Carlo) estimates, not hard guarantees.
Results depend on sampling parameters and random seed.
