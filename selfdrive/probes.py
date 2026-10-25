# -*- coding: utf-8 -*-
"""可执行环境探针。探针只裁判，不为系统生成答案。"""
import re


_NUMBERS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_EXPR_RE = re.compile(r"^[一二两三四五六七八九十]+(?:加[一二两三四五六七八九十]+)+$")


def number_value(token):
    return _NUMBERS.get(token)


def addition_operands(expr):
    if not isinstance(expr, str) or not _EXPR_RE.fullmatch(expr):
        return None
    tokens = expr.split("加")
    values = [_NUMBERS.get(token) for token in tokens]
    if any(value is None for value in values):
        return None
    return values


def verify_addition(expr, result):
    """独立核对封闭中文数词世界；无法表示或解析时返回 None。"""
    operands = addition_operands(expr)
    value = _NUMBERS.get(result)
    if operands is None or value is None:
        return None
    return sum(operands) == value
