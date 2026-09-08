# Proposal: #138 version gate for all three documents, without pooling them

**Kind:** CI / docs. `scripts/ci/check_docs.py` check 6. Not a trading module.

**Wrong:** the comment quotes §"Versioning" (new version when a contract/schema/
rule/test contract changes). The check only compares the **spec header** to
**cited `vN.NN` markers in the spec body**. An amendment with no marker is
invisible. Header `1.99` with no body citation passes. `business-brief.md`
(senior) and `dependency-order.md` are unchecked.

**Must keep:** the `re.sub` that strips brief citations from the spec scan. A
brief bump must not force a spec header bump.

**Should say / do:**

1. Extract header-vs-own-citations into a helper. Run it on
   `technical-spec.md`, `business-brief.md`, and `dependency-order.md`, each
   scanning only its own markers (no cross-document pooling).
2. Document that this is a **proxy** (citations present and header ≥ max
   citation), not a proof that every contract edit bumped the header.
3. Decide separately whether a `git diff` gate against the previous commit
   belongs next to `tasks/` drift. That is a different check; do not pretend
   check 6 is that check.

**Do not:** fail when the header is *newer* than any citation. Do not parse
the brief's version out of the spec as if it were a spec amendment.

**Test / CI:** a fixture spec whose body cites `v1.70` and whose header is
`1.69` fails; the same with header `1.70` passes; a spec that only cites
`brief v1.71` with header `1.70` still passes.

**Modules to re-run:** none in `zarabot/`. `scripts/ci/check_docs.py` only.

**Stop:** do not add a `git diff` versioner in the same change as the helper
until that gate is specified (what counts as a contract change).
