#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Kiloloop
# SPDX-License-Identifier: Apache-2.0
"""trust_root.py — OACP trust root: project catalog + receiver pins.

Implements the trust-authority half of the v0.4.0 message-signing design:

- **Receiver pins are the only authority.** Verification consults the
  receiver's own ``agents/<receiver>/trust/allowed_signers.yaml`` and
  nothing else; ``message_verify.load_allowed_signers`` stays the single
  swap point. Nothing in this module runs at verify time.
- **The project catalog is zero-authority distribution.**
  ``projects/<project>/trust/catalog.yaml`` records signer identities so
  receivers have a local place to import from; being cataloged grants
  nothing. A message from a cataloged-but-unpinned key still annotates
  ``signed-unknown-kid``.
- **Format v1 reserves ``domain:`` / ``instance:`` from day one.** Both
  files carry the columns; v0.4.0 validates shape only and implements no
  semantics.
- **Import is integrity-checked.** ``oacp trust import`` refuses a stub
  whose ``kid`` is not the RFC 7638 thumbprint of its ``jwk``, refuses any
  private-key component, and never silently replaces an entry that differs
  under the same ``kid``. A ``revoked`` pin is a receiver decision — import
  never reactivates one.

Trust files are CLI-managed: writers re-emit the whole file in canonical
form (comments are not preserved). Requires only PyYAML — integrity checks
use the RFC 7638 thumbprint (pure hashlib), so import and drift detection
work without the optional ``[crypto]`` extra.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(_scripts_dir))

from _oacp_constants import AGENT_RE, locked_audit  # noqa: E402
from message_signing import (  # noqa: E402
    AGENT_URN_PREFIX,
    INSTANCE_URN_PREFIX,
    AuthFormatError,
    jwk_thumbprint,
    validate_agent_urn,
    validate_instance_urn,
    validate_kid,
    validate_public_ed25519_jwk,
)
from message_verify import (  # noqa: E402
    ALLOWED_SIGNERS_RELPATH,
    PIN_STATUS_ACTIVE,
    PIN_STATUS_REVOKED,
    TRUST_FILE_VERSION,
    TrustRootError,
    load_allowed_signers,
)

CATALOG_RELPATH = Path("trust") / "catalog.yaml"

DRIFT_OK = "ok"
DRIFT_WARN = "warn"
DRIFT_ERROR = "error"


class TrustImportError(ValueError):
    """Raised when a public stub cannot be safely imported."""


def _check_reserved_column(value: Any, *, label: str, context: str) -> Optional[str]:
    """Shape-check a reserved ``domain``/``instance`` column (no semantics).

    v0.4.0 accepts a missing column, ``null``, or a non-empty string, and
    rejects everything else so format v1 files stay forward-compatible with
    the v0.4.x semantics that will eventually consume these columns.
    """
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise TrustRootError(
        f"{context}: reserved column {label!r} must be a non-empty string or null"
    )


def _check_public_jwk(jwk: Any, *, context: str) -> Dict[str, str]:
    """Validate a strict public-only Ed25519 JWK (shared locked profile).

    Delegates to the shared validator so pins, catalog, stubs, and the
    verify boundary all enforce exactly the same shape — including the
    32-byte decode of ``x`` (the thumbprint alone hashes the string
    without decoding it) and the refusal of any private component.
    """
    try:
        return validate_public_ed25519_jwk(jwk)
    except AuthFormatError as exc:
        raise TrustRootError(f"{context}: {exc}") from exc


def _check_entry_integrity(kid: str, jwk: Dict[str, str], *, context: str) -> None:
    computed = jwk_thumbprint(jwk)
    if computed != kid:
        raise TrustRootError(
            f"{context}: kid {kid!r} is not the RFC 7638 thumbprint of its "
            f"jwk (computed {computed!r})"
        )


def load_catalog(catalog_path: Path) -> Dict[str, Dict[str, Any]]:
    """Load the project's zero-authority catalog as a ``kid -> entry`` map.

    Same strictness family as ``message_verify.load_allowed_signers``: a
    missing file is an empty catalog; a present-but-unusable file raises
    ``TrustRootError``. Catalog entries carry no ``status`` — the catalog
    records identity and grants nothing.
    """
    catalog_path = Path(catalog_path)
    if not catalog_path.is_file():
        return {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TrustRootError(f"cannot read catalog {catalog_path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise TrustRootError(f"catalog {catalog_path} must be a YAML mapping")
    version = loaded.get("version")
    if version not in (TRUST_FILE_VERSION, str(TRUST_FILE_VERSION)):
        raise TrustRootError(
            f"catalog {catalog_path} has unsupported version {version!r}"
        )
    entries = loaded.get("entries")
    if entries is None:
        return {}
    if not isinstance(entries, list):
        raise TrustRootError(f"catalog {catalog_path}: entries must be a list")

    catalog: Dict[str, Dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        context = f"catalog {catalog_path}: entries[{index}]"
        if not isinstance(entry, dict):
            raise TrustRootError(f"{context} must be a mapping")
        kid = entry.get("kid")
        agent = entry.get("agent")
        if not isinstance(kid, str) or not kid:
            raise TrustRootError(f"{context} missing kid")
        try:
            validate_kid(kid)
        except AuthFormatError as exc:
            raise TrustRootError(f"{context}: {exc}") from exc
        if not isinstance(agent, str) or not AGENT_RE.fullmatch(agent):
            raise TrustRootError(
                f"{context}: agent must match {AGENT_RE.pattern}"
            )
        jwk = _check_public_jwk(entry.get("jwk"), context=context)
        _check_entry_integrity(kid, jwk, context=context)
        created_at = entry.get("created_at_utc")
        if created_at is not None and (
            not isinstance(created_at, str) or not created_at.strip()
        ):
            raise TrustRootError(
                f"{context}: created_at_utc must be a non-empty string or null"
            )
        if kid in catalog:
            raise TrustRootError(f"catalog {catalog_path}: duplicate kid {kid!r}")
        catalog[kid] = {
            "agent": agent,
            "domain": _check_reserved_column(
                entry.get("domain"), label="domain", context=context
            ),
            "instance": _check_reserved_column(
                entry.get("instance"), label="instance", context=context
            ),
            "jwk": jwk,
            "created_at_utc": created_at,
        }
    return catalog


def load_public_stub(stub_path: Path) -> Dict[str, Any]:
    """Load and validate a ``<kid>.pub.json`` stub written by ``oacp key gen``.

    Returns a normalized entry: agent, domain, instance, kid, jwk,
    created_at_utc. Raises ``TrustImportError`` on any structural or
    integrity failure — a stub is untrusted input until it passes here.
    """
    stub_path = Path(stub_path)

    def _reject_duplicates(pairs: List[Any]) -> Dict[str, Any]:
        seen: Dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise ValueError(f"duplicate JSON key {key!r}")
            seen[key] = value
        return seen

    try:
        stub = json.loads(
            stub_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
        )
    except (OSError, ValueError) as exc:
        raise TrustImportError(f"cannot read stub {stub_path}: {exc}") from exc
    if not isinstance(stub, dict):
        raise TrustImportError(f"stub {stub_path} must be a JSON object")

    context = f"stub {stub_path}"
    kid = stub.get("kid")
    try:
        validate_kid(kid)
    except AuthFormatError as exc:
        raise TrustImportError(f"{context}: {exc}") from exc
    try:
        jwk = _check_public_jwk(stub.get("jwk"), context=context)
        _check_entry_integrity(kid, jwk, context=context)
    except TrustRootError as exc:
        raise TrustImportError(str(exc)) from exc

    agent = stub.get("agent")
    if not isinstance(agent, str) or not AGENT_RE.fullmatch(agent):
        raise TrustImportError(f"{context}: agent must match {AGENT_RE.pattern}")

    try:
        validate_agent_urn(stub.get("agent_urn"))
        validate_instance_urn(stub.get("instance_urn"))
    except AuthFormatError as exc:
        raise TrustImportError(f"{context}: {exc}") from exc
    agent_suffix = stub["agent_urn"][len(AGENT_URN_PREFIX):]
    domain, _, urn_agent = agent_suffix.rpartition(":")
    if urn_agent != agent:
        raise TrustImportError(
            f"{context}: agent_urn names {urn_agent!r} but stub agent is {agent!r}"
        )
    instance = stub["instance_urn"][len(INSTANCE_URN_PREFIX):]

    created_at = stub.get("created_at_utc")
    if created_at is not None and (
        not isinstance(created_at, str) or not created_at.strip()
    ):
        raise TrustImportError(
            f"{context}: created_at_utc must be a non-empty string or null"
        )

    return {
        "agent": agent,
        "domain": domain,
        "instance": instance,
        "kid": kid,
        "jwk": jwk,
        "created_at_utc": created_at,
    }


# ---------------------------------------------------------------------------
# Canonical writers (atomic, whole-file re-emit)
# ---------------------------------------------------------------------------

def _atomic_write_text(path: Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


def _ordered_entry(kid: str, entry: Dict[str, Any], *, with_status: bool) -> Dict[str, Any]:
    ordered: Dict[str, Any] = {
        "agent": entry["agent"],
        "domain": entry.get("domain"),
        "instance": entry.get("instance"),
        "kid": kid,
        "jwk": {
            "kty": entry["jwk"]["kty"],
            "crv": entry["jwk"]["crv"],
            "x": entry["jwk"]["x"],
        }
        if isinstance(entry.get("jwk"), dict)
        else None,
        "created_at_utc": entry.get("created_at_utc"),
    }
    if with_status:
        ordered["status"] = entry.get("status", PIN_STATUS_ACTIVE)
        # Pins never carried created_at_utc in the 1b format — keep it out.
        ordered.pop("created_at_utc", None)
    return ordered


def _render_trust_file(
    entries: Dict[str, Dict[str, Any]], *, list_key: str, with_status: bool
) -> str:
    import yaml  # type: ignore

    ordered = [
        _ordered_entry(kid, entry, with_status=with_status)
        for kid, entry in sorted(
            entries.items(), key=lambda item: (item[1]["agent"], item[0])
        )
    ]
    document = {"version": TRUST_FILE_VERSION, list_key: ordered}
    return yaml.safe_dump(document, sort_keys=False, default_flow_style=False)


def write_catalog(catalog_path: Path, entries: Dict[str, Dict[str, Any]]) -> None:
    _atomic_write_text(
        catalog_path, _render_trust_file(entries, list_key="entries", with_status=False)
    )


def write_pins(pins_path: Path, pins: Dict[str, Dict[str, Any]]) -> None:
    _atomic_write_text(
        pins_path, _render_trust_file(pins, list_key="signers", with_status=True)
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _entries_equal(existing: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    """Identity comparison for same-kid entries: agent + public key material.

    Reserved columns are compared only when both sides carry a value —
    an entry that predates them is eligible for the in-place upgrade
    performed by `_merge_reserved_columns`, not rejected.
    """
    if existing.get("agent") != incoming.get("agent"):
        return False
    existing_jwk = existing.get("jwk") or {}
    if existing_jwk.get("x") != incoming["jwk"]["x"]:
        return False
    for column in ("domain", "instance"):
        theirs, ours = existing.get(column), incoming.get(column)
        if theirs is not None and ours is not None and theirs != ours:
            return False
    return True


def _receiver_pins_path(project_dir: Path, receiver: str) -> Path:
    """Resolve a receiver's pins path, refusing anything but an existing
    agent of this project.

    Receiver names come from CLI flags and inbox-adjacent tooling, so they
    are untrusted: the name must satisfy the agent-name rules (no
    separators, no leading dot), the agent directory must already exist,
    and the resolved directory must sit directly under the project's
    ``agents/`` root — a trust file must never be creatable outside the
    selected project.
    """
    if not isinstance(receiver, str) or not AGENT_RE.fullmatch(receiver):
        raise TrustImportError(f"receiver name must match {AGENT_RE.pattern}")
    agents_root = Path(project_dir) / "agents"
    agent_dir = agents_root / receiver
    if not agent_dir.is_dir():
        raise TrustImportError(
            f"receiver {receiver!r} has no agent directory under {agents_root}"
        )
    if agent_dir.resolve().parent != agents_root.resolve():
        raise TrustImportError(
            f"receiver {receiver!r} escapes the project agents directory"
        )
    return agent_dir / ALLOWED_SIGNERS_RELPATH


def _merge_reserved_columns(
    existing: Dict[str, Any], incoming: Dict[str, Any]
) -> bool:
    """Fill missing reserved columns from a validated incoming entry.

    Returns True when a value was populated — the caller then rewrites the
    file and reports ``updated`` instead of ``unchanged``. Existing values
    are never overwritten (`_entries_equal` already rejected conflicts).
    """
    changed = False
    for column in ("domain", "instance"):
        if existing.get(column) is None and incoming.get(column) is not None:
            existing[column] = incoming[column]
            changed = True
    return changed


def import_public_stub(
    stub_path: Path,
    project_dir: Path,
    *,
    receiver: Optional[str] = None,
    catalog_only: bool = False,
) -> Dict[str, Any]:
    """Import a public stub into the project catalog and a receiver's pins.

    Default flow records the identity in ``trust/catalog.yaml`` (zero
    authority) **and** pins it ``active`` in the receiver's
    ``allowed_signers.yaml`` (the part that grants trust). With
    ``catalog_only=True`` the identity is recorded and no authority is
    granted anywhere. An existing same-identity entry that predates the
    reserved columns is upgraded in place (``updated``).

    The whole transaction — catalog read through pins write — holds the
    ONE project trust lock (a stable ``.lock`` sibling of the catalog), so
    concurrent imports serialize instead of silently dropping each other's
    entries; atomic replace alone prevents partial files, not lost
    updates. Within the transaction the catalog is written first; if the
    pin step then refuses (same-kid conflict, revoked pin), the identity
    stays recorded. That is the intended asymmetry — recording an identity
    is always safe, granting authority is what gets refused.
    """
    if not catalog_only and not receiver:
        raise TrustImportError("receiver is required unless importing catalog-only")

    project_dir = Path(project_dir)
    pins_path: Optional[Path] = None
    if receiver is not None:
        pins_path = _receiver_pins_path(project_dir, receiver)
    entry = load_public_stub(stub_path)
    kid = entry["kid"]
    report: Dict[str, Any] = {
        "kid": kid,
        "agent": entry["agent"],
        "catalog": None,
        "pins": "skipped" if catalog_only else None,
    }

    catalog_path = project_dir / CATALOG_RELPATH
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with locked_audit(catalog_path):
        catalog = load_catalog(catalog_path)
        existing = catalog.get(kid)
        if existing is None:
            catalog[kid] = {key: entry[key] for key in
                            ("agent", "domain", "instance", "jwk", "created_at_utc")}
            write_catalog(catalog_path, catalog)
            report["catalog"] = "added"
        elif _entries_equal(existing, entry):
            if _merge_reserved_columns(existing, entry):
                write_catalog(catalog_path, catalog)
                report["catalog"] = "updated"
            else:
                report["catalog"] = "unchanged"
        else:
            raise TrustImportError(
                f"catalog already records kid {kid!r} with different identity — "
                "refusing to replace; remove the entry manually if it is wrong"
            )
        report["catalog_path"] = str(catalog_path)

        if catalog_only:
            return report

        pins = load_allowed_signers(pins_path)
        current = pins.get(kid)
        if current is None:
            pins[kid] = {
                "agent": entry["agent"],
                "domain": entry["domain"],
                "instance": entry["instance"],
                "jwk": entry["jwk"],
                "status": PIN_STATUS_ACTIVE,
            }
            write_pins(pins_path, pins)
            report["pins"] = "added"
        elif current.get("status") == PIN_STATUS_REVOKED:
            raise TrustImportError(
                f"kid {kid!r} is revoked in {pins_path} — import never reactivates "
                "a revoked pin; remove the entry manually to re-trust"
            )
        elif _entries_equal(current, entry):
            if _merge_reserved_columns(current, entry):
                write_pins(pins_path, pins)
                report["pins"] = "updated"
            else:
                report["pins"] = "unchanged"
        else:
            raise TrustImportError(
                f"pins already carry kid {kid!r} with different identity — "
                "refusing to replace; remove the entry manually if it is wrong"
            )
    report["pins_path"] = str(pins_path)
    report["receiver"] = receiver
    return report


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

class TrustRevokeError(ValueError):
    """Raised when a pin revocation is refused."""


def _agents_with_pins(project_dir: Path) -> List[str]:
    agents_root = Path(project_dir) / "agents"
    if not agents_root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in agents_root.iterdir()
        if entry.is_dir() and (entry / ALLOWED_SIGNERS_RELPATH).is_file()
    )


def revoke_pin(
    project_dir: Path,
    kid: str,
    *,
    receiver: Optional[str] = None,
    all_receivers: bool = False,
) -> Dict[str, Any]:
    """Flip a pinned kid to ``status: revoked`` through the canonical writer.

    Revocation is the one lifecycle operation that was still a hand-edit of
    ``allowed_signers.yaml`` — the exact surface where a non-canonical kid
    spelling silently fails to revoke the canonical key. This path validates
    the kid's canonical spelling up front, loads pins through the full
    integrity check, and re-emits the whole file canonically, all under the
    one project trust lock so revokes and imports serialize.

    The jwk stays in place on a revoked entry: the reader re-validates
    retained key material, so a later manual reactivation cannot smuggle a
    swapped key. An unknown kid is refused — a revocation for a never-pinned
    kid is a no-op by definition and probably a typo; revoke never creates
    entries. Revoking an already-revoked pin reports ``unchanged``
    (idempotent — fleet compromise response must be safely re-runnable).

    With ``all_receivers=True`` every receiver in the project that pins the
    kid is revoked in one transaction; receivers that never pinned it are
    skipped, but the kid must be pinned by at least one receiver.
    """
    validate_kid(kid)
    if bool(receiver) == bool(all_receivers):
        raise TrustRevokeError(
            "exactly one of a receiver or all_receivers is required"
        )

    project_dir = Path(project_dir)
    catalog_path = project_dir / CATALOG_RELPATH
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    receivers_report: Dict[str, str] = {}
    with locked_audit(catalog_path):
        # Enumeration happens under the lock so the target set is a
        # lock-consistent snapshot (an import racing this revoke cannot
        # add a receiver the transaction never saw).
        if receiver is not None:
            targets = [receiver]
        else:
            targets = _agents_with_pins(project_dir)

        # Load and integrity-validate every target before writing any of
        # them: a compromise response that fails must leave zero pins
        # changed — a partial fleet revocation would leave later receivers
        # silently trusting the compromised kid behind an error exit.
        pending: List[Tuple[Path, Dict[str, Dict[str, Any]], str]] = []
        for target in targets:
            pins_path = _receiver_pins_path(project_dir, target)
            if all_receivers and not pins_path.is_file():
                continue
            pins = load_allowed_signers(pins_path)
            entry = pins.get(kid)
            if entry is None:
                if all_receivers:
                    continue
                raise TrustRevokeError(
                    f"kid {kid!r} is not pinned by receiver {target!r} — "
                    "revoke never creates entries"
                )
            if entry.get("status") == PIN_STATUS_REVOKED:
                receivers_report[target] = "unchanged"
                continue
            entry["status"] = PIN_STATUS_REVOKED
            pending.append((pins_path, pins, target))

        for pins_path, pins, target in pending:
            write_pins(pins_path, pins)
            receivers_report[target] = "revoked"

    if not receivers_report:
        raise TrustRevokeError(
            f"kid {kid!r} is not pinned by any receiver in this project"
        )
    return {
        "kid": kid,
        "receivers": receivers_report,
        "revoked": sorted(
            name for name, state in receivers_report.items() if state == "revoked"
        ),
    }


# ---------------------------------------------------------------------------
# Catalog-vs-pins drift (consumed by `oacp doctor`)
# ---------------------------------------------------------------------------

def _drift(severity: str, code: str, message: str) -> Dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def drift_report(project_dir: Path) -> List[Dict[str, str]]:
    """Compare the project catalog against every agent's pins.

    Zero-authority semantics shape what counts as drift: an *unpinned*
    catalog identity is the normal resting state (never flagged), while a
    *pinned* identity missing from the catalog means authority was granted
    to an identity that was never recorded — that is the drift this check
    exists to catch. Integrity failures (kid not the thumbprint of its
    jwk, bad key material, private components) are enforced by the readers
    themselves and surface here as unreadable-file errors carrying the
    detail.
    """
    project_dir = Path(project_dir)
    findings: List[Dict[str, str]] = []

    catalog_path = project_dir / CATALOG_RELPATH
    catalog: Dict[str, Dict[str, Any]] = {}
    catalog_usable = True
    if not catalog_path.is_file():
        findings.append(
            _drift(DRIFT_OK, "catalog-absent", "no project catalog (trust/catalog.yaml)")
        )
    else:
        try:
            catalog = load_catalog(catalog_path)
        except TrustRootError as exc:
            catalog_usable = False
            findings.append(_drift(DRIFT_ERROR, "catalog-unreadable", str(exc)))
        else:
            findings.append(
                _drift(
                    DRIFT_OK,
                    "catalog-loaded",
                    f"catalog records {len(catalog)} identit"
                    f"{'y' if len(catalog) == 1 else 'ies'}",
                )
            )

    agents_dir = project_dir / "agents"
    if not agents_dir.is_dir():
        return findings

    # First pass: load every receiver's pins so the advisory checks below
    # can see cross-receiver state (who else pins an identity).
    loaded_pins: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        pins_path = agent_dir / ALLOWED_SIGNERS_RELPATH
        if not pins_path.is_file():
            continue
        agent = agent_dir.name
        try:
            loaded_pins[agent] = load_allowed_signers(pins_path)
        except TrustRootError as exc:
            findings.append(
                _drift(DRIFT_ERROR, "pins-unreadable", f"{agent}: {exc}")
            )

    for agent in sorted(loaded_pins):
        pins = loaded_pins[agent]
        active = 0
        for kid, pin in pins.items():
            if pin.get("status") != PIN_STATUS_ACTIVE:
                continue
            active += 1
            if catalog_usable and kid not in catalog:
                findings.append(
                    _drift(
                        DRIFT_WARN,
                        "pin-not-in-catalog",
                        f"{agent}: active pin {kid} ({pin.get('agent')}) is not "
                        "recorded in the project catalog",
                    )
                )
        findings.append(
            _drift(
                DRIFT_OK,
                "pins-loaded",
                f"{agent}: {len(pins)} pin{'s' if len(pins) != 1 else ''} "
                f"({active} active)",
            )
        )

    # Advisory: cataloged-but-unpinned identities. Zero-authority semantics
    # keep an unpinned catalog identity legitimate (the catalog grants
    # nothing), so this never blocks — but it is exactly the enablement gap
    # where a peer's signed traffic annotates `signed-unknown-kid` while the
    # trust root reports clean. Warn only when the identity shows a liveness
    # signal for this receiver (another receiver actively pins it, or the
    # receiver's inbox has traffic from that agent); otherwise note it.
    for agent in sorted(loaded_pins):
        pins = loaded_pins[agent]
        for kid, entry in catalog.items():
            catalog_agent = entry.get("agent")
            if catalog_agent == agent or kid in pins:
                continue
            pinned_elsewhere = any(
                other != agent
                and other_pins.get(kid, {}).get("status") == PIN_STATUS_ACTIVE
                for other, other_pins in loaded_pins.items()
            )
            has_traffic = _inbox_has_traffic_from(
                agents_dir / agent / "inbox", catalog_agent
            )
            severity = DRIFT_WARN if (pinned_elsewhere or has_traffic) else DRIFT_OK
            signal = (
                " (another receiver pins it)" if pinned_elsewhere
                else " (inbox traffic present)" if has_traffic
                else ""
            )
            findings.append(
                _drift(
                    severity,
                    "catalog-not-pinned",
                    f"{agent}: cataloged identity {kid} ({catalog_agent}) is "
                    f"not pinned — signed traffic from {catalog_agent} "
                    f"annotates signed-unknown-kid{signal}",
                )
            )
    return findings


_TRAFFIC_PROBE_MAX_BYTES = 1024 * 1024


def _inbox_has_traffic_from(inbox_dir: Path, agent: Optional[str]) -> bool:
    """Best-effort liveness signal: any inbox message whose top-level
    `from:` names this agent.

    Filenames embed the sender, but sender names and message types may
    both contain underscores, so the filename grammar is not parseable —
    the message's own `from` field is the attribution. Malformed or
    oversized files are skipped (this feeds an advisory, never
    enforcement).
    """
    if not agent or not inbox_dir.is_dir():
        return False
    try:
        import yaml  # type: ignore
    except Exception:
        return False
    for path in inbox_dir.glob("*.yaml"):
        try:
            if path.stat().st_size > _TRAFFIC_PROBE_MAX_BYTES:
                continue
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(loaded, dict) and loaded.get("from") == agent:
            return True
    return False


def has_trust_root(project_dir: Path) -> bool:
    """True when the project has any trust file to check."""
    project_dir = Path(project_dir)
    if (project_dir / CATALOG_RELPATH).is_file():
        return True
    agents_dir = project_dir / "agents"
    if not agents_dir.is_dir():
        return False
    return any(
        (agent_dir / ALLOWED_SIGNERS_RELPATH).is_file()
        for agent_dir in agents_dir.iterdir()
        if agent_dir.is_dir()
    )
