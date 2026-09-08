# Proposal: #118 S-28 — mechanical gates (do not enable all at once)

The audit's hand checks, as `check_docs.py` extras, each with an **allowlist
of today's known gaps** (or `make check` goes red on 23 unclaimed rules).

1. Signature diff §4 vs `interfaces.md` (+ require `→` / typed params).
2. §8 one owner, fail two incompatible remedies.
3. §5 sole-owner sentence vs SQL writers.
4. §4 headings vs `dependency-order` vs `M` (S-06 is the instance).
5. §3.2 heading reachable from `M`; `None` key iff no block (S-23, S-24).
6. §7.1 event literal in the owning module's `extra=`.
7. AGENTS “1–29” vs highest ordinal; file tree vs `*.py`; coverage sentence.
8. Version header vs cited `vN.NN` (exists; see #138 for brief/dep-order).

**New failure class:** amendment fixed §4 and left the §8 rule. Gate 2.

**Do not:** turn on gate 2/3/6 without allowlists — they will fail today's
tree. Land gates one PR at a time.
