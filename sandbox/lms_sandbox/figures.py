"""Graphs in the theme's colours (read from the theme artifact's tokens.json), for Claude to call from its
own scripts. Other diagrams it draws itself, following the theme's Figures section.

    from lms_figures import draw_graph
    draw_graph(
        nodes={"S": (0, 1), "A": (1, 2), "G": (3, 1)},
        edges=[("S", "A", 6), ("A", "G", 3), ("S", "G", 20)],
        path=["S", "A", "G"],
        heuristics={"S": 6, "A": 5, "G": 0},
        out="route_graph.png",
    )

Path nodes are navy with a gold ring and white letters, other nodes sky with a navy ring;
path edges are thick navy with bold navy costs, other edges grey; h values sit in muted grey
above each node (or below, for the nodes named in `below`). The image goes to
/work/out/figures/<out> unless `out` is a full path.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch  # noqa: E402

FIGURES = Path(os.environ.get("LMS_FIGURES_DIR", "/work/out/figures"))
TOKENS = Path(os.environ.get("LMS_THEME_DIR", "/work/theme")) / "tokens.json"


def _colours() -> dict[str, str]:
    """The theme artifact's colours (tokens.json), so figures follow the theme."""
    import json
    try:
        return {t["name"]: t["value"] for t in json.loads(TOKENS.read_text())["color"]["tokens"]}
    except (OSError, ValueError, KeyError):
        return {}


_C = _colours()
NAVY, SKY, GOLD = _C.get("navy", "#2d398c"), _C.get("sky", "#c4e4f5"), _C.get("gold", "#ffca0b")
INK, MUTED, EDGE = _C.get("ink", "#1b1c1f"), _C.get("muted", "#565b6b"), _C.get("edge", "#9aa3b8")


def draw_graph(nodes: dict[str, tuple[float, float]], edges: list[tuple], path: list[str] | tuple = (),
               heuristics: dict[str, float] | None = None, directed: bool = True, out: str = "graph.png",
               below: set[str] | list[str] = (), x_inch: float = 1.9, y_inch: float = 1.32) -> Path:
    """edges: (from, to) or (from, to, cost). Coordinates are grid steps: one step across is x_inch wide,
    one step up y_inch tall, as in the report's route graph. Nodes are fixed-size circles."""
    on_path = set(path)
    path_edges = set(zip(path, path[1:])) | (set() if directed else set(zip(path[1:], path)))
    xs, ys = [p[0] for p in nodes.values()], [p[1] for p in nodes.values()]
    span_x, span_y = max(max(xs) - min(xs), 1), max(max(ys) - min(ys), 1)
    pad_x, pad_y = 0.45 / x_inch * 1.9, 0.62 / y_inch * 1.32
    fig = plt.figure(figsize=((span_x + 2 * pad_x) * x_inch, (span_y + 2 * pad_y) * y_inch), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(min(xs) - pad_x, max(xs) + pad_x)
    ax.set_ylim(min(ys) - pad_y, max(ys) + pad_y)
    ax.axis("off")
    radius_pt = 19
    shrink = radius_pt + 3

    for edge in edges:
        a, b = edge[0], edge[1]
        cost = edge[2] if len(edge) > 2 else None
        hot = (a, b) in path_edges
        (x1, y1), (x2, y2) = nodes[a], nodes[b]
        ax.add_patch(FancyArrowPatch(
            (x1, y1), (x2, y2), shrinkA=shrink, shrinkB=shrink,
            arrowstyle=("-|>" if directed else "-"), mutation_scale=18 if hot else 13,
            color=NAVY if hot else EDGE, linewidth=3.8 if hot else 1.6, zorder=1,
        ))
        if cost is not None:
            # beside the middle of the edge, on its left (above a left-to-right edge)
            sx, sy = x2 - x1, y2 - y1
            dx_pt, dy_pt = sx * x_inch * 72, sy * y_inch * 72
            length = math.hypot(dx_pt, dy_pt) or 1
            off = (-dy_pt / length * 12, dx_pt / length * 12)
            ax.annotate(str(cost), ((x1 + x2) / 2, (y1 + y2) / 2), xytext=off, textcoords="offset points",
                        ha="center", va="center", fontsize=13, color=NAVY if hot else INK,
                        fontweight="bold" if hot else "normal", zorder=3)

    for name, (x, y) in nodes.items():
        hot = name in on_path
        ax.scatter([x], [y], s=(2 * radius_pt) ** 2, c=NAVY if hot else SKY, edgecolors=GOLD if hot else NAVY,
                   linewidths=3 if hot else 1.5, zorder=2)
        ax.text(x, y, name, ha="center", va="center", fontsize=16, fontweight="bold",
                color="white" if hot else NAVY, zorder=3)
        if heuristics and name in heuristics:
            up = name not in set(below)
            ax.annotate(f"h={heuristics[name]}", (x, y), xytext=(0, (radius_pt + 11) * (1 if up else -1)),
                        textcoords="offset points", ha="center", va="center", fontsize=12, color=MUTED, zorder=3)

    dest = Path(out) if os.path.isabs(out) else FIGURES / out
    dest.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(dest, bbox_inches="tight", pad_inches=0.08, facecolor="white")
    plt.close(fig)
    return dest
