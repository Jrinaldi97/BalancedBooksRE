"""Bounded agent response projections and optional connection preconditions."""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import sys
import json
import uuid
from typing import Literal
from pydantic import Field
from app.portfolio import schemas as s

Projection = Literal["full", "compact"]


class ConnectionExpectation(s.Input):
    company_session_id: str | None = Field(default=None, min_length=1, max_length=200)
    command_schema_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_tree_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    executable_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CommandInput(s.Command):
    expected_connection: ConnectionExpectation | None = None


class CommitInput(s.Input):
    digest: str
    expected_connection: ConnectionExpectation | None = None


def _build_identity():
    frozen = bool(getattr(sys, "frozen", False))
    result = {
        "mode": "packaged" if frozen else "source",
        "process_instance_id": uuid.uuid4().hex,
        "source_tree_sha256": None,
        "executable_sha256": None,
        "identity_scope": "Process startup file snapshot; not a signature or deployment attestation.",
    }
    try:
        manifest_path = Path(__file__).with_name("build_manifest.json")
        if manifest_path.is_file() and manifest_path.stat().st_size <= 16384:
            manifest = json.loads(manifest_path.read_text())
            if isinstance(manifest, dict):
                result["manifest"] = {
                    key: manifest[key]
                    for key in (
                        "source_commit",
                        "source_tree_sha256",
                        "built_at",
                        "dirty",
                    )
                    if key in manifest and isinstance(manifest[key], (str, bool))
                }
                result["manifest_scope"] = (
                    "Build-supplied metadata; executable hash is computed independently."
                )
        if frozen:
            with open(sys.executable, "rb") as stream:
                digest = sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            result["executable_sha256"] = digest.hexdigest()
        else:
            root = Path(__file__).resolve().parents[1]
            digest = sha256()
            for path in sorted(root.rglob("*.py")):
                digest.update(path.relative_to(root).as_posix().encode() + b"\0")
                digest.update(sha256(path.read_bytes()).digest())
            result["source_tree_sha256"] = digest.hexdigest()
    except (OSError, ValueError):
        result["identity_scope"] = (
            "Build files unavailable; exact build binding is unavailable."
        )
    return result


# Read once while routes load, rather than claiming a later disk edit changed
# the application already running in this process.
BUILD_IDENTITY = _build_identity()


def company_session_id(db):
    from app.models.settings import Settings
    from app.services.auth import COMPANY_SESSION_SETTING

    row = db.query(Settings).filter_by(key=COMPANY_SESSION_SETTING).first()
    return row.value if row else None


def check_connection(db, expected, schema_digest):
    from app.portfolio.service import Problem

    if expected is None:
        return
    current = {
        "company_session_id": company_session_id(db),
        "command_schema_digest": (
            schema_digest()
            if callable(schema_digest) and expected.command_schema_digest is not None
            else schema_digest
        ),
        "source_tree_sha256": BUILD_IDENTITY["source_tree_sha256"],
        "executable_sha256": BUILD_IDENTITY["executable_sha256"],
    }
    mismatches = [
        field
        for field, value in expected.model_dump(exclude_none=True).items()
        if current[field] != value
    ]
    if mismatches:
        raise Problem(
            "connection_precondition_failed",
            "Company or application contract changed; read context and reassess before retrying",
            {"mismatched_fields": mismatches, "required_action": "refresh_context"},
        )


def _omit(record, keys, path, omitted):
    for key in keys:
        if key in record:
            del record[key]
            omitted.append(path + "/" + key)


def _mark_projection(result, omitted, reference):
    result["projection"] = "compact"
    result["omitted_fields"] = sorted(omitted)
    result["full_result_ref"] = reference
    return result


def project_state(state, projection):
    if projection == "full":
        return state
    result = deepcopy(state)
    omitted = []
    _omit(result["observation"], ("source",), "observation", omitted)
    for i, review in enumerate(result["reviews"]):
        _omit(review, ("detail", "resolution"), f"reviews/{i}", omitted)
    # Retain every financial component and both basis residuals; the digest is
    # always the full state digest, not a digest of the shortened response.
    return _mark_projection(
        result,
        omitted,
        f"/api/portfolio/observations/{state['observation']['id']}/state?as_of={state['as_of']}",
    )


def project_event(detail, projection):
    if projection == "full":
        return detail
    result = deepcopy(detail)
    omitted = []
    _omit(result["event"], ("source", "rationale"), "event", omitted)
    for collection in ("reviews_created", "reviews_resolved"):
        for i, review in enumerate(result.get(collection, [])):
            _omit(review, ("detail", "resolution"), f"{collection}/{i}", omitted)
    return _mark_projection(
        result, omitted, f"/api/portfolio/events/{detail['event']['id']}"
    )


def empty_verification():
    return {
        "external_evidence": "not_recorded",
        "latest_attestations": [],
        "history": [],
        "automated_external_validation": False,
        "note": "Reviewer attestations bind the exact committed effects and registered source hashes; the application does not independently fetch or certify external evidence.",
    }


def operation_result(result, execution_state="committed", projection="full"):
    """API boundary states: call 'committed' only after the transaction commits."""
    from app.portfolio.service import digest
    from urllib.parse import urlencode

    output = deepcopy(result)
    output["execution_state"] = execution_state
    output["verification"] = empty_verification()
    if "results" in output:
        output["results"] = [
            operation_result(child, execution_state, projection)
            for child in output["results"]
        ]
    output["effect_digest"] = digest(
        {k: v for k, v in result.items() if k != "replayed"}
    )
    if projection == "compact":
        omitted = []
        for i, transition in enumerate(
            output.get("review_lifecycle", {}).get("transitions", [])
        ):
            _omit(transition, ("detail",), f"review_lifecycle/transitions/{i}", omitted)
        _mark_projection(
            output,
            omitted,
            "/api/portfolio/requests?" + urlencode({"key": output["key"]}),
        )
    return output


def proposal_result(proposal, projection="full"):
    preview = proposal.preview
    projected = {
        "result": operation_result(preview["result"], "prepared", projection),
        "events": [project_event(item, projection) for item in preview["events"]],
    }
    reference = f"/api/portfolio/proposals/{proposal.id}"
    if projection == "compact":

        def preview_reference(value):
            if isinstance(value, dict):
                if "full_result_ref" in value:
                    value["full_result_ref"] = reference
                for child in value.values():
                    preview_reference(child)
            elif isinstance(value, list):
                for child in value:
                    preview_reference(child)

        preview_reference(projected)
    return {
        "proposal_id": proposal.id,
        "digest": proposal.digest,
        "preview": projected,
        "preview_ids_are_provisional": True,
        "committed": bool(proposal.committed_result),
        "execution_state": "committed" if proposal.committed_result else "prepared",
        "verification": {"external_evidence": "not_recorded"},
        **(
            {
                "result": operation_result(
                    proposal.committed_result, projection=projection
                )
            }
            if proposal.committed_result
            else {}
        ),
    }
