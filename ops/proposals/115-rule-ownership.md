# Proposal: #115 S-25 — every §8 rule is claimed by exactly one §4 module

**Kind:** spec + `check_docs`. 23 of 38 rules have no §4 owner. 8, 22, 34, 37
are also missing from `make_tasks` `M` lists. Individual issues already
cover 6, 8, 9b, 11–13, 18, 20, 24–27, 33.

**Should say:** like §7.1 events — one owner in the table/§4. Check: parse
ordinals vs `rule N` in §4 vs `M` lists.

**Do not:** invent owners that contradict existing §4 (S-01, S-04).

**Modules:** spec + `check_docs` after allowlisting today's gaps.
