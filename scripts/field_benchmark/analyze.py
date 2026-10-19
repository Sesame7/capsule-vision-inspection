"""Offline reconciliation. Missing evidence is an issue, never an implicit zero."""

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import cast

type Row = dict[str, object]


def load_rows(paths: list[Path], *, budget: int = 100000) -> list[Row]:
    rows: list[Row] = []
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            while line := stream.readline(65537):
                if len(line) > 65536 or len(rows) >= budget:
                    raise ValueError("Ledger exceeds bounded analysis budget")
                value: object = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Invalid event object")
                rows.append(cast(Row, value))
    return rows


def _key(row: Row) -> tuple[object, object]:
    return row.get("generation"), row.get("cycle")


def frame_key(row: Row) -> tuple[object, ...]:
    return (*_key(row), row.get("camera"), row.get("sequence"))


def _words(row: Row) -> list[int] | None:
    ids, states = row.get("channel_ids"), row.get("business")
    if not isinstance(ids, (list, tuple)) or not isinstance(states, (list, tuple)):
        return None
    ids, states = cast(list[object], ids), cast(list[object], states)
    if len(ids) != 36 or len(states) != 36 or set(ids) != set(range(1, 37)):
        return None
    words = [0, 0, 0]
    for channel, state in zip(ids, states, strict=True):
        if type(channel) is not int or state not in ("OK", "NG", "EMPTY", "UNKNOWN"):
            return None
        if state != "OK":
            words[(channel - 1) // 16] |= 1 << ((channel - 1) % 16)
    return words


def reconcile(
    rows: list[Row],
    run: str,
    *,
    reports: bool,
    archives: bool,
    required_owners: tuple[str, ...],
) -> Row:
    issues: list[str] = []
    by_kind: dict[str, list[Row]] = defaultdict(list)
    streams: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if row.get("run") != run:
            issues.append("foreign run identity")
        kind, producer = row.get("kind"), row.get("producer")
        if not isinstance(kind, str) or not isinstance(producer, str):
            issues.append("missing event/producer identity")
            continue
        by_kind[kind].append(row)
        streams[producer].append(row)
    for producer, stream in streams.items():
        stream.sort(key=lambda r: int(cast(int, r.get("index", -1))))
        if [r.get("index") for r in stream] != list(range(len(stream))):
            issues.append(f"{producer}: missing/duplicate ledger index")
        times = [r.get("at") for r in stream]
        if any(not isinstance(v, (float, int)) for v in times) or times != sorted(
            cast(list[float], times)
        ):
            issues.append(f"{producer}: invalid event time/order")
        if not any(r.get("kind") == "clock" for r in stream):
            issues.append(f"{producer}: missing clock provenance")
    if not streams:
        issues.append("missing event ledger")

    def unique(kind: str, key: str) -> dict[object, Row]:
        found: dict[object, Row] = {}
        for row in by_kind[kind]:
            value = row.get(key)
            if value in found:
                issues.append(f"duplicate {kind}: {value}")
            found[value] = row
        return found

    plans = unique("trigger_plan", "pulse")
    if not plans:
        issues.append("missing finite trigger plan")
    sends = unique("activation_send", "pulse")
    acks = unique("activation_ack", "pulse")
    lows = {r.get("pulse") for r in by_kind["release_readback"] if r.get("low") is True}
    if not sends.keys() <= plans.keys():
        issues.append("activation without planned identity")
    for pulse in sends:
        if pulse not in acks:
            issues.append(f"activation ACK missing/uncertain: {pulse}")
        if pulse not in lows:
            issues.append(f"verified pulse release missing: {pulse}")
        transitions = [
            r
            for r in rows
            if r.get("pulse") == pulse
            and r.get("kind")
            in (
                "activation_send",
                "activation_ack",
                "release_send",
                "release_ack",
                "release_readback",
            )
        ]
        transitions.sort(key=lambda r: float(cast(float, r["at"])))
        if [r.get("kind") for r in transitions] != [
            "activation_send",
            "activation_ack",
            "release_send",
            "release_ack",
            "release_readback",
        ]:
            issues.append(f"trigger transition evidence missing/out of order: {pulse}")
    if by_kind["activation_uncertain"] or by_kind["unresolved_release"]:
        issues.append("trigger uncertainty/unresolved release observed")
    if sends and not by_kind["supervisor_ready"]:
        issues.append("activation without independent supervisor ready evidence")
    if by_kind["trigger_late"] or by_kind["trigger_cancelled"]:
        issues.append("schedule incomplete (late/cancelled); no replacement pulses")
    if len(sends) != len(plans):
        issues.append("planned triggers not all attempted")

    admissions = Counter(_key(r) for r in by_kind["cycle_admitted"])
    terminals = Counter(
        _key(r) for r in by_kind["cycle_terminal"] + by_kind["cycle_exception"]
    )
    if admissions != terminals or any(n != 1 for n in admissions.values()):
        issues.append("cycle admission/terminal exactly-once mismatch")
    if len(admissions) != len(sends):
        issues.append(
            "activation/cycle count differs; no proximity association assumed"
        )
    if (
        by_kind["cycle_exception"]
        or by_kind["cycle_rejected"]
        or by_kind["frame_rejected"]
        or by_kind["frame_submission_exception"]
    ):
        issues.append("cycle/frame runtime rejection or exception observed")
    submitted = Counter(frame_key(r) for r in by_kind["frame_submitted"])
    frame_accepted = Counter(frame_key(r) for r in by_kind["frame_admitted"])
    rejected = Counter(frame_key(r) for r in by_kind["frame_rejected"])
    if submitted != frame_accepted + rejected:
        issues.append("frame admission outcome missing/duplicated")
    if any(n != 1 for n in frame_accepted.values()):
        issues.append("duplicate admitted FrameId")
    for key in frame_accepted:
        if key[:2] not in admissions:
            issues.append(f"frame without admitted cycle: {key}")
    expected: dict[tuple[object, object], list[int]] = {}
    business: Counter[str] = Counter()
    technical: Counter[str] = Counter()
    for terminal in by_kind["cycle_terminal"]:
        words = _words(terminal)
        if words is None:
            issues.append(f"invalid 36-channel terminal: {_key(terminal)}")
            continue
        expected[_key(terminal)] = words
        business.update(cast(list[str], terminal["business"]))
        technical.update(cast(list[str], terminal.get("technical", [])))
        camera_ids = cast(list[str], terminal.get("cameras", []))
        cameras = [r for r in by_kind["camera_result"] if _key(r) == _key(terminal)]
        if len(camera_ids) != 6 or [r.get("camera") for r in cameras] != list(
            camera_ids
        ):
            issues.append(
                f"missing/unordered six-camera result evidence: {_key(terminal)}"
            )
        if len(cast(list[str], terminal.get("technical", []))) != 36:
            issues.append(f"missing technical channel evidence: {_key(terminal)}")
        if terminal.get("status") != "COMPLETE":
            issues.append(f"technical incomplete cycle: {_key(terminal)}")
    for result in by_kind["camera_result"]:
        if (
            result.get("sequence") is not None
            and frame_key(result) not in frame_accepted
        ):
            issues.append(
                f"camera result without matching admitted FrameId: {frame_key(result)}"
            )
        submitted_row = next(
            (
                r
                for r in by_kind["frame_submitted"]
                if frame_key(r) == frame_key(result)
            ),
            None,
        )
        if submitted_row is not None and result.get("shape") != submitted_row.get(
            "shape"
        ):
            issues.append(f"camera image-shape mismatch: {frame_key(result)}")
    if frame_accepted and not by_kind["sdk_metadata"]:
        issues.append("missing SDK frame metadata")
    sdk_ids = Counter(
        (r.get("source_epoch"), r.get("camera"), r.get("image_number"))
        for r in by_kind["sdk_metadata"]
    )
    if any(n != 1 for n in sdk_ids.values()):
        issues.append("duplicate SDK camera/ordinal metadata")
    for row in by_kind["frame_submitted"]:
        sdk = (row.get("source_epoch"), row.get("camera"), row.get("sequence"))
        if sdk not in sdk_ids:
            issues.append(f"missing SDK identity evidence: {frame_key(row)}")

    eligible = {_key(r): r for r in by_kind["report_eligible"]}
    if len(eligible) != len(by_kind["report_eligible"]):
        issues.append("duplicate report eligibility")
    attempts = [_key(r) for r in by_kind["report_attempt"]]
    outcomes: dict[tuple[object, object], list[Row]] = defaultdict(list)
    for row in by_kind["report_outcome"]:
        outcomes[_key(row)].append(row)
    if reports:
        skips = {_key(r) for r in by_kind["report_skipped"]}
        if set(expected) != set(eligible) | skips:
            issues.append("report eligibility/skipping observation missing")
        if len(attempts) != len(set(attempts)):
            issues.append("repeated reporting attempt")
        for key, row in eligible.items():
            if list(cast(list[int], row.get("words", []))) != expected.get(key):
                issues.append(f"expected bitmap mismatch: {key}")
            result = outcomes[key]
            if len(result) != 1:
                issues.append(f"missing/duplicate report terminal: {key}")
            elif result[0].get("outcome") != "acknowledged":
                issues.append(f"report failed/skipped/uncertain: {key}")
            if key not in attempts:
                issues.append(f"eligible report not attempted: {key}")
            wire = [r for r in by_kind["report_write_send"] if _key(r) == key]
            if [r.get("start") for r in wire] != [50, 49]:
                issues.append(f"bitmap/sequence write order missing/mismatch: {key}")
            elif wire[0].get("words") != expected.get(key):
                issues.append(f"wire bitmap mismatch: {key}")
        commits = by_kind["target_commit"]
        if len(commits) != len(attempts):
            issues.append("target commit count differs from attempts (ACK is separate)")
        initial = by_kind["report_initial"]
        last_sequence: int | None = None
        if len(initial) != 1 or type(initial[0].get("sequence")) is not int:
            issues.append("initial reporting sequence observation missing/duplicated")
        else:
            last_sequence = cast(int, initial[0]["sequence"])
        for key, commit in zip(attempts, commits, strict=False):
            result = outcomes[key]
            if len(result) != 1:
                continue
            outcome = result[0]
            if (commit.get("sequence"), commit.get("words")) != (
                outcome.get("sequence"),
                outcome.get("words"),
            ):
                issues.append(f"target sequence/bitmap mismatch: {key}")
            if outcome.get("words") != expected.get(key):
                issues.append(f"worker bitmap mismatch: {key}")
            sequence = commit.get("sequence")
            if type(sequence) is not int or not 0 <= sequence <= 65535:
                issues.append("invalid target sequence")
            elif last_sequence is not None and sequence != (last_sequence + 1) & 0xFFFF:
                issues.append("target sequence progression mismatch")
            if type(sequence) is int:
                last_sequence = sequence
    elif eligible or attempts or outcomes or by_kind["target_commit"]:
        issues.append("report activity in disabled mode")

    archive_eligible = Counter(frame_key(r) for r in by_kind["archive_eligible"])
    written = Counter(frame_key(r) for r in by_kind["archive_written"])
    failed = Counter(frame_key(r) for r in by_kind["archive_failed"])
    if archives:
        if archive_eligible != written + failed:
            issues.append("archive eligible images missing/dropped or duplicated")
        snapshots = by_kind["archive_snapshot"]
        if not snapshots:
            issues.append("missing archive drain/capacity evidence")
        elif any(r.get("dropped", 0) or r.get("failed", 0) for r in snapshots):
            issues.append(
                "archive drops/failures observed; per-drop identity unavailable"
            )
        if failed:
            issues.append("archive write failure")
        if written and not by_kind["archive_file"]:
            issues.append("archive file/byte verification missing")
        files = Counter(frame_key(r) for r in by_kind["archive_file"])
        if files != written:
            issues.append("archive file identity/count mismatch")
        if snapshots and (
            snapshots[-1].get("alive") is not False
            or snapshots[-1].get("pending") != 0
            or snapshots[-1].get("retained_bytes") != 0
        ):
            issues.append("archive drain/termination incomplete")
    elif archive_eligible or written:
        issues.append("archive activity in disabled mode")

    if not by_kind["stop_requested"]:
        issues.append("Stop request not observed")
    owners = {r.get("owner"): r for r in by_kind["owner_stopped"]}
    stopped = {
        name
        for name, row in owners.items()
        if row.get("terminated") is True and row.get("cleanup_safe") is True
    }
    for owner in required_owners:
        if owner not in stopped:
            issues.append(f"actual owner termination missing: {owner}")
    if by_kind["missing"]:
        issues.extend(
            f"missing observation: {r.get('observation')}" for r in by_kind["missing"]
        )

    timings: dict[str, object] = {}
    for kind, field in (
        ("inference_finished", "duration_ms"),
        ("report_outcome", "completion_to_ack_ms"),
        ("camera_result", "vimo_ms"),
        ("archive_written", "duration_ms"),
    ):
        samples = sorted(
            float(v)
            for r in by_kind[kind]
            if isinstance(v := r.get(field), (int, float))
        )
        timings[f"{kind}.{field}"] = {
            "count": len(samples),
            "missing": sum(r.get(field) is None for r in by_kind[kind]),
            "p50": samples[(len(samples) - 1) * 50 // 100] if samples else None,
            "p95": samples[(len(samples) - 1) * 95 // 100] if samples else None,
            "p99": samples[(len(samples) - 1) * 99 // 100] if samples else None,
            "max": samples[-1] if samples else None,
        }
    return {
        "run": run,
        "scope": "backend only; not Qt/Web or electrical-edge performance",
        "evidence_complete": not issues,
        "issues": sorted(set(issues)),
        "counts": {
            kind: len(values) for kind, values in sorted(by_kind.items()) if values
        },
        "planned": len(plans),
        "attempted": len(sends),
        "activation_acks": len(acks),
        "pulse_releases": len(set(sends) & lows),
        "business": dict(business),
        "technical": dict(technical),
        "reporting": {
            "eligible": len(eligible),
            "attempted": len(attempts),
            "outcomes": dict(
                Counter(str(r.get("outcome")) for r in by_kind["report_outcome"])
            ),
            "uncertain": sum(
                r.get("outcome") == "failed"
                and r.get("stage") in ("bitmap", "sequence")
                and r.get("end_code") is None
                for r in by_kind["report_outcome"]
            ),
            "target_commits": len(by_kind["target_commit"]),
        },
        "archive": {
            "eligible": sum(archive_eligible.values()),
            "written": sum(written.values()),
            "failed": sum(failed.values()),
            "encoded_bytes": sum(
                cast(int, r.get("bytes", 0)) for r in by_kind["archive_file"]
            ),
        },
        "timings_ms": timings,
        "quantiles": "lower order statistic: floor((n-1)*p)",
        "clock": "producer host monotonic; device timestamps raw; no cross-clock subtraction",
    }
