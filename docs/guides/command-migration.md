# Command namespaces in 0.5.3

OACP 0.5.3 groups related operations under `agent`, `trust`, and `autonomy`.
Update callers before upgrading; the previous root commands now return an
unknown-command error with exit code 2 and have no aliases.

| Previous command | Replacement |
| --- | --- |
| `oacp add-agent <project> <agent> ...` | `oacp agent add <project> <agent> ...` |
| `oacp key gen ...` | `oacp trust keygen ...` |
| `oacp key list ...` | `oacp trust keys ...` |
| `oacp autonomy-outcome <audit> ...` | `oacp autonomy outcome <audit> ...` |
| `oacp autonomy-finalize <audit> ...` | `oacp autonomy finalize <audit> ...` |

Arguments, output, and operation exit codes stay with the existing
implementations. The global agent registry and `agent init|sync|show|list`
remain available. `trust keygen` and `trust keys` manage local keys without
requiring a project; `trust list` continues to list project catalog entries
and receiver pins.

For `agent` and `trust`, `--oacp-dir <path>` or `--oacp-dir=<path>` can appear
before or after the subcommand; the last occurrence wins. Leaf help works as
`oacp agent add --help` or `oacp help agent add` (likewise for `trust keygen`
and `autonomy finalize`).

Stored audit receipts retain the immutable
`result.finalizer.name: oacp autonomy-finalize` discriminator. It identifies
the writer format and is independent of the current command spelling.

The separate [memory command migration](memory-cli-migration.md) covers the
removed memory, event-writing, and retention shims. The root command list now
contains 13 entries, including `session-init` and `setup`.

## Workflow validators

The bundled `scripts/check_quality_gate.py`, `make quality` target, and
`templates/github_actions_quality_gate.yaml` have been removed. Configure the owning review
workflow's versioned validator in skills or CI before upgrading copied
callers. The workflow owns its findings-status policy and must return a typed
failure before side effects when validation fails.

Handoff body schemas likewise belong to their owning workflow. The kernel no
longer emits body-schema advisories; required envelope fields, body type and
size limits, signing, and admission checks keep their existing behavior. See
the [validation boundary](../protocol/inbox_outbox.md#validation-boundary).

## Audit directory diagnostics

The autonomy evaluator warns on stderr when `--audit-dir` does not end in
`autonomy_decisions`. The supplied location remains valid and the decision and
JSON output are unchanged. Workspace tools continue scanning the canonical
`agents/<receiver>/audit/autonomy_decisions/` directory.

For custom evaluator invocations, pass the same path to
`oacp doctor --audit-dir <path>` to include a WARN-level Audit finding. This
option diagnoses the supplied basename; it does not redirect workspace scans
or introduce a receiver configuration setting.


## Packet and handoff templates

The packet initializer (`scripts/init_packet.sh` and `make packet`), review
packet template, merge-decision template and handoff packet template are retired.
Use formats and generators owned by your review or handoff workflow. The public
v0.5.2 tag preserves the former artifacts for historical reference; upgrading
does not migrate or delete existing runtime packets.

The findings template and review-loop documentation remain available. Envelope
validation, signing, admission, and message types are unchanged. Update source
checkout callers before upgrading; there is no replacement kernel packet verb.
