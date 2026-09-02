# Audit-record integrity fixtures

Sanitized reproductions of the terminal-record failure shapes observed in
receiver audit corpora: off-enum `completion_kind` / `final_state`
vocabulary, duplicate live evaluations of one logical message, terminal
records still carrying a paused checkpoint action, duplicated YAML keys,
`breached: true` with an empty field list, fragmented realized-axis
spellings, a `breach_basis` label contradicting its breach source
(`declared_intent` over a realized axis and a realized-true effect),
a checkpoint clear recorded over the admission outcome
(`admission_outcome_replaced_by_clear`),
and a supersession chain hijacked by an unrelated-identity
record (`superseded_unrelated_successor`) — plus valid shapes
(`clean_terminal_done`, `final_state_superseded_valid`) that must stay
finding-free.

`expected_findings.yaml` pins the exact finding-code multiset per record.
The executable runner is `tests/test_audit_record_conformance.py`, which
copies this directory into a scratch audit dir and compares
`finalize_autonomy_record.sweep_audit_dir` output against the pins.
