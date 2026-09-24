"""Render docs/fault_sweep.png from results/fault_sweep.json (needs matplotlib)."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SHOWN = {
    "single pass": ("#9e9e9e", ":"),
    "retry everything": ("#d95f02", "--"),
    "controlled, naive retries": ("#7570b3", "-."),
    "controlled, no staleness check": ("#66a61e", "-."),
    "controlled": ("#1b9e77", "-"),
}


def main() -> None:
    sweep = json.loads((ROOT / "results" / "fault_sweep.json").read_text())
    rates = [100 * f for f in sweep["fault_rates"]]
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), sharex=True)
    for name, (color, style) in SHOWN.items():
        rows = sweep["policies"][name]
        for ax, key in zip(axes, ("correct_pct", "wrong_pct", "escalated_pct"), strict=True):
            ax.plot(rates, [r[key] for r in rows], style, color=color, marker="o", ms=4, lw=2, label=name)
    for ax, title in zip(axes, ("Diagnosed correctly", "Diagnosed wrongly", "Escalated to a human"), strict=True):
        ax.set_title(title)
        ax.set_xlabel("tool calls that fail (%)")
        ax.set_ylim(-2, 102)
        ax.grid(alpha=0.3)
    axes[0].set_ylabel("% of 1,200 incidents")
    axes[0].legend(fontsize=8, loc="lower left")
    fig.tight_layout()
    out = ROOT / "docs" / "fault_sweep.png"
    fig.savefig(out, dpi=150)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
