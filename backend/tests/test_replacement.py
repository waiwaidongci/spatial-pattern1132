"""Integration tests for whole-period replacement: preflight, confirm, drift, concurrency, rollback.

Runs against SQLite in a per-test temp dir; point DATABASE_URL at PostgreSQL to
exercise the same suite (advisory locks then serialize confirmation).
"""
import os, sys, tempfile, threading

os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mkdtemp(prefix="spatial-test-") + "/test.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SEED_DEMO", "false")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.main import app  # noqa: E402
from src.config.database import Base, get_db  # noqa: E402
from src.controllers import dependencies  # noqa: E402
from src.models import GridCell, NeighborEdge, SamplingNote, SpatialRun, SpatialSurvey, SurveyReplacement, User  # noqa: E402
from src.services.analysis_service import analyze_spatial  # noqa: E402
from src.services.run_service import create_snapshot  # noqa: E402

CELLS = [{"code": f"G01-0{i+1}", "row": 0, "col": i} for i in range(4)]
# square + one diagonal, stored undirected
EDGES = [{"a": "G01-01", "b": "G01-02"}, {"a": "G01-02", "b": "G01-03"}, {"a": "G01-03", "b": "G01-04"}, {"a": "G01-01", "b": "G01-04"}, {"a": "G01-01", "b": "G01-03"}]


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def Session(db_path):
    engine = create_engine("sqlite:///" + db_path, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def clients(Session):
    started = []

    def build(actor="farah@example", role="analyst"):
        def override_db():
            db = Session()
            try:
                yield db
            finally:
                db.close()

        def override_user():
            return User(id=1, email=actor, name=actor, role=role, password_hash="")

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[dependencies.current_user] = override_user
        test_client = TestClient(app)
        started.append(test_client)
        return test_client

    yield build
    app.dependency_overrides.clear()
    for test_client in started:
        test_client.close()


@pytest.fixture
def client(clients):
    return clients()


@pytest.fixture
def sid(Session):
    with Session() as db:
        survey = SpatialSurvey(code="LG-TEST", name="替换验证", period="2026-11-13", description="测试观测期", source="synthetic.csv", cell_size_m=250)
        db.add(survey)
        db.flush()
        for i, c in enumerate(CELLS):
            db.add(GridCell(survey_id=survey.id, code=c["code"], row=c["row"], col=c["col"], value=float(i + 1), note="来源批注" if i % 2 == 0 else ""))
        for e in EDGES:
            db.add(NeighborEdge(survey_id=survey.id, a=e["a"], b=e["b"]))
        db.commit()
        return survey.id


def payload(values=(10, 11, 12, 13), cells=None, edges=None):
    cells = cells if cells is not None else CELLS
    return {"cells": [dict(c, value=v) for c, v in zip(cells, values)], "edges": edges if edges is not None else EDGES}


def snapshot_state(Session, sid):
    with Session() as db:
        cells = [(c.code, c.row, c.col, c.value, c.note) for c in db.query(GridCell).filter_by(survey_id=sid).order_by(GridCell.code)]
        edges = sorted((e.a, e.b) for e in db.query(NeighborEdge).filter_by(survey_id=sid))
    return cells, edges


def test_preflight_reports_all_broken_rows_and_writes_nothing(client, Session, sid):
    before = snapshot_state(Session, sid)
    bad = {
        "cells": [
            {"code": "G01-01", "row": 0, "col": 0, "value": 9},
            {"code": "G01-02", "row": 0, "col": 1, "value": "bad"},
            {"code": "G01-03", "row": 0, "col": 2, "value": 3},
            {"code": "G01-04", "row": 0, "col": 0, "value": 4},
        ],
        "edges": [
            {"a": "G01-01", "b": "G01-02"},
            {"a": "G01-02", "b": "G01-01"},
            {"a": "G01-01", "b": "G01-09"},
            {"a": "G01-01", "b": "G01-01"},
        ],
    }
    r = client.post(f"/surveys/{sid}/replacement/preflight", json=bad)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "invalid" and body["token"] is None
    assert body["summary"]["broken"] == len(body["broken_rows"]) >= 5
    fields = {b["field"] for b in body["broken_rows"]}
    assert {"value", "position", "edge", "edges"} <= fields
    assert any("无向邻接边重复" in b["message"] for b in body["broken_rows"])
    assert any("自环" in b["message"] for b in body["broken_rows"])
    assert any("未知格点 G01-09" in b["message"] for b in body["broken_rows"])
    assert snapshot_state(Session, sid) == before
    with Session() as db:
        assert db.query(SurveyReplacement).count() == 0


def test_preflight_keeps_annotations_by_cell_code_and_collapses_edge_orientation(client, Session, sid):
    reordered_cells = [CELLS[3], CELLS[0], CELLS[1], CELLS[2]]
    flipped_edges = [{"a": e["b"], "b": e["a"]} for e in EDGES]
    r = client.post(f"/surveys/{sid}/replacement/preflight", json=payload(cells=reordered_cells, edges=flipped_edges))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["cell_count"] == 4 and body["edge_count"] == 5
    assert body["preserved_notes"] == 2
    assert body["token"] and body["batch_hash"]
    with Session() as db:
        row = db.query(SurveyReplacement).filter_by(id=body["replacement_id"]).one()
        notes = {c["code"]: c["note"] for c in row.payload["cells"]}
        assert notes == {"G01-01": "来源批注", "G01-02": "", "G01-03": "来源批注", "G01-04": ""}
        assert sorted((e["a"], e["b"]) for e in row.payload["edges"]) == sorted((min(e["a"], e["b"]), max(e["a"], e["b"])) for e in EDGES)
        # preflight is read-only: live grid is untouched
        assert db.query(GridCell).filter_by(survey_id=sid).count() == 4


def test_preflight_rejects_duplicate_undirected_edge_even_when_direction_differs(client, sid):
    dup = EDGES + [{"a": EDGES[0]["b"], "b": EDGES[0]["a"]}]
    r = client.post(f"/surveys/{sid}/replacement/preflight", json=payload(edges=dup))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "invalid"
    messages = " ".join(b["message"] for b in body["broken_rows"])
    assert "无向邻接边重复" in messages and "a↔b 与 b↔a 视为同一条" in messages


def test_confirm_rejects_unknown_token_and_missing_ack(client, sid):
    assert client.post(f"/surveys/{sid}/replacement/confirm", json={"token": "x" * 24, "confirm": True}).status_code == 404
    r = client.post(f"/surveys/{sid}/replacement/confirm", json={"token": "x" * 24})
    assert r.status_code == 422  # explicit acknowledgement is required


def test_preflight_reports_graph_level_problems_and_empty_batches(client, sid):
    # Too few cells, an isolated cell, and a batch with no rows all stay in one report.
    three = {"cells": [{"code": f"C{i}", "row": 0, "col": i, "value": i} for i in range(3)], "edges": [{"a": "C0", "b": "C1"}]}
    body = client.post(f"/surveys/{sid}/replacement/preflight", json=three).json()
    assert body["status"] == "invalid" and "4–100" in body["broken_rows"][0]["message"]
    isolated = {"cells": [{"code": f"C{i}", "row": 0, "col": i, "value": i} for i in range(4)], "edges": [{"a": "C0", "b": "C1"}]}
    body = client.post(f"/surveys/{sid}/replacement/preflight", json=isolated).json()
    assert body["status"] == "invalid" and "孤点" in body["broken_rows"][0]["message"]
    empty = client.post(f"/surveys/{sid}/replacement/preflight", json={"cells": [], "edges": []}).json()
    assert empty["status"] == "invalid" and empty["summary"]["broken"] >= 1
    assert client.post(f"/surveys/{sid}/replacement/preflight", json={"cells": [], "edges": [], "option": True}).status_code == 422


def test_confirm_rejects_superseded_batch(client, Session, sid):
    """A token only works for the exact preflight it came from; a newer preflight supersedes it."""
    first = client.post(f"/surveys/{sid}/replacement/preflight", json=payload(values=(50, 51, 52, 53))).json()
    assert first["status"] == "ready"
    second = client.post(f"/surveys/{sid}/replacement/preflight", json=payload(values=(60, 61, 62, 63))).json()
    assert second["status"] == "ready"
    r = client.post(f"/surveys/{sid}/replacement/confirm", json={"token": first["token"], "confirm": True})
    assert r.status_code == 409, r.text
    with Session() as db:
        assert db.query(SurveyReplacement).filter_by(id=first["replacement_id"]).one().status in ("superseded", "stale")
        assert db.query(GridCell).filter_by(survey_id=sid, code="G01-01").one().value == 1.0


def test_confirm_rejects_exactly_matching_batch_hash_but_new_lifecycle(client, Session, sid):
    """Re-submitting the very same content gives a fresh token; the old token still cannot win twice."""
    first = client.post(f"/surveys/{sid}/replacement/preflight", json=payload()).json()
    same = client.post(f"/surveys/{sid}/replacement/preflight", json=payload()).json()
    assert same["token"] == first["token"] and same["replacement_id"] == first["replacement_id"]
    ok = client.post(f"/surveys/{sid}/replacement/confirm", json={"token": first["token"], "confirm": True})
    assert ok.status_code == 200
    assert client.post(f"/surveys/{sid}/replacement/confirm", json={"token": first["token"], "confirm": True}).status_code == 409


def test_confirm_refuses_after_source_or_note_drift(client, Session, sid):
    pre = client.post(f"/surveys/{sid}/replacement/preflight", json=payload()).json()
    token = pre["token"]
    with Session() as db:
        db.query(GridCell).filter_by(survey_id=sid, code="G01-02").one().note = "预检后有人改了批注"
        db.commit()
    r = client.post(f"/surveys/{sid}/replacement/confirm", json={"token": token, "confirm": True})
    assert r.status_code == 409 and "改动" in r.json()["detail"]
    with Session() as db:
        assert db.query(SurveyReplacement).filter_by(id=pre["replacement_id"]).one().status == "stale"
        assert db.query(GridCell).filter_by(survey_id=sid, code="G01-01").one().value == 1.0

    fresh = client.post(f"/surveys/{sid}/replacement/preflight", json=payload()).json()
    ok = client.post(f"/surveys/{sid}/replacement/confirm", json={"token": fresh["token"], "confirm": True})
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "confirmed"
    with Session() as db:
        values = {c.code: c.value for c in db.query(GridCell).filter_by(survey_id=sid)}
        assert values == {"G01-01": 10.0, "G01-02": 11.0, "G01-03": 12.0, "G01-04": 13.0}
    # stale token cannot be replayed, and a second confirmed batch is refused
    assert client.post(f"/surveys/{sid}/replacement/confirm", json={"token": token, "confirm": True}).status_code == 409
    again = client.post(f"/surveys/{sid}/replacement/preflight", json=payload()).json()
    assert client.post(f"/surveys/{sid}/replacement/confirm", json={"token": again["token"], "confirm": True}).status_code == 409


def test_confirm_refuses_after_edge_drift(client, Session, sid):
    pre = client.post(f"/surveys/{sid}/replacement/preflight", json=payload()).json()
    with Session() as db:
        db.add(NeighborEdge(survey_id=sid, a="G01-02", b="G01-04"))
        db.commit()
    r = client.post(f"/surveys/{sid}/replacement/confirm", json={"token": pre["token"], "confirm": True})
    assert r.status_code == 409 and "邻接边" in r.json()["detail"]


def test_confirm_refuses_after_value_drift(client, Session, sid):
    pre = client.post(f"/surveys/{sid}/replacement/preflight", json=payload()).json()
    with Session() as db:
        db.query(GridCell).filter_by(survey_id=sid, code="G01-03").one().value = 99.0
        db.commit()
    assert client.post(f"/surveys/{sid}/replacement/confirm", json={"token": pre["token"], "confirm": True}).status_code == 409


def test_viewer_cannot_read_or_write_replacements(clients, sid):
    client = clients(actor="vera@example", role="viewer")
    assert client.post(f"/surveys/{sid}/replacement/preflight", json=payload()).status_code == 403
    assert client.post(f"/surveys/{sid}/replacement/confirm", json={"token": "x" * 24, "confirm": True}).status_code == 403
    assert client.get(f"/surveys/{sid}/replacement").status_code == 403


def test_confirmed_data_feeds_new_runs_while_old_snapshots_and_results_survive(client, Session, sid, monkeypatch):
    import src.services.run_service as run_service
    monkeypatch.setattr(run_service.queue, "rpush", lambda *a, **k: 1)  # no live Redis in unit tests
    with Session() as db:
        survey = db.get(SpatialSurvey, sid)
        snapshot = create_snapshot(db, survey)
        result = analyze_spatial(snapshot, 499, 2026)
        old = SpatialRun(name="旧研究", survey_id=sid, permutations=499, seed=2026, input_snapshot=snapshot, status="completed", result=result, owner="A")
        db.add(old)
        db.flush()
        db.add(SamplingNote(run_id=old.id, title="旧建议", interpretation="旧解释", proposal="旧补测", status="approved", owner="A", reviewer="B", review_note="旧批准"))
        db.commit()
        frozen_snapshot, frozen_result, old_id = old.input_snapshot, old.result, old.id

    pre = client.post(f"/surveys/{sid}/replacement/preflight", json=payload(values=(20, 21, 22, 23))).json()
    ok = client.post(f"/surveys/{sid}/replacement/confirm", json={"token": pre["token"], "confirm": True})
    assert ok.status_code == 200, ok.text

    with Session() as db:
        old = db.get(SpatialRun, old_id)
        assert old.input_snapshot == frozen_snapshot
        assert old.result == frozen_result
        assert old.status == "completed" and old.error == ""
        assert db.query(SamplingNote).filter_by(run_id=old_id).one().status == "approved"

    r = client.post("/runs", json={"name": "确认后新研究", "survey_id": sid, "permutations": 499, "seed": 2026})
    assert r.status_code == 202, r.text
    with Session() as db:
        fresh = db.query(SpatialRun).filter_by(name="确认后新研究").one()
        assert [c["value"] for c in fresh.input_snapshot["cells"]] == [20.0, 21.0, 22.0, 23.0]
        assert fresh.input_snapshot["cells"][0]["note"] == "来源批注"  # kept by code
        assert old.input_snapshot != fresh.input_snapshot


def test_two_concurrent_confirms_accept_exactly_one(client, Session, sid, db_path):
    """Two actors confirm the same preflighted batch at once; one write, one clean 409."""
    from src.services.replacement_service import confirm
    from src.models import SpatialSurvey, User
    from fastapi import HTTPException

    pre = client.post(f"/surveys/{sid}/replacement/preflight", json=payload(values=(30, 31, 32, 33))).json()
    assert pre["status"] == "ready"
    token = pre["token"]

    engine = create_engine("sqlite:///" + db_path, connect_args={"check_same_thread": False, "timeout": 15})
    Session2 = sessionmaker(bind=engine, expire_on_commit=False)
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(email):
        db = Session2()
        try:
            barrier.wait(timeout=5)
            outcomes.append(confirm(db, db.get(SpatialSurvey, sid), token, User(id=1, email=email, name=email, role="analyst")))
        except HTTPException as e:
            outcomes.append({"status": "rejected", "code": e.status_code, "detail": e.detail})
        except Exception as e:  # pragma: no cover - surfaced in assertion below
            outcomes.append({"status": "error", "detail": repr(e)})
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(e,)) for e in ("alpha@example", "beta@example")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [o for o in outcomes if o.get("status") == "confirmed"]
    rejects = [o for o in outcomes if o.get("status") == "rejected"]
    assert len(wins) == 1, outcomes
    assert len(rejects) == 1 and rejects[0]["code"] == 409, outcomes

    with Session() as db:
        cells, edges = snapshot_state(Session, sid)
        assert [c[3] for c in cells] == [30.0, 31.0, 32.0, 33.0]
        assert len(edges) == 5
        confirmed = db.query(SurveyReplacement).filter_by(survey_id=sid, status="confirmed").all()
        assert len(confirmed) == 1
        assert confirmed[0].confirmed_by == wins[0]["confirmed_by"]
        assert db.query(SurveyReplacement).filter_by(survey_id=sid, status="confirmed").one().confirmed_at


def test_apply_failure_rolls_back_whole_batch(client, Session, sid, monkeypatch):
    before = snapshot_state(Session, sid)
    import src.services.replacement_service as svc
    from src.services.replacement_service import confirm
    from src.models import SpatialSurvey, User

    with Session() as db:
        pre = client.post(f"/surveys/{sid}/replacement/preflight", json=payload(values=(40, 41, 42, 43))).json()
        token = pre["token"]
        replacement_id = pre["replacement_id"]

        def boom(db_, survey_, batch):
            # Half-written state: delete old cells then insert one new cell, then fail.
            db_.query(GridCell).filter_by(survey_id=survey_.id).delete(synchronize_session=False)
            db_.add(GridCell(survey_id=survey_.id, code="G01-01", row=0, col=0, value=99.0, note="部分写入"))
            db_.flush()
            raise RuntimeError("模拟落库中途故障")

        monkeypatch.setattr(svc, "_apply_batch", boom)
        survey = db.get(SpatialSurvey, sid)
        with pytest.raises(RuntimeError):
            confirm(db, survey, token, User(id=1, email="a@x", name="A", role="analyst"))
        db.rollback()

    assert snapshot_state(Session, sid) == before
    with Session() as db:
        row = db.query(SurveyReplacement).filter_by(id=replacement_id).one()
        assert row.status == "preflight"
        assert row.confirmed_by == "" and row.confirmed_at == ""
        assert db.query(NeighborEdge).filter_by(survey_id=sid).count() == 5

    monkeypatch.undo()
    # the untouched batch token still works after the failed attempt
    with Session() as db:
        ok = confirm(db, db.get(SpatialSurvey, sid), token, User(id=1, email="a@x", name="A", role="analyst"))
        assert ok["status"] == "confirmed"
    with Session() as db:
        assert [c.value for c in db.query(GridCell).filter_by(survey_id=sid).order_by(GridCell.code)] == [40.0, 41.0, 42.0, 43.0]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
