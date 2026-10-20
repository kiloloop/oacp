# Streaming message verification

`oacp verify --batch --project <project> --receiver <receiver> --oacp-dir <home>`
reads one message path per stdin line. Each output line is the existing JSON
verification object plus `path`, emitted in input order and flushed immediately.
`--json` is optional in batch mode. Paths containing spaces are preserved; newline
characters cannot occur within a path. The final input line need not end in a newline.

One invocation loads one receiver pin-policy snapshot and uses it for the entire
stream. Start a new invocation after changing trust policy. Verification retains
the existing bounded-byte, signature, sender-binding and payload-digest rules;
batch output supplies annotations, not intake authority.

Exit status is zero when the complete stream was processed, even if individual
verdicts are invalid, untrusted, revoked or unsupported. Inspect each `status`.
Unreadable or non-regular files (including directories and FIFOs) and empty
input lines produce `invalid` with `read_error: true`, a null payload digest, and
a read-error reason. The additive `read_error` field distinguishes these rows
from invalid message content; other rows omit it. Invalid arguments or stream I/O/decoding failures
exit nonzero; output emitted before a stream failure remains valid partial output.
An empty stream succeeds without output.

Batch mode cannot combine with a positional message file, `--quarantine` or
`--attach-audit`. Those effects retain their single-message interface. Existing
single-message output and exit codes are unchanged.
