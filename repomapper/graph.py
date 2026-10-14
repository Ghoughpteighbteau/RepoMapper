"""The file reference graph as numpy edge arrays, and PageRank over it.

Edges are parallel arrays (a multigraph: the same pair can occur many times, once per shared name plus once for
an import). PageRank sums the parallel weights, like networkx does for a MultiDiGraph."""
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import scipy.sparse as sp


class PageRankConvergenceError(RuntimeError):
    pass


@dataclass
class RefGraph:
    nodes: List[str]            # rel_fname per node index
    src: np.ndarray             # edge source node index (the referencing / importing file)
    dst: np.ndarray             # edge target node index (the defining / imported file)
    weight: np.ndarray          # edge weight
    name_id: np.ndarray         # index into names of the identifier that links the files; -1 for an import edge
    names: List[str]

    @property
    def index(self) -> Dict[str, int]:
        return {node: i for i, node in enumerate(self.nodes)}

    def edge_weights(self, src: str, dst: str, imports: Optional[bool] = None) -> List[float]:
        """Weights of the parallel edges src -> dst (only import or only name edges if imports is given)."""
        index = self.index
        if src not in index or dst not in index:
            return []
        mask = (self.src == index[src]) & (self.dst == index[dst])
        if imports is not None:
            mask &= (self.name_id < 0) == imports
        return [float(w) for w in self.weight[mask]]

    def has_edge(self, src: str, dst: str) -> bool:
        return bool(self.edge_weights(src, dst))


def pagerank(n: int, src: np.ndarray, dst: np.ndarray, weight: np.ndarray, alpha: float = 0.85,
             personalization: Optional[np.ndarray] = None, tol: float = 1.0e-6, max_iter: int = 100) -> np.ndarray:
    """PageRank by power iteration, with networkx's semantics: parallel edges sum, dangling nodes (no outgoing
    weight) spread their rank by the personalization vector (uniform without one), which is normalized to sum 1,
    and iteration stops when the L1 change drops below n * tol. Raises ZeroDivisionError for an all-zero
    personalization and PageRankConvergenceError when max_iter is not enough."""
    if n == 0:
        return np.zeros(0)
    out = np.bincount(src, weights=weight, minlength=n)
    dangling = out == 0
    scale = np.zeros(n)
    scale[~dangling] = 1.0 / out[~dangling]
    # transition matrix transposed, so one iteration is a plain matrix-vector product
    transition = sp.csr_array((weight * scale[src], (dst, src)), shape=(n, n))

    if personalization is None:
        p = np.full(n, 1.0 / n)
    else:
        p = np.asarray(personalization, dtype=float)
        total = p.sum()
        if total == 0:
            raise ZeroDivisionError
        p = p / total
    x = np.full(n, 1.0 / n)
    for _ in range(max_iter):
        xlast = x
        x = alpha * (transition @ x + xlast[dangling].sum() * p) + (1 - alpha) * p
        if np.abs(x - xlast).sum() < n * tol:
            return x
    raise PageRankConvergenceError(f"PageRank did not converge in {max_iter} iterations")


def ident_ranks(graph: RefGraph, ranks: np.ndarray, target_scale: Optional[np.ndarray] = None) -> Dict[tuple, float]:
    """Each file's rank split across its outgoing edges by weight, summed per (target file, name). Import edges
    take their share of the split but credit no definition. target_scale multiplies each target file's sums."""
    n = len(graph.nodes)
    total = np.bincount(graph.src, weights=graph.weight, minlength=n)
    named = graph.name_id >= 0
    src, dst, name = graph.src[named], graph.dst[named], graph.name_id[named]
    share = ranks[src] * graph.weight[named] / total[src]
    keys, inverse = np.unique(dst.astype(np.int64) * max(len(graph.names), 1) + name, return_inverse=True)
    sums = np.bincount(inverse.ravel(), weights=share, minlength=len(keys))
    width = max(len(graph.names), 1)
    if target_scale is not None:
        sums = sums * target_scale[keys // width]
    nodes, names = graph.nodes, graph.names
    return {(nodes[k // width], names[k % width]): v for k, v in zip(keys.tolist(), sums.tolist())}
