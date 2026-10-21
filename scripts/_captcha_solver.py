"""测试专用：从 Redis 直接解出图形验证码答案。

生产路径里答案永远不会出现在任何响应中——那正是"图形验证码"的意义。
测试是白盒：拿 cid 去 Redis 读答案，再走完整的"提交账号密码 + 图形码"链路，
被测代码与真实用户完全一致，只是"认图"这一步由脚本代替人眼完成。
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from sca.platform import security

CAP_KEY = "sca:cap:{cid}"


def solve_captcha(resp: Dict[str, Any] | None) -> Tuple[str, str]:
    """入参是 /api/auth/code 或 /api/auth/captcha 的响应，返回 (答案, cid)。"""
    cid = str((resp or {}).get("cid") or "")
    if not cid:
        return "", ""
    r = security._redis
    raw = r.get(CAP_KEY.format(cid=cid)) if r is not None else None
    return str(raw or ""), cid
