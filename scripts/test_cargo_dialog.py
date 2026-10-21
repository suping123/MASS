"""订单助手的货类识别与意图识别验证。

    ./.venv/Scripts/python.exe scripts/test_cargo_dialog.py

起因（用户实测截图）：
  1) 说「西昌到广东 粗加工蚕丝 20t 后天23点整」，助手继续问"运的是什么货"；
     答「农产品 2000」还是问货类；问「有什么货类」它又把同一句反问发一遍。
  2) 答「普货」，助手回「「普货」的运输要点：…」——把槽位答案当成了知识问答，
     货类槽位永远填不上，对话走不下去。

两个根因，都要盯住：
  * 货类词表有三份（落单 / 对话 / 问答），互不同步，"蚕丝""农产品"谁都不认；
  * `classify_question` 用「提到货类 + 没有问号」判知识问答，把"普货"这种
    最标准的槽位回答吃掉了。

所以这里不只测"能识别蚕丝"，还测三份词表真的是同一份、裸词不被截走、
同一项追问第二遍要换说法。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakeredis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import delete  # noqa: E402

from sca import cargo as C  # noqa: E402
from sca.platform import models as M  # noqa: E402
from sca.platform import security  # noqa: E402
from sca.platform.agents import slots as S  # noqa: E402
from sca.platform.db import session_as  # noqa: E402
from sca.platform.main import app  # noqa: E402
from sca.platform.services import advice  # noqa: E402
from _captcha_solver import solve_captcha  # noqa: E402

FAILS: list[str] = []
PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILS.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def login(c: TestClient, account: str) -> str:
    r = c.post("/api/auth/code", json={"account": account, "purpose": "login"})
    code, cid = solve_captcha(r.json())
    r = c.post("/api/auth/login", json={"account": account, "password": "123456",
                                       "code": code, "cid": cid})
    assert r.status_code == 200, f"{account}: {r.text}"
    return (r.json() or {})["token"]


def cleanup_sessions(sids: list[str]) -> None:
    if not sids:
        return
    db = session_as("app")
    try:
        db.execute(delete(M.ChatMessage).where(M.ChatMessage.session_id.in_(sids)))
        db.execute(delete(M.ChatSession).where(M.ChatSession.id.in_(sids)))
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- 词典唯一性
def stage_dict() -> None:
    print("\n[1] 货类词典：三处共用一份")
    from sca.intake import _CARGO_RULES

    intake_names = {name for _, name in _CARGO_RULES}
    check("落单规则的类别就是词典的类别", intake_names == set(C.CATEGORIES),
          f"{sorted(intake_names)} vs {sorted(C.CATEGORIES)}")

    # 每个别名在三个入口的表现必须一致：intake 落单、slots 判"提没提"、advice 认类
    mismatch: list[str] = []
    for alias, name in C.ALIAS_INDEX:
        if C.find_cargo(alias) != name:
            mismatch.append(f"find_cargo({alias})={C.find_cargo(alias)}≠{name}")
        if not S.cargo_mentioned(alias):
            mismatch.append(f"slots 不认 {alias}")
        if advice.find_cargo(alias) != name:
            mismatch.append(f"advice.find_cargo({alias})={advice.find_cargo(alias)}≠{name}")
    check("每个别名在三处口径一致", not mismatch, "; ".join(mismatch[:4]))

    check("「粗加工蚕丝」认成纺织品", C.find_cargo("粗加工蚕丝") == "纺织品",
          str(C.find_cargo("粗加工蚕丝")))
    check("「农产品」认成普货", C.find_cargo("农产品") == "普货",
          str(C.find_cargo("农产品")))
    check("「农产品」归并时有说明", "普货" in C.guess_note("农产品"),
          C.guess_note("农产品"))
    check("每个标准类名都能认回自己",
          all(C.find_cargo(n) == n for n in C.CATEGORIES),
          str([n for n in C.CATEGORIES if C.find_cargo(n) != n]))

    # 长词优先：医药冷链不能被"冷链"吞掉，危化品不能落到别的类
    check("「医药冷链」不被「冷链」吞掉", C.find_cargo("医药冷链") == "医药冷链",
          str(C.find_cargo("医药冷链")))
    check("「危化品」认成危险品", C.find_cargo("危化品") == "危险品",
          str(C.find_cargo("危化品")))

    flags = C.flags("冷链")
    check("冷链自动带温控标志", flags["temp_ctrl"] and not flags["hazmat"], str(flags))
    flags = C.flags("危险品")
    check("危险品自动带危品标志", flags["hazmat"] and not flags["temp_ctrl"], str(flags))


# ---------------------------------------------------------------- 意图识别
def stage_intent() -> None:
    print("\n[2] 意图识别：裸货类词是回答，不是提问")
    bare = {"普货": "普货", "冷链": "冷链", "危险品": "危险品", "纺织品": "纺织品",
            "农产品 2000": "普货", "普货 20吨": "普货"}
    bad = [t for t in bare if advice.classify_question(t) != "none"]
    check("裸货类词不当成知识问答", not bad, str(bad))

    check("「有什么货类」判成 cargo_options",
          advice.classify_question("有什么货类") == "cargo_options",
          advice.classify_question("有什么货类"))
    check("「货类有哪些」也判成 cargo_options",
          advice.classify_question("货类有哪些") == "cargo_options",
          advice.classify_question("货类有哪些"))

    # 真正的提问不能被误伤
    care = {"冷链要注意什么": "cargo_care", "普货有什么要求": "cargo_care",
            "危化品能拉吗": "cargo_what"}
    wrong = {t: advice.classify_question(t) for t, want in care.items()
             if advice.classify_question(t) != want}
    check("真正的货类提问仍走知识问答", not wrong, str(wrong))

    qa = advice.answer("有什么货类")
    check("货类清单列出全部类别",
          all(n in qa["text"] for n in C.CATEGORIES),
          f"缺 {[n for n in C.CATEGORIES if n not in qa['text']]}")
    check("货类清单带 options 供前端点选",
          qa.get("options") == list(C.CATEGORIES), str(qa.get("options")))


# ---------------------------------------------------------------- 槽位解析
def stage_slots() -> None:
    print("\n[3] 槽位解析：一句话能抽全，缺的才问")
    s = S.apply_parse({}, "西昌到广东 粗加工蚕丝 20t 后天23点整")
    check("货类抽对了（蚕丝→纺织品）", s.get("cargo_type") == "纺织品",
          str(s.get("cargo_type")))
    check("重量抽对了", s.get("weight_t") == 20.0, str(s.get("weight_t")))
    check("时效保住了时刻（不是只剩「后天」）",
          s.get("required_by") == "后天23:00前", str(s.get("required_by")))
    check("这时只缺预算一项",
          S.missing_slots(s) == ["budget_max"], str(S.missing_slots(s)))

    s2 = S.apply_parse(s, "农产品 2000")
    check("预算填上了", s2.get("budget_max") == "2000", str(s2.get("budget_max")))
    check("补齐后没有缺失", S.missing_slots(s2) == [], str(S.missing_slots(s2)))

    # 编号回答：只在"正在问货类"时生效
    s3 = S.apply_parse({"_asking": "cargo_type", "weight_t": 20.0}, "5")
    check("回编号「5」= 危险品", s3.get("cargo_type") == "危险品",
          str(s3.get("cargo_type")))
    check("编号选危险品自动带危品标志", s3.get("hazmat") is True, str(s3.get("hazmat")))
    s4 = S.apply_parse({"weight_t": 20.0}, "5")
    check("不在问货类时，「5」不被当编号", s4.get("cargo_type") is None,
          str(s4.get("cargo_type")))

    # 改口：句中修正
    s5 = S.apply_parse({"cargo_type": "纺织品", "origin": "西昌", "dest": "广州",
                        "weight_t": 20.0, "required_by": "后天", "budget_max": "2000"},
                       "货类改成农产品", overwrite=S.is_correction("货类改成农产品"))
    check("「货类改成农产品」能覆盖", s5.get("cargo_type") == "普货",
          str(s5.get("cargo_type")))
    check("改口有说明", bool(s5.get("_cargo_note")), str(s5.get("_cargo_note")))
    check("「不着急」不算修正", S.is_correction("不着急") is False)
    check("「目的地换成南京」算修正", S.is_correction("目的地换成南京") is True)

    # 省份只给到省 -> 复述里必须说明按省会算
    s6 = S.apply_parse({}, "西昌到广东 纺织品 20吨 后天23点整")
    text = S.summary_text(s6)
    check("复述说明「广东按省会广州算」",
          "广东" in text and "广州" in text and "省会" in text, text[-90:])


# ---------------------------------------------------------------- 端到端对话
def stage_chat(c: TestClient, H: dict, created: list[str]) -> None:
    print("\n[4] 端到端对话（真实 HTTP + 真实库）")

    # 4.1 截图1 的第一句：不该再问货类
    r = c.post("/api/shipper/chat",
               json={"text": "西昌到广东 粗加工蚕丝 20t 后天23点整"}, headers=H).json()
    created.append(r["session_id"])
    check("第一句就认出货类，不再问「运的是什么货」",
          "运的是什么货" not in r["reply"], r["reply"][:120])
    check("槽位里货类是纺织品", (r.get("slots") or {}).get("cargo_type") == "纺织品",
          str((r.get("slots") or {}).get("cargo_type")))
    check("只追问预算", (r.get("missing") or []) == ["budget_max"],
          str(r.get("missing")))

    # 4.2 「有什么货类」给清单 + 可点选项
    r2 = c.post("/api/shipper/chat",
                json={"session_id": r["session_id"], "text": "有什么货类"},
                headers=H).json()
    check("问货类得到清单而不是同一句反问",
          "可选货类" in r2["reply"] and "纺织品" in r2["reply"], r2["reply"][:80])
    check("清单带 options", len(((r2.get("data") or {}).get("options")) or []) == len(C.CATEGORIES),
          str((r2.get("data") or {}).get("options")))
    check("问货类没把已填的货类冲掉",
          (r2.get("slots") or {}).get("cargo_type") == "纺织品",
          str((r2.get("slots") or {}).get("cargo_type")))

    # 4.3 截图2：答「普货」必须进槽位，不能变成知识问答
    r3 = c.post("/api/shipper/chat",
                json={"text": "石家庄到南京 20吨 明天18点前到 预算1.2万"},
                headers=H).json()
    created.append(r3["session_id"])
    check("这句只缺货类", (r3.get("missing") or []) == ["cargo_type"],
          str(r3.get("missing")))
    r4 = c.post("/api/shipper/chat",
                json={"session_id": r3["session_id"], "text": "普货"}, headers=H).json()
    check("答「普货」不再回「运输要点」",
          "运输要点" not in r4["reply"], r4["reply"][:120])
    check("答「普货」后货类填上了",
          (r4.get("slots") or {}).get("cargo_type") == "普货",
          str((r4.get("slots") or {}).get("cargo_type")))
    check("答「普货」后信息齐了，进入核对",
          (r4.get("missing") or []) == [] and "确认" in r4["reply"],
          f"missing={r4.get('missing')} reply={r4['reply'][:80]}")

    # 4.4 同一项问第二遍要换说法：给可选值，不是复读
    r5 = c.post("/api/shipper/chat",
                json={"text": "成都到重庆 8吨 明天18点前到 预算8000"}, headers=H).json()
    created.append(r5["session_id"])
    check("这句缺货类", (r5.get("missing") or []) == ["cargo_type"],
          str(r5.get("missing")))
    check("第一次追问保持简短（只列反问，不糊一屏清单）",
          "可选货类" not in r5["reply"], r5["reply"][:80])
    r6 = c.post("/api/shipper/chat",
                json={"session_id": r5["session_id"], "text": "就是那个"}, headers=H).json()
    check("用户答不出、第二次追问改给可选货类清单",
          "可选货类" in r6["reply"], r6["reply"][:120])
    check("第二次追问的回复带 options",
          len(((r6.get("data") or {}).get("options")) or []) == len(C.CATEGORIES),
          str((r6.get("data") or {}).get("options")))
    r7 = c.post("/api/shipper/chat",
                json={"session_id": r5["session_id"], "text": "你看着办"}, headers=H).json()
    check("第三次追问仍然给清单（不退回复读）",
          "可选货类" in r7["reply"], r7["reply"][:80])

    # 4.5 回编号能收口
    r8 = c.post("/api/shipper/chat",
                json={"session_id": r5["session_id"], "text": "2"}, headers=H).json()
    check("回编号「2」= 冷链",
          (r8.get("slots") or {}).get("cargo_type") == "冷链",
          str((r8.get("slots") or {}).get("cargo_type")))
    check("回编号后信息齐了", (r8.get("missing") or []) == [], str(r8.get("missing")))


def main() -> int:
    security._redis = fakeredis.FakeRedis(decode_responses=True)
    with TestClient(app) as c:
        H = {"Authorization": "Bearer " + login(c, "shipper1")}
        created: list[str] = []
        try:
            stage_dict()
            stage_intent()
            stage_slots()
            stage_chat(c, H, created)
        finally:
            cleanup_sessions(created)
            print(f"\n[清理] 删除本次会话 {len(created)} 个")
    total = PASSED + len(FAILS)
    print(f"\n{'=' * 60}")
    if FAILS:
        print(f"FAIL：{len(FAILS)}/{total} 项未通过")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"PASS：{total} 项检查全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
