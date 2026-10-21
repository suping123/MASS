"""检索用的文本切分与词项（唯一口径）。

为什么单独一个模块
------------------
召回、重排、评测三处都要把中文切成 2-gram、英文切成词。这个项目已经吃过
"同一份词表抄成三份、不同步"的亏（货类词典曾经三份，现象是"用户说蚕丝
系统听不懂，但它答得出纺织品"）。检索侧同理：召回用 2-gram、重排用别的切法，
两边的"命中"就不是同一个概念，重排分数会看着像随机。

所以只留这一份，谁要切词都从这里取。
"""
from __future__ import annotations

import re
from typing import List, Set

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> List[str]:
    """英文/数字按词，中文按相邻两字（2-gram）。

    中文不做分词：没装词典时按 2-gram 退而求其次，虽会产生"的运"这种噪声，
    但对短查询足够——且它是确定性的，测试可复现。
    """
    text = text or ""
    toks: List[str] = _TOKEN_RE.findall(text.lower())
    cn = _CJK_RE.findall(text)
    for i in range(len(cn) - 1):
        toks.append(cn[i] + cn[i + 1])
    return toks


def terms(text: str) -> Set[str]:
    """去重词项集合。算覆盖率时用这个——重复出现的"冷链"只应算命中一次。"""
    return set(tokenize(text))
