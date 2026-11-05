"""Plot the TB2.1 trim-shape arms (passes and cost per pass) to docs/media, light and dark."""
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).parent
DATA = HERE / "tb21-h55-arms.json"
OUT = HERE.parents[2] / "docs" / "media"
NATIVE = "Native"
THEMES = {
    "": {"bg": "white", "ink": "#1b1b2a", "muted": "#5f6275", "grid": "#e3e4ec", "accent": "#4450a8", "other": "#8a8ca0"},
    "-dark": {"bg": "#0d1117", "ink": "#e6e8f0", "muted": "#9ba0b5", "grid": "#2a2f3a", "accent": "#8f9cff", "other": "#6c7086"},
}


def plot(settings, tasks, source, suffix, theme):
    names = [s["name"] for s in settings][::-1]
    passed = [s["passed"] for s in settings][::-1]
    per_pass = [s["cost_usd"] / s["passed"] for s in settings][::-1]
    colors = [theme["other"] if n == NATIVE else theme["accent"] for n in names]
    fig, (left, right) = plt.subplots(1, 2, figsize=(9.6, 5.2), facecolor=theme["bg"], sharey=True)
    fig.subplots_adjust(left=0.16, right=0.95, top=0.72, bottom=0.14, wspace=0.12)
    fig.text(0.03, 0.93, "Terminal-Bench 2.1: trim shapes", fontsize=17, weight="bold", color=theme["ink"])
    native = next(s for s in settings if s["name"] == NATIVE)
    best = min(settings, key=lambda s: s["cost_usd"] / s["passed"])
    fig.text(0.03, 0.83, f"All settings passed {min(passed)}–{max(passed)} of {tasks}; {best['name']} cost "
             f"\\${best['cost_usd'] / best['passed']:.2f} per pass, native \\${native['cost_usd'] / native['passed']:.2f}.\n"
             "One trial per task: a one-task gap is within noise.", fontsize=11.5, color=theme["muted"], va="center")
    for ax, values, title, fmt in ((left, passed, f"Tasks passed, of {tasks}", "{:.0f}"),
                                   (right, per_pass, "Cost per pass, USD", "\\${:.2f}")):
        ax.set_facecolor(theme["bg"])
        ax.barh(names, values, color=colors, height=0.6)
        ax.set_title(title, loc="left", fontsize=12.5, weight="bold", color=theme["ink"])
        top = max(values)
        ax.set_xlim(0, top * 1.22)
        for y, v in enumerate(values):
            ax.text(v + top * 0.02, y, fmt.format(v), va="center", fontsize=10.5, color=theme["ink"])
        ax.xaxis.grid(True, color=theme["grid"])
        ax.set_axisbelow(True)
        ax.tick_params(colors=theme["muted"], length=0, labelsize=10.5)
        for side in ax.spines.values():
            side.set_visible(False)
    left.set_xlim(0, tasks)
    fig.text(0.03, 0.03, f"Source: {source}", fontsize=8.5, color=theme["muted"], wrap=True)
    fig.savefig(OUT / f"termbench-arms{suffix}.png", dpi=110, facecolor=theme["bg"])
    plt.close(fig)


def main():
    data = json.loads(DATA.read_text())
    source = "Terminal-Bench 2.1, 8 long tasks · Claude Code, Haiku 5.5 via Harbor · cost reported by Claude Code"
    for suffix, theme in THEMES.items():
        plot(data["settings"], data["tasks"], source, suffix, theme)


if __name__ == "__main__":
    sys.exit(main())
