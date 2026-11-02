"""新增长程任务的参考解法（t8-004/005/006）。"""

T8B_SOLUTIONS = {
    "t8-004": {
        "config/reader.py": '''"""配置读取"""


def read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()
''',
        "config/parser.py": '''"""解析"""


def parse(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out
''',
        "config/coerce.py": '''"""类型转换"""


def coerce(value):
    if not isinstance(value, str):
        return value
    lowered = value.strip().lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value
''',
        "config/merge.py": '''"""合并"""


def merge(base, override):
    out = dict(base or {})
    out.update(override or {})
    return out
''',
        "config/loader.py": '''"""加载器门面"""

from config.coerce import coerce
from config.merge import merge
from config.parser import parse
from config.reader import read_text


def load(path, defaults=None):
    raw = parse(read_text(path))
    typed = {k: coerce(v) for k, v in raw.items()}
    return merge(defaults or {}, typed)
''',
    },
    "t8-005": {
        "cache/node.py": '''"""链表节点"""


class Node:
    def __init__(self, key, value):
        self.key = key
        self.value = value
        self.prev = None
        self.next = None
''',
        "cache/dlist.py": '''"""双向链表"""


class DList:
    def __init__(self):
        self.head = None
        self.tail = None

    def push_front(self, node):
        node.prev = None
        node.next = self.head
        if self.head:
            self.head.prev = node
        self.head = node
        if self.tail is None:
            self.tail = node

    def remove(self, node):
        if node.prev:
            node.prev.next = node.next
        else:
            self.head = node.next
        if node.next:
            node.next.prev = node.prev
        else:
            self.tail = node.prev
        node.prev = node.next = None

    def pop_back(self):
        if self.tail is None:
            return None
        node = self.tail
        self.remove(node)
        return node
''',
        "cache/lru.py": '''"""LRU 缓存"""

from cache.dlist import DList
from cache.node import Node


class LRUCache:
    def __init__(self, capacity):
        self.capacity = capacity
        self.map = {}
        self.list = DList()

    def get(self, key):
        node = self.map.get(key)
        if node is None:
            return None
        self.list.remove(node)
        self.list.push_front(node)
        return node.value

    def put(self, key, value):
        node = self.map.get(key)
        if node is not None:
            node.value = value
            self.list.remove(node)
            self.list.push_front(node)
            return
        if len(self.map) >= self.capacity:
            evicted = self.list.pop_back()
            if evicted is not None:
                del self.map[evicted.key]
        node = Node(key, value)
        self.map[key] = node
        self.list.push_front(node)
''',
        "cache/ttl.py": '''"""带 TTL 的缓存"""


class TTLCache:
    def __init__(self, capacity, ttl_at):
        self.capacity = capacity
        self.ttl_at = ttl_at
        self.store = {}

    def get(self, key, now):
        entry = self.store.get(key)
        if entry is None:
            return None
        value, stored_at = entry
        if now - stored_at > self.ttl_at:
            del self.store[key]
            return None
        return value

    def put(self, key, value, now):
        if len(self.store) >= self.capacity and key not in self.store:
            oldest = min(self.store, key=lambda k: self.store[k][1])
            del self.store[oldest]
        self.store[key] = (value, now)
''',
        "cache/index.py": '''"""倒排索引"""


class Index:
    def __init__(self):
        self.docs = {}

    def add(self, doc_id, text):
        self.docs[doc_id] = (text or "").lower().split()

    def search(self, word):
        target = (word or "").lower()
        return sorted(doc_id for doc_id, words in self.docs.items() if target in words)
''',
    },
    "t8-006": {
        "textutil/slug.py": '''"""slug"""

import re


def slugify(text):
    lowered = (text or "").lower()
    cleaned = re.sub(r"[^a-z0-9\\s-]", "", lowered)
    return re.sub(r"[\\s-]+", "-", cleaned).strip("-")
''',
        "textutil/wrap.py": '''"""折行"""


def wrap(text, width):
    if width <= 0:
        return [text]
    lines = []
    current = ""
    for word in (text or "").split():
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines
''',
        "textutil/truncate.py": '''"""截断"""


def truncate(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"[:limit]
    return text[: limit - 1] + "…"
''',
    },
}
