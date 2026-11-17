import sqlite3

import pytest
from fastapi.testclient import TestClient
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c):
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _sub_version(c, h):
    r = c.get("/api/subscriptions", headers=h)
    assert r.status_code == 200
    return r.json()["version"]


def _change_sub(c, h, consumer, action="订阅", version=None):
    v = _sub_version(c, h) if version is None else version
    return c.post("/api/subscriptions", headers=h,
                  json={"consumer": consumer, "action": action, "expected_version": v})


def _add_complete_slice(c, h, slice_no, biz_date="2026-11-04"):
    r = c.post("/api/slices", headers=h, json={
        "slice_no": slice_no, "biz_date": biz_date, "partition_key": "mid",
        "row_count": 10, "expected_count": 10})
    assert r.status_code == 200
    return r.json()["id"]


def test_pipeline():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "日切放行"
    h = _login(c)
    assert len(c.get("/api/slices", headers=h).json()["items"]) >= 1
    # partial cannot release
    bad = c.post("/api/slices/1/release", headers=h, json={"consumer": "ads_order"})
    assert bad.status_code == 409
    sid = _add_complete_slice(c, h, "SL-T-1")
    # 未订阅消费方偷接放行 → 拒，记订阅口径偏了
    sneak = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert sneak.status_code == 409 and "订阅口径偏了" in sneak.json()["detail"]
    # 订阅后同一事务放行 + 推水位
    assert _change_sub(c, h, "ads_x").status_code == 200
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert rel.status_code == 200
    wms = [w for w in c.get("/api/watermarks", headers=h).json()["items"] if w["source_slice"] == "SL-T-1"]
    assert wms and wms[0]["release_id"]
    # 放行详情带口径版本，与订阅目录对读
    rels = c.get("/api/releases", headers=h).json()["items"]
    assert rels[0]["sub_version"] == _sub_version(c, h)


def test_unsubscribed_rejected_and_traced():
    c = TestClient(app)
    h = _login(c)
    sid = _add_complete_slice(c, h, "SL-T-2", "2026-11-05")
    r = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ghost"})
    assert r.status_code == 409 and "订阅口径偏了" in r.json()["detail"]
    rels = c.get("/api/releases", headers=h).json()["items"]
    assert any(x["consumer"] == "ghost" and x["status"] == "拒绝" and "订阅口径偏了" in x["note"] for x in rels)
    aud = c.get("/api/subscriptions/audit", headers=h).json()["items"]
    assert any(a["action"] == "口径偏离拒绝" and a["consumer"] == "ghost" for a in aud)
    assert not any(w["consumer"] == "ghost" for w in c.get("/api/watermarks", headers=h).json()["items"])


def test_removed_consumer_gets_no_watermark():
    c = TestClient(app)
    h = _login(c)
    # 清单删除（退订）ads_order 后，禁止再对其推水位
    assert _change_sub(c, h, "ads_order", action="退订").status_code == 200
    sid = _add_complete_slice(c, h, "SL-T-3", "2026-11-06")
    r = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_order"})
    assert r.status_code == 409 and "订阅口径偏了" in r.json()["detail"]
    wms = c.get("/api/watermarks", headers=h).json()["items"]
    assert not any(w["consumer"] == "ads_order" and w["source_slice"] == "SL-T-3" for w in wms)


def test_contention_second_writer_rejected():
    c = TestClient(app)
    h = _login(c)
    v = _sub_version(c, h)
    first = _change_sub(c, h, "c_a", version=v)
    assert first.status_code == 200
    # 后手拿着过期口径版本改落 → 订阅争用，禁止默成
    second = _change_sub(c, h, "c_b", version=v)
    assert second.status_code == 409 and "订阅争用" in second.json()["detail"]
    aud = c.get("/api/subscriptions/audit", headers=h).json()["items"]
    assert any(a["action"] == "订阅争用" and a["consumer"] == "c_b" for a in aud)
    subs = {s["consumer"]: s["status"] for s in c.get("/api/subscriptions", headers=h).json()["items"]}
    assert subs.get("c_a") == "订阅中" and "c_b" not in subs


def test_rulebook_locked_no_flip():
    c = TestClient(app)
    h = _login(c)
    import app as appmod
    conn = sqlite3.connect(appmod.DB_PATH)
    conn.execute("UPDATE subscription_meta SET semantics='乙：草稿确认后原子切换' WHERE id=1")
    conn.commit()
    conn.close()
    r = c.get("/api/subscriptions", headers=h)
    assert r.status_code == 409 and "禁止改口" in r.json()["detail"]
    sid = _add_complete_slice(c, h, "SL-T-4", "2026-11-07")
    r2 = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_order"})
    assert r2.status_code == 409 and "禁止改口" in r2.json()["detail"]
