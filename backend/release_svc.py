"""解笼拨付：核验通过且绑有效核验票后，才把笼内可用额拨付给受益人。

出金绑票命运（见 docs/出金绑票命运说明.md，2026-11-22 锁死）：
- 强制绑票：请求必须带非空 attest_ticket_no，且票号属于本笼、同笼代、结论通过。
- 缺票/废票只走甲路径：releases 零行、笼可用与账户冻结零改，仅记拒写审计。
- 同笼并发抢 cage:{id} 笼锁；后到只留争用痕，不写业务。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, try_acquire
from models import ReleaseIn

router = APIRouter(tags=["release"])


@router.get("/api/releases")
def list_releases(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT r.*, c.cage_code, c.contract_no
               FROM releases r
               JOIN cages c ON c.id=r.cage_id
               ORDER BY r.id DESC LIMIT 200"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/releases")
def do_release(body: ReleaseIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"cage:{body.cage_id}"
    conn = connect()
    try:
        # 抢笼锁：失败者的争用痕在 try_acquire 内已写入，独立提交后才抛 409
        if not try_acquire(conn, lock_key, user):
            conn.commit()
            raise HTTPException(409, "争用：后到只记争用，不写业务")
        try:
            cage = conn.execute(
                "SELECT * FROM cages WHERE id=?", (body.cage_id,)
            ).fetchone()
            if not cage:
                raise HTTPException(404, "笼不存在")

            ticket_raw = body.attest_ticket_no
            ticket_no = ticket_raw.strip() if isinstance(ticket_raw, str) else ""
            now = now_iso()

            # ---- 甲路径：缺票零写入，只记拒写审计 ----
            if not ticket_no:
                write_audit(
                    conn,
                    user,
                    "release_reject_no_ticket",
                    cage["cage_code"],
                    {
                        "cage_id": body.cage_id,
                        "cage_code": cage["cage_code"],
                        "cage_generation": int(cage["generation"]),
                        "attest_ticket_no": ticket_raw,
                        "amount": body.amount,
                        "reason": "出金缺核验票号，拨付册零写入",
                    },
                )
                conn.commit()
                raise HTTPException(400, "出金须绑定核验票号，缺票已拒写")

            if cage["status"] != "核验通过":
                raise HTTPException(409, "仅核验通过的笼可解笼拨付")

            # ---- 票号必须真实存在且与本笼、本笼代、通过结论对齐 ----
            att = conn.execute(
                """SELECT * FROM attestations
                   WHERE ticket_no=? AND cage_id=? AND attest_gen=?
                   ORDER BY id DESC LIMIT 1""",
                (ticket_no, body.cage_id, int(cage["generation"])),
            ).fetchone()
            if not att or att["outcome"] != "通过":
                write_audit(
                    conn,
                    user,
                    "release_reject_bad_ticket",
                    cage["cage_code"],
                    {
                        "cage_id": body.cage_id,
                        "cage_code": cage["cage_code"],
                        "cage_generation": int(cage["generation"]),
                        "attest_ticket_no": ticket_no,
                        "amount": body.amount,
                        "reason": "票号不存在或与本笼/笼代/通过结论不符",
                    },
                )
                conn.commit()
                raise HTTPException(409, "核验票号无效，出金已拒写")

            # 同一票号只允许一次正式拨付，杜绝重复绑票出金
            dup = conn.execute(
                "SELECT 1 FROM releases WHERE attest_ticket_no=?",
                (ticket_no,),
            ).fetchone()
            if dup:
                write_audit(
                    conn,
                    user,
                    "release_reject_bad_ticket",
                    cage["cage_code"],
                    {
                        "cage_id": body.cage_id,
                        "cage_code": cage["cage_code"],
                        "cage_generation": int(cage["generation"]),
                        "attest_ticket_no": ticket_no,
                        "amount": body.amount,
                        "reason": "该核验票号已绑定过拨付",
                    },
                )
                conn.commit()
                raise HTTPException(409, "核验票号已使用，禁止重复出金")

            # 核验结论必须对齐当前笼代
            gen_att = conn.execute(
                """SELECT * FROM attestations
                   WHERE cage_id=? AND attest_gen=? AND outcome='通过'
                   ORDER BY id DESC LIMIT 1""",
                (body.cage_id, int(cage["generation"])),
            ).fetchone()
            if not gen_att:
                raise HTTPException(409, "缺少与笼代对齐的通过核验")
            avail = float(cage["available"])
            amount = float(body.amount) if body.amount is not None else avail
            if amount <= 0:
                raise HTTPException(400, "拨付金额须大于 0")
            if amount > avail + 1e-9:
                raise HTTPException(409, "拨付金额超过笼可用额")
            payee = body.payee or cage["beneficiary"]
            gen = int(cage["generation"])
            ref = f"REL-{cage['cage_code']}-g{gen}-{int(now.replace(':', '').replace('-', '').replace('T', '')[-8:])}"
            cur = conn.execute(
                """INSERT INTO releases(cage_id,amount,payee,cage_generation,status,release_ref,attest_ticket_no,created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    body.cage_id,
                    amount,
                    payee,
                    gen,
                    "已拨付",
                    ref,
                    ticket_no,
                    now,
                ),
            )
            new_avail = round(avail - amount, 6)
            new_status = "已解笼" if new_avail <= 1e-9 else "部分解笼"
            conn.execute(
                "UPDATE cages SET available=?, status=? WHERE id=?",
                (max(new_avail, 0.0), new_status, body.cage_id),
            )
            # 账户：冻结减少，余额减少（资金离开系统）
            conn.execute(
                """UPDATE bond_accounts
                   SET frozen = frozen - ?, balance = balance - ?
                   WHERE party_key=?""",
                (amount, amount, cage["party_key"]),
            )
            write_audit(
                conn,
                user,
                "cage_release",
                cage["cage_code"],
                {
                    "release_id": cur.lastrowid,
                    "amount": amount,
                    "payee": payee,
                    "cage_generation": gen,
                    "release_ref": ref,
                    "attest_ticket_no": ticket_no,
                },
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM releases WHERE id=?", (cur.lastrowid,)
            ).fetchone()
            return dict(row)
        finally:
            release(conn, lock_key, user)
            conn.commit()
    finally:
        conn.close()
