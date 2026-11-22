"""出金强制绑票：失败测先行（红 → 绿）。

覆盖《docs/出金绑票命运说明.md》：
  甲路径：缺票/废票 → 正式拨付册零行、笼可用与账户冻结零改、只记拒写审计；
  双令牌同笼出金 → 抢笼锁，后到只留争用痕；
  五页（解笼/核验/对账/账户/审计）票号与冻结数同源对齐。

夹具均在本机文件库（tmp_path 下的 SQLite）跑通，不触碰 data/app.db。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _make_passed_cage(c: TestClient, h: dict, code: str = "CG-TK-001",
                      amount: float = 50000.0) -> dict:
    """假绿事故夹具：造一只核验通过的笼。

    核验票号字段由实现侧生成（attestations.ticket_no）；
    实现落地前本夹具仍可独立复现「无票出金空白进册」事故。
    """
    c.post(
        "/api/bonds",
        headers=h,
        json={
            "party_key": f"P-{code}",
            "party_name": f"绑票测试-{code}",
            "initial_deposit": amount * 2,
        },
    )
    cage = c.post(
        "/api/cages/freeze",
        headers=h,
        json={
            "party_key": f"P-{code}",
            "contract_no": f"HT-{code}",
            "amount": amount,
            "beneficiary": "受益票号",
            "cage_code": code,
        },
    ).json()
    att = c.post(
        "/api/attestations",
        headers=h,
        json={
            "cage_id": cage["id"],
            "outcome": "通过",
            "evidence": "验收单 V-1",
        },
    ).json()
    return {
        "cage": cage,
        "attestation": att,
        "ticket_no": att.get("ticket_no"),
    }


def _ticket_of(c: TestClient, h: dict, attestation_id: int) -> str:
    atts = c.get("/api/attestations", headers=h).json()["items"]
    return [a for a in atts if a["id"] == attestation_id][0]["ticket_no"]


# ---------------------------------------------------------------------------
# 假绿事故夹具（真打解笼写口）：曾复现「无票号 200 出金、拨付册票号栏空白」。
# 绑票强制落地后，同一夹具必须被拒写；本测保留夹具与写口调用，作为回归闸门，
# 防止假绿复活（复活即红灯）。
# ---------------------------------------------------------------------------

def test_fake_green_repro_blank_ticket_enters_ledger_today():
    c = TestClient(app)
    h = _auth(c)
    fx = _make_passed_cage(c, h, code="CG-FG-REPRO")
    cage = fx["cage"]
    r = c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})
    # 旧实现此处为 200 且票号栏空白 —— 现在必须拒写
    assert r.status_code == 400, r.text
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM releases WHERE cage_id=? ORDER BY id DESC LIMIT 1",
            (cage["id"],),
        ).fetchone()
        # 甲路径：正式拨付册零行
        assert row is None
        rej = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='release_reject_no_ticket'"
        ).fetchone()["c"]
        assert rej >= 1


# ---------------------------------------------------------------------------
# 失败测 1：无票号仍能出金的假绿事故 —— 必须变红
# 真打解笼写口 /api/releases，复现「空白票号进拨付册」。
# ---------------------------------------------------------------------------

def test_release_without_ticket_is_refused_zero_write():
    c = TestClient(app)
    h = _auth(c)
    fx = _make_passed_cage(c, h)
    cage = fx["cage"]

    with connect() as conn:
        before = conn.execute(
            "SELECT available, status FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()
        acct_before = conn.execute(
            "SELECT balance, frozen FROM bond_accounts WHERE party_key=?",
            (cage["party_key"],),
        ).fetchone()
        rel_before = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]

    # 缺票（不带票号字段）打写口
    r = c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})
    assert r.status_code == 400, f"缺票竟放行出金：{r.status_code} {r.text}"

    # 空白字符串票号同样拒绝
    r_blank = c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage["id"], "attest_ticket_no": "   "},
    )
    assert r_blank.status_code == 400

    with connect() as conn:
        after = conn.execute(
            "SELECT available, status FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()
        acct_after = conn.execute(
            "SELECT balance, frozen FROM bond_accounts WHERE party_key=?",
            (cage["party_key"],),
        ).fetchone()
        rel_after = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
        # 甲路径：正式拨付册零行
        assert rel_after == rel_before
        rows = conn.execute(
            "SELECT attest_ticket_no FROM releases WHERE cage_id=?", (cage["id"],)
        ).fetchall()
        assert all(row["attest_ticket_no"] for row in rows)
        # 笼可用与状态零改
        assert after["available"] == before["available"]
        assert after["status"] == before["status"]
        # 账户余额与冻结零改
        assert acct_after["balance"] == acct_before["balance"]
        assert acct_after["frozen"] == acct_before["frozen"]
        # 只记拒写审计，且审计落库不随异常回滚
        rej = conn.execute(
            """SELECT * FROM audit_trail
               WHERE action='release_reject_no_ticket' ORDER BY id DESC"""
        ).fetchall()
        assert len(rej) >= 2
        payload = rej[0]["payload"]
        assert cage["cage_code"] in payload


def test_release_with_unknown_ticket_is_refused_zero_write():
    """废票（票号不存在）同样走甲路径零写入。"""
    c = TestClient(app)
    h = _auth(c)
    fx = _make_passed_cage(c, h, code="CG-TK-002")
    cage = fx["cage"]

    with connect() as conn:
        rel_before = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
        avail_before = conn.execute(
            "SELECT available FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()["available"]

    r = c.post(
        "/api/releases",
        headers=h,
        json={"cage_id": cage["id"], "attest_ticket_no": "NO-SUCH-TICKET"},
    )
    assert r.status_code in (400, 409)

    with connect() as conn:
        rel_after = conn.execute("SELECT COUNT(*) c FROM releases").fetchone()["c"]
        avail_after = conn.execute(
            "SELECT available FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()["available"]
        assert rel_after == rel_before
        assert avail_after == avail_before
        bad_ticket_audit = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='release_reject_bad_ticket'"
        ).fetchone()["c"]
        assert bad_ticket_audit >= 1


def test_release_with_other_cage_ticket_is_refused():
    """他笼的通过票不得绑到本笼出金（票号与笼代/笼身份对齐）。"""
    c = TestClient(app)
    h = _auth(c)
    fx1 = _make_passed_cage(c, h, code="CG-TK-A")
    fx2 = _make_passed_cage(c, h, code="CG-TK-B")
    ticket1 = _ticket_of(c, h, fx1["attestation"]["id"])

    r = c.post(
        "/api/releases",
        headers=h,
        json={
            "cage_id": fx2["cage"]["id"],
            "attest_ticket_no": ticket1,
        },
    )
    assert r.status_code == 409
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE cage_id=?",
            (fx2["cage"]["id"],),
        ).fetchone()["c"]
        assert n == 0


# ---------------------------------------------------------------------------
# 失败测 2：占位冻结与对账可用拧着 —— 必须变红
# 命运说明甲路径下，任何拒写都不得新增冻结；账户冻结恒等于活笼可用合计，
# 对账页据此亮的灯必须与账户页一致。
# ---------------------------------------------------------------------------

def test_refused_release_never_twists_frozen_against_reconcile():
    c = TestClient(app)
    h = _auth(c)
    fx = _make_passed_cage(c, h, code="CG-TK-RECON", amount=60000.0)
    cage = fx["cage"]
    party = cage["party_key"]

    # 连续两次缺票拒写
    for _ in range(2):
        r = c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})
        assert r.status_code == 400

    with connect() as conn:
        acct = conn.execute(
            "SELECT balance, frozen FROM bond_accounts WHERE party_key=?",
            (party,),
        ).fetchone()
        live = conn.execute(
            """SELECT COALESCE(SUM(available),0) s FROM cages
               WHERE party_key=? AND status IN
               ('已冻结','核验通过','核验违约','部分解笼','部分罚没')""",
            (party,),
        ).fetchone()["s"]
    # 账户冻结 == 活笼可用：拒写没有制造「冻住却当可用」的占位
    assert abs(float(acct["frozen"]) - float(live)) < 1e-6
    assert float(acct["frozen"]) == 60000.0

    # 对账页（服务读模型）与账户页同源同数
    recon = c.get(f"/api/reconcile/{cage['cage_code']}", headers=h).json()
    proof = c.get(f"/api/proof/{party}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["available"] == 60000.0
    assert recon["released"] == 0.0
    assert proof["frozen_matches_live"] is True
    assert proof["account"]["frozen"] == acct["frozen"]
    assert proof["live_cage_available"] == live


# ---------------------------------------------------------------------------
# 失败测 3：双令牌同笼出金争用 —— 必须变红
# 后到者只留争用痕，不写业务；争用痕独立落库不回滚。
# ---------------------------------------------------------------------------

def test_two_tokens_same_cage_contention_leaves_only_trace():
    c = TestClient(app)
    h1 = _auth(c, "admin")
    h2 = _auth(c, "clerk")
    fx = _make_passed_cage(c, h1, code="CG-TK-LOCK", amount=30000.0)
    cage = fx["cage"]
    ticket = _ticket_of(c, h1, fx["attestation"]["id"])
    lock_key = f"cage:{cage['id']}"

    # admin 先持笼锁不释放，模拟先到者在笼内
    lr = c.post("/api/locks/acquire", headers=h1, json={"lock_key": lock_key})
    assert lr.status_code == 200

    errors: list[Exception] = []

    def clerk_release() -> None:
        try:
            r = c.post(
                "/api/releases",
                headers=h2,
                json={"cage_id": cage["id"], "attest_ticket_no": ticket},
            )
            assert r.status_code == 409, r.text
        except Exception as exc:  # pragma: no cover - 记录线程内断言失败
            errors.append(exc)

    t = threading.Thread(target=clerk_release)
    t.start()
    t.join(timeout=10)
    assert not errors

    # 后到只留争用痕
    cont = c.get("/api/contention", headers=h1).json()["items"]
    hit = [x for x in cont if x["lock_key"] == lock_key and x["actor"] == "clerk"]
    assert len(hit) == 1
    # 争用痕已独立落库（即便后到请求整体失败）
    with connect() as conn:
        persisted = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=? AND actor='clerk'",
            (lock_key,),
        ).fetchone()["c"]
        assert persisted == 1
        # 后到未写任何业务：无拨付行、笼额未动、账户未动
        rels = conn.execute(
            "SELECT COUNT(*) c FROM releases WHERE cage_id=?", (cage["id"],)
        ).fetchone()["c"]
        assert rels == 0
        avail = conn.execute(
            "SELECT available FROM cages WHERE id=?", (cage["id"],)
        ).fetchone()["available"]
        assert avail == 30000.0

    # 先到者随后正常绑票出金成功，证明锁与绑票两道关各自独立
    c.post("/api/locks/release", headers=h1, json={"lock_key": lock_key})
    ok = c.post(
        "/api/releases",
        headers=h1,
        json={"cage_id": cage["id"], "attest_ticket_no": ticket},
    )
    assert ok.status_code == 200, ok.text
    row = ok.json()
    assert row["attest_ticket_no"] == ticket


# ---------------------------------------------------------------------------
# 五页同源：解笼/核验/对账/账户/审计票号与冻结数对齐
# ---------------------------------------------------------------------------

def test_five_pages_share_same_ticket_and_frozen_numbers():
    c = TestClient(app)
    h = _auth(c)
    fx = _make_passed_cage(c, h, code="CG-TK-PAGES", amount=70000.0)
    cage = fx["cage"]
    ticket = _ticket_of(c, h, fx["attestation"]["id"])

    rel = c.post(
        "/api/releases",
        headers=h,
        json={
            "cage_id": cage["id"],
            "amount": 20000.0,
            "attest_ticket_no": ticket,
        },
    )
    assert rel.status_code == 200
    rel_row = rel.json()

    # 核验页数据源：票号源头
    atts = c.get("/api/attestations", headers=h).json()["items"]
    att_hit = [a for a in atts if a["id"] == fx["attestation"]["id"]][0]
    assert att_hit["ticket_no"] == ticket

    # 解笼页数据源：拨付行必带票号且与核验票逐行对齐
    rels = c.get("/api/releases", headers=h).json()["items"]
    rel_hit = [r for r in rels if r["id"] == rel_row["id"]][0]
    assert rel_hit["attest_ticket_no"] == ticket
    assert all(r["attest_ticket_no"] for r in rels)

    # 对账页：平衡且数字与笼面/账户同源
    recon = c.get(f"/api/reconcile/{cage['cage_code']}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["released"] == 20000.0
    assert recon["available"] == 50000.0

    # 账户页：冻结 = 活笼可用（部分解笼后活笼 50000）
    bonds = c.get("/api/bonds", headers=h).json()["items"]
    acct = [b for b in bonds if b["party_key"] == cage["party_key"]][0]
    proof = c.get(f"/api/proof/{cage['party_key']}", headers=h).json()
    assert proof["frozen_matches_live"] is True
    assert acct["frozen"] == proof["account"]["frozen"] == 50000.0

    # 审计页：出金审计载荷带同一票号
    audits = c.get("/api/audit?limit=200", headers=h).json()["items"]
    release_audits = [a for a in audits if a["action"] == "cage_release"]
    assert release_audits and ticket in release_audits[0]["payload"]
