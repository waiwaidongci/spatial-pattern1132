"""Two-phase whole-period replacement: preflight (read-only) then confirmed apply.

Preflight validates the submitted cells/edges, keeps annotations by cell code,
and reports every broken row in one response. Nothing is written to the
period's grid or edges until confirm receives the token of that exact batch.
Confirm must be repeated with the outstanding token; if the source values,
positions, annotations or undirected edges changed in between, the batch is
rejected instead of overwriting the newer state.
"""
import secrets
from collections import Counter

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ..models import GridCell, NeighborEdge, SpatialRun, SurveyReplacement
from ..utils.clock import now
from ..utils.fingerprint import fingerprint
from ..utils.serialize import record
from .audit_service import log
from .graph_service import prepare_graph

KEEP_RULE = "批注按格编号保留：同编号沿用来源批注；新编号使用提交批注；未提交的旧编号视为该格移除。"
STATUS_LABEL = {"confirmed": "已确认", "superseded": "被新预检替代", "stale": "预检已失效，需重新预检"}

# ---------------------------------------------------------------- validation

def _finite(value):
    try:
        return value == value and value not in (float("inf"), float("-inf"))
    except Exception:
        return False


def _clean_edges(raw_edges):
    """Collapse orientation and spot undirected duplicates without raising."""
    issues, normalized = [], []
    if not isinstance(raw_edges, list):
        return [], ["邻接边须为列表"]
    for i, e in enumerate(raw_edges):
        label = f"edges[{i}]"
        if not isinstance(e, dict):
            issues.append((label, "邻接边须为对象，含字符串端点a和b"))
            continue
        a, b = e.get("a"), e.get("b")
        if not isinstance(a, str) or not a.strip() or not isinstance(b, str) or not b.strip():
            issues.append((label, "端点a和b须为非空字符串"))
            continue
        if a == b:
            issues.append((label, f"无向邻接禁止自环 {a}→{b}"))
            continue
        normalized.append((a, b, label))
    return normalized, issues


def preflight_replacement(survey, source_cells, source_edges, payload):
    """Return (batch, report). batch is None whenever report lists broken rows."""
    raw_cells = payload.get("cells")
    cells, broken = [], []

    def broken_row(label, code, field, message, value=None, row=None):
        broken.append({"row": row, "label": label, "code": code, "field": field, "message": message, "value": value})

    if not isinstance(raw_cells, list) or not raw_cells:
        broken_row("cells", None, "cells", "整期替换须提交非空格点列表")
    else:
        for i, c in enumerate(raw_cells):
            label, row = f"cells[{i}]", i + 1
            if not isinstance(c, dict):
                broken_row(label, None, "cell", "格点须为对象", row=row)
                continue
            code = c.get("code")
            if not isinstance(code, str) or not code.strip():
                broken_row(label, code, "code", "格点编号须为非空字符串", code, row)
                continue
            code = code.strip()
            pos = {}
            valid = True
            for field in ("row", "col"):
                value = c.get(field)
                if type(value) is not int or value < 0:
                    broken_row(label, code, field, "位置须为非负整数", value, row)
                    valid = False
                else:
                    pos[field] = value
            value = c.get("value")
            if type(value) not in (int, float) or isinstance(value, bool) or not _finite(value):
                broken_row(label, code, "value", "观测均值须为有限数值", value, row)
                valid = False
            elif abs(float(value)) > 1e9:
                broken_row(label, code, "value", "观测均值绝对值不得超过1e9", value, row)
                valid = False
            if not valid:
                continue
            note = c.get("note")
            if note is None:
                note = ""
            if not isinstance(note, str):
                broken_row(label, code, "note", "批注须为字符串", note, row)
                continue
            if len(note) > 1500:
                broken_row(label, code, "note", "批注不得超过1500字", note, row)
                continue
            cells.append({"code": code, "row": pos["row"], "col": pos["col"], "value": float(value), "provided_note": note})

    duplicate_codes = {code for code, n in Counter(c["code"] for c in cells).items() if n > 1}
    for c in cells:
        if c["code"] in duplicate_codes:
            broken_row(f"cells[{c['code']}]", c["code"], "code", "格点编号重复", c["code"])
    duplicate_positions = {pos for pos, n in Counter((c["row"], c["col"]) for c in cells).items() if n > 1}
    for c in cells:
        if (c["row"], c["col"]) in duplicate_positions:
            broken_row(f"cells[{c['code']}]", c["code"], "position", f"位置重复：行{c['row']} / 列{c['col']}被多个格点占用", {"row": c["row"], "col": c["col"]})

    raw_edges = payload.get("edges")
    normalized, edge_issues = _clean_edges(raw_edges)
    for label, message in edge_issues:
        broken_row(label, None, "edges", message)
    known = {c["code"] for c in cells}
    if isinstance(raw_edges, list):
        for i, e in enumerate(raw_edges):
            if not isinstance(e, dict):
                continue
            a, b = e.get("a"), e.get("b")
            if not isinstance(a, str) or not isinstance(b, str):
                continue
            for endpoint in (a, b):
                if endpoint not in known:
                    broken_row(f"edges[{i}]", endpoint, "edge", f"邻接边引用未知格点 {endpoint}", {"a": a, "b": b})
    pair_counts = Counter(tuple(sorted((a, b))) for a, b, _ in normalized)
    for pair, n in pair_counts.items():
        if n > 1:
            for a, b, label in normalized:
                if tuple(sorted((a, b))) == pair:
                    broken_row(label, f"{pair[0]}—{pair[1]}", "edge", f"无向邻接边重复（{pair[0]}—{pair[1]}，出现{n}次；a↔b 与 b↔a 视为同一条）", {"a": a, "b": b})

    summary = {
        "cell_rows": len(raw_cells) if isinstance(raw_cells, list) else 0,
        "edge_rows": len(raw_edges) if isinstance(raw_edges, list) else 0,
        "broken": len(broken),
        "kept_notes_rule": KEEP_RULE,
    }
    if broken:
        return None, {"summary": summary, "broken_rows": broken}

    source_by_code = {c["code"]: c for c in source_cells}
    merged, preserved = [], 0
    for c in cells:
        source = source_by_code.get(c["code"])
        if source is not None:
            merged_note = source["note"]
            if source["note"]:
                preserved += 1
        else:
            merged_note = c["provided_note"]
        merged.append({"code": c["code"], "row": c["row"], "col": c["col"], "value": c["value"], "note": merged_note})

    try:
        _ordered, _w, graph = prepare_graph(merged, raw_edges or [])
    except ValueError as e:
        summary["broken"] = 1
        return None, {"summary": summary, "broken_rows": [{"row": None, "label": "graph", "code": None, "field": "graph", "message": str(e), "value": None}]}

    canon_edges = sorted(({"a": min(a, b), "b": max(a, b)} for a, b, _ in normalized), key=lambda e: (e["a"], e["b"]))
    batch = {"survey_id": survey.id, "survey_code": survey.code, "cells": merged, "edges": canon_edges}
    return {
        "survey_id": survey.id,
        "survey_code": survey.code,
        "batch": batch,
        "batch_hash": fingerprint(batch),
        "source_state_hash": _state_hash(source_cells, source_edges),
        "cell_count": len(merged),
        "edge_count": len(canon_edges),
        "preserved_notes": preserved,
        "removed_codes": sorted(set(source_by_code) - {c["code"] for c in merged}),
        "added_codes": sorted({c["code"] for c in merged} - set(source_by_code)),
        "graph": graph,
        "rule": KEEP_RULE,
    }, {"summary": {**summary, "broken": 0}, "broken_rows": []}


def _state_hash(cell_rows, edge_rows):
    canon_edges = sorted(({"a": min(e["a"], e["b"]), "b": max(e["a"], e["b"])} for e in edge_rows), key=lambda e: (e["a"], e["b"]))
    return fingerprint({
        "cells": [{"code": c["code"], "row": c["row"], "col": c["col"], "value": c["value"], "note": c["note"]} for c in cell_rows],
        "edges": canon_edges,
    })


# ---------------------------------------------------------------- storage

def _live_cells(db, sid):
    return [record(c) for c in db.query(GridCell).filter_by(survey_id=sid).order_by(GridCell.code).populate_existing()]


def _live_edges(db, sid):
    return [record(e) for e in db.query(NeighborEdge).filter_by(survey_id=sid).order_by(NeighborEdge.a, NeighborEdge.b).populate_existing()]


def lock_period(db, sid):
    """Serialize confirm/annotate on PostgreSQL; SQLite already serializes writes."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 100_000_000 + int(sid)})


def _run_refs(db, sid):
    return [{"run_id": r.id, "name": r.name, "status": r.status, "created_at": r.created_at} for r in db.query(SpatialRun).filter_by(survey_id=sid).order_by(SpatialRun.id)]


def _row_payload(row):
    return {
        "replacement_id": row.id,
        "survey_id": row.survey_id,
        "status": row.status,
        "batch_hash": row.batch_hash,
        "source_state_hash": row.source_state_hash,
        "cell_count": row.cell_count,
        "edge_count": row.edge_count,
        "preserved_notes": row.preserved_notes,
        "submitted_by": row.submitted_by,
        "created_at": row.created_at,
        "confirmed_by": row.confirmed_by,
        "confirmed_at": row.confirmed_at,
        "note": row.note,
        "summary": {"cell_rows": row.cell_count, "edge_rows": row.edge_count, "broken": 0},
        "broken_rows": [],
        "token": None,
    }


def _apply_batch(db, survey, payload):
    """Replace live cells and edges inside the caller's transaction."""
    db.query(GridCell).filter_by(survey_id=survey.id).delete(synchronize_session=False)
    db.query(NeighborEdge).filter_by(survey_id=survey.id).delete(synchronize_session=False)
    db.flush()
    for c in payload["cells"]:
        db.add(GridCell(survey_id=survey.id, code=c["code"], row=c["row"], col=c["col"], value=float(c["value"]), note=c.get("note") or ""))
    for e in payload["edges"]:
        db.add(NeighborEdge(survey_id=survey.id, a=e["a"], b=e["b"]))
    db.flush()


def preflight(db, survey, payload, user):
    source_cells, source_edges = _live_cells(db, survey.id), _live_edges(db, survey.id)
    batch, report = preflight_replacement(survey, source_cells, source_edges, payload)
    if batch is None:
        log(db, user.email, "replacement.preflight.rejected", survey.code)
        db.commit()
        return {"status": "invalid", "survey_id": survey.id, "survey_code": survey.code, "token": None, **report}

    # Reuse an outstanding token only when both the submitted batch and the
    # source state are unchanged; otherwise the stored payload (including the
    # preserved annotations) would be stale, so supersede it and issue a new one.
    current = db.query(SurveyReplacement).filter_by(
        survey_id=survey.id, batch_hash=batch["batch_hash"], source_state_hash=batch["source_state_hash"], status="preflight"
    ).order_by(SurveyReplacement.id.desc()).first()
    if current is None:
        for stale in db.query(SurveyReplacement).filter_by(survey_id=survey.id, status="preflight").all():
            stale.status = "superseded"
        current = SurveyReplacement(
            survey_id=survey.id,
            token=secrets.token_urlsafe(24),
            batch_hash=batch["batch_hash"],
            source_state_hash=batch["source_state_hash"],
            payload=batch["batch"],
            cell_count=batch["cell_count"],
            edge_count=batch["edge_count"],
            preserved_notes=batch["preserved_notes"],
            submitted_by=user.email,
        )
        db.add(current)
        db.flush()
    log(db, user.email, "replacement.preflight", f"{survey.code}:{current.id}")
    db.commit()
    return {
        "status": "ready",
        "replacement_id": current.id,
        "token": current.token,
        **{k: batch[k] for k in ("survey_id", "survey_code", "batch_hash", "source_state_hash", "cell_count", "edge_count", "preserved_notes", "removed_codes", "added_codes", "graph", "rule")},
        "summary": report["summary"],
        "broken_rows": [],
        "runs_on_period": _run_refs(db, survey.id),
    }


def confirm(db, survey, token, user):
    row = db.query(SurveyReplacement).filter_by(survey_id=survey.id, token=token).first()
    if not row:
        raise HTTPException(404, "预检批次不存在；请重新预检后再确认")
    if row.status != "preflight":
        raise HTTPException(409, f"该批次{STATUS_LABEL.get(row.status, row.status)}，不能再次确认")
    lock_period(db, survey.id)
    # Re-read after taking the lock: another confirmer may have won the race,
    # and the identity map would otherwise hand us the pre-lock snapshot.
    row = db.query(SurveyReplacement).filter_by(survey_id=survey.id, token=token).populate_existing().first()
    if not row or row.status != "preflight":
        raise HTTPException(409, "该批次已被确认或被新预检替代；请刷新后再操作")
    if db.query(SurveyReplacement).filter_by(survey_id=survey.id, status="confirmed").first():
        raise HTTPException(409, "该观测期已有已确认批次；整期数据只允许替换一次")

    cells, edges = _live_cells(db, survey.id), _live_edges(db, survey.id)
    if _state_hash(cells, edges) != row.source_state_hash:
        row.status = "stale"
        db.commit()
        raise HTTPException(409, "预检后来源数值、位置、批注或邻接边已改动；请重新预检，本次不覆盖")

    guard = (
        db.query(SurveyReplacement)
        .filter(SurveyReplacement.id == row.id, SurveyReplacement.status == "preflight")
        .update({"status": "confirmed", "confirmed_by": user.email, "confirmed_at": now()}, synchronize_session=False)
    )
    if guard != 1:
        db.rollback()
        raise HTTPException(409, "该批次正被并发确认；请刷新后再操作")
    try:
        _apply_batch(db, survey, row.payload)
        log(db, user.email, "replacement.confirm", f"{survey.code}:{row.id}")
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "该观测期已被并发确认；本次整批未写入")
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return {
        **_row_payload(row),
        "status": "confirmed",
        "cell_count": row.cell_count,
        "edge_count": row.edge_count,
        "preserved_notes": row.preserved_notes,
    }


def list_replacements(db, survey):
    return [_row_payload(r) for r in db.query(SurveyReplacement).filter_by(survey_id=survey.id).order_by(SurveyReplacement.id.desc())]


def confirmed_batch(db, sid):
    row = db.query(SurveyReplacement).filter_by(survey_id=sid, status="confirmed").order_by(SurveyReplacement.id.desc()).first()
    return None if row is None else {"replacement_id": row.id, "batch_hash": row.batch_hash, "confirmed_at": row.confirmed_at, "confirmed_by": row.confirmed_by}
