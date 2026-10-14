import networkx as nx
import numpy as np
import pytest

from repomapper.graph import RefGraph, PageRankConvergenceError, ident_ranks, pagerank


def arrays(edges):
    return (np.array([e[0] for e in edges], dtype=np.int64), np.array([e[1] for e in edges], dtype=np.int64),
            np.array([e[2] for e in edges], dtype=float))


def nx_ranks(n, edges, personalization=None, alpha=0.85):
    g = nx.MultiDiGraph()
    g.add_nodes_from(range(n))
    for s, d, w in edges:
        g.add_edge(s, d, weight=w)
    ranks = nx.pagerank(g, alpha=alpha, personalization=personalization)
    return np.array([ranks[i] for i in range(n)])


# 0 -> 1 twice (parallel edges), 1 -> 2, 3 -> 2, 2 and 4 dangling
EDGES = [(0, 1, 1.0), (0, 1, 2.5), (1, 2, 1.0), (3, 2, 0.5), (3, 0, 4.0)]


@pytest.mark.parametrize("personalization", [None, {0: 100.0}, {1: 3.0, 4: 1.0}, {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}])
@pytest.mark.parametrize("alpha", [0.85, 0.5])
def test_pagerank_matches_networkx(personalization, alpha):
    src, dst, w = arrays(EDGES)
    seeds = None if personalization is None else np.array([personalization.get(i, 0) for i in range(5)], dtype=float)
    ours = pagerank(5, src, dst, w, alpha, seeds)
    assert ours == pytest.approx(nx_ranks(5, EDGES, personalization, alpha), rel=1e-9, abs=1e-12)
    assert ours.sum() == pytest.approx(1.0)


def test_pagerank_with_no_edges_is_uniform_or_the_seed():
    empty = np.zeros(0, dtype=np.int64)
    assert pagerank(3, empty, empty, np.zeros(0)) == pytest.approx([1 / 3] * 3)
    assert pagerank(2, empty, empty, np.zeros(0), personalization=np.array([3.0, 1.0])) == pytest.approx([0.75, 0.25])


def test_pagerank_all_zero_personalization_raises_like_networkx():
    src, dst, w = arrays(EDGES)
    with pytest.raises(ZeroDivisionError):
        pagerank(5, src, dst, w, personalization=np.zeros(5))


def test_pagerank_reports_failure_to_converge():
    src, dst, w = arrays([(0, 1, 1.0), (1, 0, 1.0)])
    with pytest.raises(PageRankConvergenceError):
        pagerank(2, src, dst, w, alpha=0.99999, personalization=np.array([1.0, 0.0]), max_iter=2)


def test_ident_ranks_split_a_files_rank_by_edge_weight_and_skip_imports():
    graph = RefGraph(["a", "b", "c"], np.array([0, 0, 0, 1]), np.array([1, 1, 2, 2]),
                     np.array([1.0, 1.0, 2.0, 1.0]), np.array([0, 1, 0, -1]), ["x", "y"])
    got = ident_ranks(graph, np.array([0.4, 0.2, 0.0]))
    # a splits 0.4 over weights 1, 1, 2; b's only edge is an import, which credits no name
    assert got == pytest.approx({("b", "x"): 0.1, ("b", "y"): 0.1, ("c", "x"): 0.2})


def test_ident_ranks_target_scale():
    graph = RefGraph(["a", "b"], np.array([0]), np.array([1]), np.array([1.0]), np.array([0]), ["x"])
    assert ident_ranks(graph, np.array([0.5, 0.5]), np.array([1.0, 3.0])) == pytest.approx({("b", "x"): 1.5})


def test_edge_weights_and_has_edge():
    graph = RefGraph(["a", "b"], np.array([0, 0]), np.array([1, 1]), np.array([1.0, 2.0]), np.array([0, -1]), ["x"])
    assert graph.edge_weights("a", "b") == [1.0, 2.0]
    assert graph.edge_weights("a", "b", imports=True) == [2.0]
    assert graph.has_edge("a", "b") and not graph.has_edge("b", "a") and not graph.has_edge("a", "zz")
