"""Wiki-link relationship graph for the Obsidian vault.

Wikilinks resolve the way Obsidian reads them, without touching the file
system: a target is a vault-relative path, the tail of one, or a bare note
name, optionally followed by a heading (``#``), a block (``^``) or an alias
(``|``, escaped as ``\\|`` inside a table). :class:`LinkIndex` keys every
Markdown file by its path and its stem, case-folded and NFC-normalised, so two
files that differ only in case resolve the same on a case-sensitive Linux file
system as on macOS.
"""

from __future__ import annotations

import heapq
import json
import logging
import math
import os
import re
import threading
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Version of the wikilink resolution rules. ``link_graph.json`` records it, and
#: a graph saved under another version has its edges re-registered on load.
RESOLVER_VERSION = "v2"

# The whole inside of [[...]]: target, heading, block, alias and escapes alike.
_WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")

_MD_SUFFIX = ".md"

# Hebbian edge-weight parameters (w = 1 + eta*log1p(co_occurrence), clamped)
_HEBBIAN_ETA = 0.1
_MIN_WEIGHT = 0.1
_MAX_WEIGHT = 5.0


# Tie-break between equally weighted neighbours in ``LinkGraph.related``.
_DIRECTION_RANK = {"both": 0, "outgoing": 1, "incoming": 2}


def _edge_key(a: str, b: str) -> str:
    return "||".join(sorted((a, b)))


def extract_wikilinks(content: str) -> list[str]:
    """The raw inside of every ``[[...]]`` in *content*, alias and heading included."""
    return _WIKILINK_RE.findall(content)


def parse_target(raw: str) -> str | None:
    """The note a raw wikilink names, or ``None`` when it names none.

    ``\\|`` (an alias escaped inside a table) reads as ``|``; the alias after
    ``|`` and the heading or block after ``#`` or ``^`` are dropped; a ``.md``
    suffix is dropped and backslashes read as path separators. An empty
    result (``[[#Heading]]``, a link inside the same note) is ``None``.
    """
    text = raw.replace("\\|", "|").split("|", 1)[0]
    for mark in ("#", "^"):
        text = text.split(mark, 1)[0]
    text = text.strip()
    if text.casefold().endswith(_MD_SUFFIX):
        text = text[: -len(_MD_SUFFIX)]
    text = text.replace("\\", "/").strip().strip("/")
    return text or None


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def _directories(rel: str) -> list[str]:
    return [_fold(part) for part in rel.split("/")[:-1]]


def _common_depth(a: str, b: str) -> int:
    depth = 0
    for left, right in zip(_directories(a), _directories(b), strict=False):
        if left != right:
            break
        depth += 1
    return depth


@dataclass(frozen=True)
class LinkIndex:
    """Every Markdown file of a vault, keyed for wikilink resolution.

    ``by_path`` maps the folded vault-relative path without ``.md`` to the
    files that carry it (more than one only when two names differ in case
    alone); ``by_stem`` maps the folded file name without ``.md`` to every file
    with that name. Values are vault-relative paths with ``/`` separators,
    sorted.
    """

    by_path: Mapping[str, tuple[str, ...]]
    by_stem: Mapping[str, tuple[str, ...]]

    @classmethod
    def build(cls, rel_paths: Iterable[str]) -> LinkIndex:
        by_path: dict[str, list[str]] = {}
        by_stem: dict[str, list[str]] = {}
        for rel in rel_paths:
            if not rel.casefold().endswith(_MD_SUFFIX):
                continue
            key = _fold(rel[: -len(_MD_SUFFIX)])
            by_path.setdefault(key, []).append(rel)
            by_stem.setdefault(key.rsplit("/", 1)[-1], []).append(rel)
        return cls(
            by_path={k: tuple(sorted(v)) for k, v in by_path.items()},
            by_stem={k: tuple(sorted(v)) for k, v in by_stem.items()},
        )


def _closest(candidates: tuple[str, ...], source: str) -> str:
    """The candidate sharing the deepest directory prefix with ``source``; ties go to the smallest path."""
    return min(candidates, key=lambda rel: (-_common_depth(rel, source), rel))


def resolve_target(target: str, index: LinkIndex, source: str) -> str | None:
    """The vault-relative file a parsed wikilink *target* points to, from *source*.

    In order: the exact path; files whose path ends with the target; files whose
    name is the target's last segment. Several candidates resolve to the one
    sharing the deepest directory prefix with ``source``, then to the smallest
    path. ``None`` when nothing matches, a non-Markdown target such as a
    ``.canvas`` file included. The result may be ``source`` itself.
    """
    key = _fold(target)
    candidates = index.by_path.get(key, ())
    if not candidates:
        stem = key.rsplit("/", 1)[-1]
        pool = index.by_stem.get(stem, ())
        if "/" in key:
            suffix = "/" + key
            candidates = tuple(rel for rel in pool if _fold(rel[: -len(_MD_SUFFIX)]).endswith(suffix))
        if not candidates:
            candidates = pool
    if not candidates:
        return None
    return _closest(candidates, source)


def resolve_link(raw: str, index: LinkIndex, source: str) -> str | None:
    """The file a raw wikilink in *source* links to, or ``None`` when it is not an edge.

    ``None`` covers a link that resolves to nothing and a link back to
    ``source`` itself (``[[Hub#Intro]]`` inside ``Hub.md``).
    """
    target = parse_target(raw)
    if target is None:
        return None
    resolved = resolve_target(target, index, source)
    return None if resolved == source else resolved


class LinkGraph:
    """Bi-directional wiki-link graph."""

    def __init__(self) -> None:
        # file_path → set of linked file_paths (outgoing)
        self._outgoing: dict[str, set[str]] = {}
        # file_path → set of files that link TO this file (incoming / backlinks)
        self._incoming: dict[str, set[str]] = {}
        # undirected edge key → learned weight (default 1.0 when absent).
        # Learned overlay: deleting it loses nothing that can't be re-learned.
        self._weights: dict[str, float] = {}
        #: Resolver version the edges were registered under; ``None`` when
        #: the saved graph carries no resolver field.
        self.resolver: str | None = RESOLVER_VERSION

    # -- building -----------------------------------------------------------

    def set_links(self, source: str, targets: list[str]) -> None:
        """Register outgoing links from *source* to *targets*."""
        target_set = set(targets)
        self._outgoing[source] = target_set
        for t in target_set:
            self._incoming.setdefault(t, set()).add(source)

    def clear(self) -> None:
        # Weights survive the reindex rebuild: edges are re-registered from
        # wikilinks and weights re-attach by pair key. save() prunes orphans.
        self._outgoing.clear()
        self._incoming.clear()

    def successor(self) -> LinkGraph:
        """An empty graph that shares this graph's learned weights.

        A rebuild registers its edges on the successor and swaps it in when
        done, so a reader never walks a half-built graph, and a weight learned
        on either graph while the rebuild runs is kept by both.
        """
        graph = LinkGraph()
        graph._weights = self._weights
        return graph

    # -- Hebbian weights ------------------------------------------------------

    def weight(self, a: str, b: str) -> float:
        """Learned weight of the undirected edge (default 1.0)."""
        return self._weights.get(_edge_key(a, b), 1.0)

    def reinforce(self, a: str, b: str, co_occurrence: int) -> bool:
        """Recompute the edge weight from the co-occurrence frequency.

        w = 1 + eta*log1p(f_co), clamped to [0.1, 5.0]. Idempotent for a
        given f_co, so it survives rebuilds and never compounds. Returns
        True when the stored weight actually changed.
        """
        if co_occurrence <= 0:
            return False
        new_w = min(_MAX_WEIGHT, max(_MIN_WEIGHT, 1.0 + _HEBBIAN_ETA * math.log1p(co_occurrence)))
        key = _edge_key(a, b)
        if abs(self._weights.get(key, 1.0) - new_w) < 1e-9:
            return False
        self._weights[key] = new_w
        return True

    # -- querying -----------------------------------------------------------

    def outgoing(self, file_path: str) -> set[str]:
        return self._outgoing.get(file_path, set())

    def incoming(self, file_path: str) -> set[str]:
        return self._incoming.get(file_path, set())

    def related(self, file_path: str, limit: int = 10, *, ndigits: int | None = None) -> list[dict[str, Any]]:
        """Return the neighbours of *file_path*, strongest learned edges first.

        A neighbour linked both ways is one row with direction ``both``: the
        weight belongs to the undirected edge, so the two directions carry the
        same one. ``ndigits`` rounds the weight before sorting, so the order
        is the one the rounded weights show. The order is total, so ``limit``
        cuts the same rows on every run: weight descending, then ``both``,
        outgoing and incoming, then path. The cut happens after sorting, never
        on the walk order of a ``set``.
        """
        outgoing = self.outgoing(file_path)
        incoming = {fp for fp in self.incoming(file_path) if fp != file_path}
        results: list[dict[str, Any]] = []
        for fp in outgoing | incoming:
            if fp in outgoing and fp in incoming:
                direction = "both"
            else:
                direction = "outgoing" if fp in outgoing else "incoming"
            weight = self.weight(file_path, fp)
            if ndigits is not None:
                weight = round(weight, ndigits)
            results.append({"file": fp, "direction": direction, "weight": weight})
        results.sort(key=lambda r: (-r["weight"], _DIRECTION_RANK[r["direction"]], r["file"]))
        return results[:limit]

    def has_node(self, file_path: str) -> bool:
        """True if *file_path* appears as either source or target in the graph."""
        return file_path in self._outgoing or file_path in self._incoming

    def neighbors(self, file_path: str) -> set[str]:
        """Return the union of outgoing and incoming neighbors (undirected view)."""
        return self.outgoing(file_path) | self.incoming(file_path)

    def suggest_edges(
        self,
        *,
        min_shared: int = 2,
        min_confidence: float = 0.6,
        limit: int = 50,
        hub_percentile: float = 95.0,
    ) -> list[dict[str, Any]]:
        """Suggest likely-but-missing edges via shared-neighbor confidence.

        confidence(A, B) = |N(A) ∩ N(B)| / min(|N(A)|, |N(B)|)

        Pairs already linked are skipped. High-degree hub nodes (above the
        *hub_percentile* of the degree distribution) inflate every
        neighborhood they touch, so they are excluded from pairing.
        Suggestions are an overlay for curation — never written to the vault.
        """
        nodes = sorted(set(self._outgoing) | set(self._incoming))
        neighbor_map = {n: self.neighbors(n) for n in nodes}

        degrees = sorted(len(v) for v in neighbor_map.values())
        if not degrees:
            return []
        cutoff_idx = min(len(degrees) - 1, int(len(degrees) * hub_percentile / 100))
        hub_cutoff = degrees[cutoff_idx]

        candidates = [
            n for n in nodes if min_shared <= len(neighbor_map[n]) <= hub_cutoff
        ]

        suggestions: list[dict[str, Any]] = []
        for i, a in enumerate(candidates):
            na = neighbor_map[a]
            for b in candidates[i + 1 :]:
                if b in na:  # already linked (undirected view)
                    continue
                nb = neighbor_map[b]
                shared = len(na & nb)
                if shared < min_shared:
                    continue
                confidence = shared / min(len(na), len(nb))
                if confidence >= min_confidence:
                    suggestions.append(
                        {
                            "source": a,
                            "target": b,
                            "shared_neighbors": shared,
                            "confidence": round(confidence, 2),
                        }
                    )

        suggestions.sort(key=lambda s: (s["confidence"], s["shared_neighbors"]), reverse=True)
        return suggestions[:limit]

    def suggest_for(
        self,
        file_path: str,
        *,
        min_shared: int = 2,
        min_confidence: float = 0.6,
        hub_percentile: float = 95.0,
    ) -> list[dict[str, Any]]:
        """The suggestions of :meth:`suggest_edges` that involve *file_path*, without a global cut.

        Each row names the other document (``file``) with ``shared_neighbors``
        and ``confidence``, strongest first, ties by path. The same hub
        exclusion applies: when *file_path* is itself a hub, or has fewer than
        ``min_shared`` neighbours, nothing is suggested.
        """
        nodes = sorted(set(self._outgoing) | set(self._incoming))
        neighbor_map = {n: self.neighbors(n) for n in nodes}
        degrees = sorted(len(v) for v in neighbor_map.values())
        if not degrees or file_path not in neighbor_map:
            return []
        hub_cutoff = degrees[min(len(degrees) - 1, int(len(degrees) * hub_percentile / 100))]
        na = neighbor_map[file_path]
        if not min_shared <= len(na) <= hub_cutoff:
            return []
        suggestions: list[dict[str, Any]] = []
        for other in nodes:
            nb = neighbor_map[other]
            if other == file_path or other in na or not min_shared <= len(nb) <= hub_cutoff:
                continue
            shared = len(na & nb)
            if shared < min_shared:
                continue
            confidence = round(shared / min(len(na), len(nb)), 2)
            if shared / min(len(na), len(nb)) >= min_confidence:
                suggestions.append({"file": other, "shared_neighbors": shared, "confidence": confidence})
        suggestions.sort(key=lambda r: (-r["confidence"], -r["shared_neighbors"], r["file"]))
        return suggestions

    def get_impact(self, file_path: str, max_depth: int = 3) -> dict[str, Any]:
        """Recursive BFS traversal showing blast radius of changes to *file_path*.

        Only follows **incoming** (backlink) edges: files that reference the
        source directly or transitively.

        Returns a dict with per-depth lists and a total count. Which files
        land at which depth does not depend on the walk order (a file is
        claimed at the first level that reaches it); each list is sorted by
        path so the order does not depend on it either.
        """
        from collections import deque

        visited: set[str] = {file_path}
        queue: deque[tuple[str, int]] = deque()

        # Seed with direct backlinks (depth 1)
        for fp in self.incoming(file_path):
            if fp != file_path:
                queue.append((fp, 1))
                visited.add(fp)

        depth_buckets: dict[int, list[str]] = {}

        while queue:
            current, depth = queue.popleft()
            if depth > max_depth:
                continue
            depth_buckets.setdefault(depth, []).append(current)
            # Expand backlinks of current node
            for fp in self.incoming(current):
                if fp not in visited and depth + 1 <= max_depth:
                    visited.add(fp)
                    queue.append((fp, depth + 1))

        result: dict[str, Any] = {"source": file_path}
        total = 0
        for d in range(1, max_depth + 1):
            files = sorted(depth_buckets.get(d, []))
            result[f"depth_{d}"] = files
            total += len(files)
        result["total_affected"] = total
        return result

    # -- paths ----------------------------------------------------------------

    def shortest_path(self, from_path: str, to_path: str, max_hops: int = 6) -> list[str] | None:
        """The fewest-hops path over the undirected view, or None beyond ``max_hops``.

        Among several equally short paths the lexicographically smallest one
        (compared as a tuple of file paths) is returned, so the answer never
        depends on the walk order of a ``set``. A breadth-first pass from
        ``to_path`` measures every node's distance to it; the path then walks
        from ``from_path``, each step taking the smallest neighbour that is
        one hop closer.
        """
        if from_path == to_path:
            return [from_path]
        distance: dict[str, int] = {to_path: 0}
        frontier = [to_path]
        while frontier and from_path not in distance:
            hops = distance[frontier[0]] + 1
            if hops > max_hops:
                break
            next_frontier: list[str] = []
            for node in frontier:
                for nb in sorted(self.neighbors(node)):
                    if nb not in distance:
                        distance[nb] = hops
                        next_frontier.append(nb)
            frontier = next_frontier
        if from_path not in distance:
            return None
        path = [from_path]
        while path[-1] != to_path:
            closer = distance[path[-1]] - 1
            path.append(min(nb for nb in self.neighbors(path[-1]) if distance.get(nb) == closer))
        return path

    def shortest_weighted_path(
        self, from_path: str, to_path: str, max_hops: int = 6
    ) -> tuple[list[str] | None, float]:
        """Dijkstra over the undirected view with edge cost 1/weight.

        Learned highways (weight > 1) become cheaper to traverse than
        decorative links. Returns (path, total_cost) or (None, inf).

        Candidates are ordered by ``(cost, path)``: at equal cost the
        lexicographically smallest path wins, so ties never depend on the
        walk order of a ``set``.
        """
        best: dict[str, tuple[float, tuple[str, ...]]] = {from_path: (0.0, (from_path,))}
        heap: list[tuple[float, tuple[str, ...]]] = [(0.0, (from_path,))]

        while heap:
            cost, path = heapq.heappop(heap)
            node = path[-1]
            if node == to_path:
                return list(path), cost
            if (cost, path) != best[node] or len(path) - 1 >= max_hops:
                continue
            for nb in sorted(self.neighbors(node)):
                step = 1.0 / max(self.weight(node, nb), _MIN_WEIGHT)
                candidate = (cost + step, (*path, nb))
                if nb not in best or candidate < best[nb]:
                    best[nb] = candidate
                    heapq.heappush(heap, candidate)
        return None, float("inf")

    # -- persistence --------------------------------------------------------

    def _edge_exists(self, key: str) -> bool:
        a, _, b = key.partition("||")
        return b in self.neighbors(a)

    def save(self, path: Path) -> None:
        # Snapshots taken in one step each: another thread may learn a weight
        # while this one writes.
        learned = {
            k: round(w, 4)
            for k, w in list(self._weights.items())
            if abs(w - 1.0) > 1e-9 and self._edge_exists(k)
        }
        data = {
            "resolver": RESOLVER_VERSION,
            "outgoing": {k: sorted(v) for k, v in list(self._outgoing.items())},
            "incoming": {k: sorted(v) for k, v in list(self._incoming.items())},
            "weights": learned,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # One temporary file per writer: a reindex and a debounced weight save
        # may write the same graph from two threads. Compact JSON: the file is
        # a sidecar no one reads by eye, and load() parses either format.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
        os.replace(tmp, path)
        log.info("Graph saved to %s (%d learned weights)", path, len(learned))

    @classmethod
    def load(cls, path: Path) -> LinkGraph:
        graph = cls()
        if not path.exists():
            return graph
        data = json.loads(path.read_text())
        graph._outgoing = {k: set(v) for k, v in data.get("outgoing", {}).items()}
        graph._incoming = {k: set(v) for k, v in data.get("incoming", {}).items()}
        graph._weights = {k: float(w) for k, w in data.get("weights", {}).items()}
        graph.resolver = data.get("resolver")
        log.info("Graph loaded from %s (%d files)", path, len(graph._outgoing))
        return graph
