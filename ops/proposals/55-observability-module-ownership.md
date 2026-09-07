# Proposal: #55 remaining §7.1 events still have no owning module heading

**Status:** spec v1.61 on this branch. Delete this file when v1.61 is on `main`.

Each catalog event now has a sentence in its owning §4 heading plus a §3.2 case.
`event` is producer-set (v1.60). Rotation is five×10 MB. `config_invalid` is
emitted by `app.startup` after a last-resort `configure`. `signal_*` is
`app.loops`; the gate stays pure.
