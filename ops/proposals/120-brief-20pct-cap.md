# Proposal: #120 S-29 — brief §21 still says 20% position cap

**Kind:** brief (senior). §12: 10% entry, total exposure 100%, per-position
cap **replaced**. §21 table still “Maximum position size | 20%” **and** a
10% row two lines below. AGENTS: brief wins — a stale 20% outranks the spec.

**Should say:** delete the 20% row; keep 10% entry + 100% total. Related
S-21 (slippage can still bind exposure).

**Do not:** restore `MAX_POSITION_PCT` in `config` because §21 still says 20.

**Modules:** `business-brief.md` §21 (human). Then spec if anything still
cites 20%.
