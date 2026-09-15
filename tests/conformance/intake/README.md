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

## Envelope and skill-body boundary (0.5.2)

`body_schema.yaml` supplies 32 cases, run for each of `handoff` and
`handoff_complete` under off/warn/enforce. The six goldens in
`expected/body_schema/` pin the validation errors, advisory presence and gate
outcome (192 rows). The original five signing classes / 13 mode goldens above
remain unchanged.

The harness creates a scratch sender key and receiver pin, signs the exact
case bytes, executes both `oacp validate`'s CLI entry point and the real gate
entry point, and reads the persisted audit back. No live keys or policy are
used. It asserts that `skill_owned_body_schema` findings carry `code`,
`severity: advisory`, and `detail`, remain outside `reason_codes`, and reach
`logged_notes` unchanged. Well-formed body controls record no advisory;
malformed body schemas advise. The skill-owned body checks leave the validator
in 0.5.3.

| Class | Well-formed | Malformed | Absent |
|---|---|---|---|
| Required envelope / body | Pass | Existing error (nested and oversized included) | Existing error, including empty body |
| Optional envelope | Existing scalar/numeric/type rules pass | Existing error (nested/unknown/telemetry cases included) | Pass |
| Signing trailer | Trusted signed control passes | Structure errors remain errors; enforce rejects before body parsing | Off/warn proceed; enforce rejects unsigned |
| Thread fields | Pass | Existing conversation/parent errors | Pass without grant authority |
| Expiry | Future passes | Bad format/calendar errors; past pauses admission | Pass |
| Gate-read profile/grant blocks | Existing policy applies | Existing profile/shape pause | Profile exemption passes; required profile pauses |
| Skill schema | No advisory | Advisory only | Nonempty body missing skill fields advises |

Combined-invalid cases pair the body advisory with invalid expiry, task
profile, or signing input: an advisory cannot mask the existing rejection or
pause. An always-pause control retains its early policy pause without running body
validation or recording a gate advisory. `--quiet` suppresses only success output, not advisories.

Schema validation and admission are distinct. Malformed `task_profile` and
`continuation_grants` mapping shapes pause the gate with
`task_profile_unparsable`; they do not acquire new standalone-validator
errors. The existing `handoff_complete` voluntary-profile control pauses with
`continuation_grant_type_not_granted`. Similarly, exact auth-trailer framing
is checked by standalone validation and enforce verification; warn remains
annotation-only and does not gain a new framing rejection at the gate.
The matrix preserves these existing boundaries.
