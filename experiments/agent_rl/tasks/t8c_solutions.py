"""t8-007/008 的参考解法（依赖链对照实验专用）。

注意：这两组任务的测试数据较少，测试正文中的字符串
会被拼成实际的辅助字符，以避免污染测试用例。
"""

_FLAT = {
    "flat/upper.py": '"""转大写"""\n\n\ndef upper(text):\n    return (text or "").upper()\n',
    "flat/reverse.py": '"""反转"""\n\n\ndef reverse(text):\n    return (text or "")[::-1]\n',
    "flat/count.py": '"""计数"""\n\n\ndef count_words(text):\n    return len((text or "").split())\n',
    "flat/strip.py": '"""去空白"""\n\n\ndef strip_all(text):\n    return (text or "").strip()\n',
}

_CHAIN = {
    "chain/tokenizer.py": '"""分词"""\n\n\ndef tokenize(text):\n    return (text or "").split()\n',
    "chain/lower.py": '"""小写化"""\n\nfrom chain.tokenizer import tokenize\n\n\ndef lower_tokens(text):\n    return [t.lower() for t in tokenize(text)]\n',
    "chain/filter.py": '"""过滤"""\n\nfrom chain.lower import lower_tokens\n\n\ndef drop_stopwords(text, stop):\n    stop = {s.lower() for s in (stop or set())}\n    return [t for t in lower_tokens(text) if t not in stop]\n',
    "chain/join.py": '"""拼接"""\n\nfrom chain.filter import drop_stopwords\n\n\ndef normalize(text, stop):\n    return "-".join(drop_stopwords(text, stop))\n',
}

T8C_SOLUTIONS = {
    "t8-007": _FLAT,
    "t8-008": _CHAIN,
}
