# Intake conformance corpus

Pins the receive-path behavior of the autonomy gate's intake verification
(`intake_verify`) across the `signing.verify_mode` matrix: four failure
classes — unsigned / signed-INVALID / unknown-kid / revoked — under each of
`off` / `warn` / `enforce`, plus one signed-verified positive control under
`enforce`.

The message and pin artifacts are deliberately **reused from the signing
conformance corpus** (`../signing/messages/`, `../signing/pins/`) — the wire
vectors are pinned once, there; this corpus pins what a receiver *does* with
each verification outcome at intake:

| mode | failure classes | verified |
|---|---|---|
| `off` | proceed, no verification | proceed |
| `warn` | proceed, annotate (identity recorded, no authority) | proceed |
| `enforce` | **reject**: quarantine evidence copy to `dead_letter/` (mode 600, no-clobber, original untouched), do not evaluate, exit 3 | proceed |

Each `expected/<case>__<mode>.yaml` golden pins the intake action, the gate
exit code, the annotation label, and whether a quarantine copy was written.
`tests/test_intake_conformance.py` executes the real gate CLI against a
scratch receiver workspace per golden.

A change here is a receive-path contract change and needs a ruling, exactly
like the signing corpus (see `../signing/README.md`).
