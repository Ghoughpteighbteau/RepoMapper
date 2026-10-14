"""Persisted rankings: `.repomap/cache/ranking.v1` (the unseeded ranking) and `.repomap/cache/graph.v1` (the base graph
every ranking starts from), on disk, plus small in-process memos for the MCP server.

The base graph (graph.v1) is what the files give whatever the seeds are: reference edges as per-name counts, import
edges, definitions and their lines. Chat files, mentioned files and identifiers only reweight edges and set the
personalization, which a seeded build redoes (vectorized) on top of the stored graph. Its key is the ranking key
without what only the history part reads (HEAD, the day) or the outline does, and it covers the stamps of every file
considered, chat files included. It is stored by any build, seeded or not, unless a file was missing or failed to parse.

Building the ranking (parsing is cached per file already; this is bucketing tags, the reference graph, PageRank) takes
seconds on a big repo, and for an unseeded map (no chat files, mentions) its result only depends on the files, the
settings and git HEAD. So the result is stored under a key covering all of that and reused while the key matches.

A hit must give exactly what a fresh build would, so the key covers everything build_ranking reads: the extractor
fingerprint, the (path, mtime_ns, size) of every listed file, the ranking-relevant settings, areas.toml entries, HEAD
(history signals), and the day (activity decays with age). Files read but not listed (JS/TS barrels that import
resolution opens, or probes that found nothing) are recorded with their stamps in the entry and re-checked on load.

File layout: one header line `repomap-ranking v<VERSION> <key>`, then a zlib-compressed pickle of the payload. It is
written to a temp file and renamed into place, so a reader sees the old or the new file, never half of one. Anything
unexpected on read (other key or version, truncated or garbled data, mismatched fields) is a miss, never an error.
"""
import hashlib
import os
import pickle
import tempfile
import threading
import zlib
from collections import OrderedDict
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from repomapper.graph import BaseGraph, OutlinePairs, PairSums, RefGraph

RANKING_CACHE_VERSION = 1
RANKING_CACHE_FILE = f"ranking.v{RANKING_CACHE_VERSION}"
GRAPH_CACHE_VERSION = 1
GRAPH_CACHE_FILE = f"graph.v{GRAPH_CACHE_VERSION}"
ACTIVITY_BUCKET_SECONDS = 86400   # history activity decays continuously; a stored ranking is good for the day it was made
MEMO_ROOTS = 4                    # roots kept in the in-process memo

Stamp = Optional[Tuple[int, int]]   # (mtime_ns, size); None for a file that doesn't exist


def stamp_of(path: str) -> Stamp:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def make_key(parts: Sequence[Any], stamps: Sequence[Tuple[str, Stamp]]) -> str:
    """Hex digest of the settings-like parts (anything with a stable repr) and the (path, stamp) of every file."""
    h = hashlib.sha256()
    h.update(f"v{RANKING_CACHE_VERSION}".encode())
    for part in parts:
        h.update(b"\x00")
        h.update(repr(part).encode())
    h.update(b"\x00files")
    for path, stamp in sorted(stamps):
        h.update(f"\x00{path}\x00{stamp}".encode())
    return h.hexdigest()


def reads_valid(root: str, reads: Sequence[Tuple[str, Stamp]]) -> bool:
    """Whether the unlisted files a build read (barrels, probes that found nothing) are still as they were."""
    return all(stamp_of(os.path.join(root, rel)) == stamp for rel, stamp in reads)


def pack(ranking, reads: Sequence[Tuple[str, Stamp]]) -> Dict[str, Any]:
    """The payload of an unseeded ranking, with the big dicts as arrays. Needs ranking.pair_edges."""
    pairs = ranking.pair_edges
    nodes = pairs.nodes
    node_index = {node: i for i, node in enumerate(nodes)}
    names: Dict[str, int] = {}
    ident_file, ident_name, ident_value = [], [], []
    for (rel, name), value in ranking.ident_ranks.items():
        ident_file.append(node_index[rel])
        ident_name.append(names.setdefault(name, len(names)))
        ident_value.append(value)
    return {
        "nodes": nodes,
        "ranks": np.array([ranking.file_ranks[node] for node in nodes], dtype=np.float64),
        "ident": (np.array(ident_file, dtype=np.int32), np.array(ident_name, dtype=np.int32),
                  np.array(ident_value, dtype=np.float64), list(names)),
        "included": ranking.included,
        "definitions": ranking.definitions,
        "nested": ranking.nested,
        "types": ranking.types,
        "report": asdict(ranking.report),
        "named": _pack_pairs(pairs.named),
        "imports": _pack_pairs(pairs.imports),
        "reads": list(reads),
    }


def _pack_pairs(sums: PairSums):
    return sums.src.astype(np.int32), sums.dst.astype(np.int32), sums.weight


def _unpack_pairs(packed) -> PairSums:
    src, dst, weight = packed
    return PairSums(src.astype(np.int64), dst.astype(np.int64), weight)


def unpack(payload: Dict[str, Any]):
    """Rebuild the Ranking from a payload; raises on anything that doesn't fit."""
    from repomapper.repomap_class import FileReport, Ranking
    nodes: List[str] = payload["nodes"]
    ident_file, ident_name, ident_value, names = payload["ident"]
    ident_ranks = {(nodes[f], names[n]): v
                   for f, n, v in zip(ident_file.tolist(), ident_name.tolist(), ident_value.tolist())}
    empty = np.zeros(0, dtype=np.int64)
    # Only the pair sums the outline reads are kept, so the graph is a stub that holds the node list
    graph = RefGraph(nodes, empty, empty, np.zeros(0), empty, [])
    pairs = OutlinePairs(nodes, _unpack_pairs(payload["named"]), _unpack_pairs(payload["imports"]))
    ranks = payload["ranks"]
    if len(ranks) != len(nodes):
        raise ValueError("ranks and nodes differ")
    return Ranking(graph, dict(zip(nodes, ranks.tolist())), ident_ranks, payload["included"], payload["definitions"],
                   FileReport(**payload["report"]), set(), set(), set(), payload["nested"], payload["types"],
                   pairs), [tuple(read) for read in payload["reads"]]


def cache_path(root: str, cache_dir: str) -> str:
    return os.path.join(root, cache_dir, RANKING_CACHE_FILE)


def graph_cache_path(root: str, cache_dir: str) -> str:
    return os.path.join(root, cache_dir, GRAPH_CACHE_FILE)


def header(key: str) -> bytes:
    return f"repomap-ranking v{RANKING_CACHE_VERSION} {key}\n".encode()


def graph_header(key: str) -> bytes:
    return f"repomap-graph v{GRAPH_CACHE_VERSION} {key}\n".encode()


def read_payload(path: str, expected_header: bytes, unpack_fn):
    """unpack_fn(payload) of the file if it starts with expected_header, else None. Never raises."""
    try:
        with open(path, "rb") as f:
            if f.readline() != expected_header:
                return None
            payload = pickle.loads(zlib.decompress(f.read()))
        return unpack_fn(payload)
    except Exception:   # corrupt, torn, other version or fields: recompute
        return None


def load(path: str, key: str):
    """(Ranking, reads) from the file if it holds this key, else None. Never raises."""
    return read_payload(path, header(key), unpack)


def write_payload(path: str, head: bytes, payload: Dict[str, Any]) -> None:
    """Write atomically (temp file in the same directory, then rename); failures are ignored."""
    tmp = None
    try:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        body = zlib.compress(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL), 1)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(head)
            f.write(body)
        os.replace(tmp, path)
        tmp = None
    except Exception:
        pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def store(path: str, key: str, payload: Dict[str, Any]) -> None:
    write_payload(path, header(key), payload)


def pack_graph(base: BaseGraph) -> Dict[str, Any]:
    """The base graph's fields as a payload (arrays pickle compactly)."""
    return {field: getattr(base, field) for field in BaseGraph.__dataclass_fields__}


def unpack_graph(payload: Dict[str, Any]) -> BaseGraph:
    """Rebuild the BaseGraph from a payload; raises on anything that doesn't fit."""
    if set(payload) != set(BaseGraph.__dataclass_fields__):
        raise ValueError("fields differ")
    base = BaseGraph(**payload)
    edges, imports, defs = (len(base.src), len(base.import_src), len(base.def_file))
    if not (edges == len(base.dst) == len(base.name_id) == len(base.counts)
            and imports == len(base.import_dst) == len(base.import_weight)
            and defs == len(base.def_line) == len(base.def_name)
            and len(base.names) == len(base.name_definers) == len(base.name_mul)):
        raise ValueError("array lengths differ")
    n = len(base.nodes)
    for array, limit in ((base.src, n), (base.dst, n), (base.import_src, n), (base.import_dst, n),
                         (base.name_id, len(base.names)), (base.def_file, len(base.included)),
                         (base.def_name, len(base.def_names))):
        if len(array) and (array.min() < 0 or array.max() >= limit):
            raise ValueError("index out of range")
    base.reads = [tuple(read) for read in base.reads]
    return base


def load_graph(path: str, key: str) -> Optional[BaseGraph]:
    """The BaseGraph from the file if it holds this key, else None. Never raises."""
    return read_payload(path, graph_header(key), unpack_graph)


def store_graph(path: str, key: str, base: BaseGraph) -> None:
    write_payload(path, graph_header(key), pack_graph(base))


class Memo:
    """In-process memo for long-lived callers (the MCP server): one entry per root, least recently used dropped."""

    def __init__(self):
        self.entries: "OrderedDict[str, Tuple[str, Any, list]]" = OrderedDict()
        self.lock = threading.Lock()

    def get(self, root: str, key: str):
        """(value, reads) remembered for root under key, else None."""
        with self.lock:
            entry = self.entries.get(root)
            if entry is None or entry[0] != key:
                return None
            self.entries.move_to_end(root)
            return entry[1], entry[2]

    def put(self, root: str, key: str, value, reads: list) -> None:
        with self.lock:
            self.entries[root] = (key, value, reads)
            self.entries.move_to_end(root)
            while len(self.entries) > MEMO_ROOTS:
                self.entries.popitem(last=False)

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()


ranking_memo = Memo()   # unseeded Rankings
graph_memo = Memo()     # BaseGraphs


def memo_get(root: str, key: str):
    return ranking_memo.get(root, key)


def memo_put(root: str, key: str, ranking, reads: list) -> None:
    ranking_memo.put(root, key, ranking, reads)


def memo_clear() -> None:
    ranking_memo.clear()
    graph_memo.clear()
