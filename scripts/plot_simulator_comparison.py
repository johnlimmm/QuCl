#!/usr/bin/env python3
"""Compact standalone comparison plots from a completed campaign, without reruns."""
import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

COLORS = ("#AEC9E2", "#F3CAA8", "#E7BDD0")
HATCHES = ("///", "\\\\", "..")
ENGINES = ("QuCl", "NetSquid-only", "Q2NS-only")


def results_text(summary, interval, cost=None):
    lines = [
        (
            "# Actual simulator comparison — first pilot"
            if summary["pilot"]
            else "# Actual simulator comparison"
        ),
        "",
        "Every execution completes swapping at R, native classical control/readiness, "
        "teleportation at A using the same A–B pair, and final correction at B.",
        "",
        "- New evaluation runs: **{}**; separate calibration runs: **{}**.".format(
            summary["simulation_runs"], summary["calibration_runs"]
        ),
        "- Maximum independent-reference density-matrix error: **{:.3g}**.".format(
            summary["max_density_matrix_error"]
        ),
        "- Loaded traffic phases: **{}**. Six inputs and four workflows are averaged "
        "inside each phase; they are not independent traffic samples.".format(
            len(summary["plan"]["test_traffic_seeds"])
        ),
        "",
        "The table selects the {}-ns workflow-start interval (0.5x for the default "
        "profile). All intervals are retained in the campaign CSV/JSON.".format(interval),
        "",
        "| Load | Engine | Noise-off latency (µs) | Noisy expected final fidelity |",
        "|---:|---|---:|---:|",
    ]
    for load in summary["plan"]["classical_loads"]:
        for engine in ENGINES:

            def cell(mode):
                return next(
                    (
                        g
                        for g in summary["groups"]
                        if g["mode"] == mode
                        and g["engine"] == engine
                        and g["load"] == load
                        and g["interval_ns"] == interval
                    ),
                    None,
                )

            noise, plain = cell("noisy"), cell("noiseless")
            lines.append(
                "| {:g} | {} | {:.2f} | {} |".format(
                    load,
                    engine,
                    plain["latency_ns"]["mean"] / 1000,
                    (
                        "{:.5f}".format(noise["expected_fidelity"]["mean"])
                        if noise
                        else "unsupported"
                    ),
                )
            )
    lines += [
        "",
        "Q2NS-only uses the actual Q2NS density-matrix backend and native "
        "instantaneous operations. Its noise-off fidelity is one within numerical "
        "precision. It has no matching T1/T2-plus-gate-noise model in this adapter, "
        "so it is absent from the noisy fidelity comparison. These are differences "
        "between configured models, not a simulator accuracy or speed ranking.",
        "",
        "NetSquid-only uses actual native ClassicalChannels with fixed, independently "
        "calibrated delays. It retains the same quantum programs, memory noise and "
        "FIFO policy as QuCl. Fixed delay is a selected abstraction, not a limitation "
        "of NetSquid. The existing Fixed-Dc ablation remains an equivalence control.",
        "",
        "Native sampled fidelity is retained in every report. The noisy expectation "
        "uses all 16 joint branches of the separate NetSquid reference at observed "
        "operation times; native sampled states are cross-validated. Error bars are "
        "95% phase-bootstrap intervals conditional on calibration. This small pilot "
        "does not generalize over arbitrary traffic or hardware.",
        "",
        "![Noise-off latency](standalone_latency.png)",
        "",
        "![Matched noisy fidelity](matched_fidelity.png)",
    ]
    if cost:
        lines += [
            "",
            "## Cold execution cost",
            "",
            "{} runs; {} repeats per engine/size/load. The following means use "
            "32 workflows and load 0.75.".format(cost["runs"], cost["repeats"]),
            "",
            "| Engine | Time (s) | Time per workflow (ms) |",
            "|---|---:|---:|",
        ]
        for engine in ENGINES:
            own = [
                r
                for r in cost["records"]
                if r["engine"] == engine and r["workflows"] == 32 and r["load"] == 0.75
            ]
            mean = sum(r["wall_seconds"] for r in own) / len(own)
            lines.append("| {} | {:.3f} | {:.2f} |".format(engine, mean, mean / 32 * 1000))
        lines += [
            "",
            cost["scope"],
            "",
            "The Q2NS worker imports no NetSquid. Configuration preparation occurs "
            "in the untimed parent. Three repeats are preliminary; graph whiskers "
            "show observed ranges, not confidence intervals. NetSquid-only omits "
            "background-packet events represented by its fixed delays, so the "
            "engines perform different amounts of work.",
            "",
            "![Cold execution cost](standalone_cost.png)",
        ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-ns", type=int, default=13703)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    s = json.loads((args.campaign / "summary.json").read_text())
    if not s["passed"]:
        raise ValueError("completed successful campaign required")
    args.output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "hatch.linewidth": 0.6,
        }
    )
    loads = s["plan"]["classical_loads"]
    for mode, field, ylabel, scale, name in (
        ("noiseless", "latency_ns", "Latency (µs)", 0.001, "standalone_latency"),
        ("noisy", "expected_fidelity", "Fidelity", 1, "matched_fidelity"),
    ):
        fig, ax = plt.subplots(figsize=(5.0, 3.0))
        engines = ENGINES if mode == "noiseless" else ENGINES[:2]
        width = 0.72 / len(engines)
        all_bounds = []
        for i, engine in enumerate(engines):
            cells = [
                next(
                    g
                    for g in s["groups"]
                    if g["mode"] == mode
                    and g["engine"] == engine
                    and g["load"] == load
                    and g["interval_ns"] == args.interval_ns
                )
                for load in loads
            ]
            means = np.array([g[field]["mean"] * scale for g in cells])
            bounds = (
                np.array(
                    [
                        (g[field]["ci95_cluster_bootstrap"] or [g[field]["mean"]] * 2)
                        for g in cells
                    ]
                )
                * scale
            )
            all_bounds.extend(bounds.flatten())
            x = np.arange(len(loads)) + (i - (len(engines) - 1) / 2) * width
            if mode == "noiseless":
                ax.bar(
                    x,
                    means,
                    width * 0.91,
                    color=COLORS[i],
                    edgecolor="#4F5660",
                    linewidth=0.65,
                    hatch=HATCHES[i],
                    label=engine,
                )
            else:
                # Points avoid a truncated bar baseline when differences are small.
                ax.plot(
                    x,
                    means,
                    linestyle="none",
                    marker=("o", "s")[i],
                    markersize=7,
                    markerfacecolor=COLORS[i],
                    markeredgecolor="#4F5660",
                    label=engine,
                )
            ax.errorbar(
                x,
                means,
                yerr=np.maximum(0, np.array([means - bounds[:, 0], bounds[:, 1] - means])),
                fmt="none",
                ecolor="#4F5660",
                capsize=3,
                linewidth=0.8,
            )
        ax.set_xticks(np.arange(len(loads)), ["{:g}".format(v) for v in loads])
        ax.set_xlabel("Load")
        ax.set_ylabel(ylabel)
        ax.set_axisbelow(True)
        ax.yaxis.grid(True, color="#E9EBEE", linewidth=0.6)
        ax.legend(
            loc="lower center",
            bbox_to_anchor=(0.5, 1.01),
            ncol=len(engines),
            frameon=False,
            fontsize=9,
            handlelength=1.5,
            columnspacing=1,
        )
        if mode == "noiseless":
            ax.set_ylim(0, max(all_bounds) * 1.13)
        else:
            spread = max(max(all_bounds) - min(all_bounds), 0.001)
            ax.set_ylim(min(all_bounds) - spread * 0.22, max(all_bounds) + spread * 0.22)
        fig.tight_layout(pad=0.5)
        for ext in ("pdf", "png"):
            fig.savefig(args.output / (name + "." + ext), dpi=220, bbox_inches="tight")
        plt.close(fig)
    note = (
        "Plots use workflow-start interval {} ns. Latency compares the three engines with "
        "noise off; Q2NS operations are instantaneous. Fidelity compares only QuCl and "
        "NetSquid-only with identical T1/T2 and gate noise; its axis is expanded. "
        "Fidelity is the six-input, branch-weighted final Bob fidelity; latency includes "
        "the entire Swap–Teleport chain. Error bars are traffic-phase bootstrap 95% "
        "intervals; zero load is deterministic. NetSquid-only load indexes the fixed-delay "
        "calibration; it does not generate background packets. Pilot={}."
    ).format(args.interval_ns, s["pilot"])
    (args.output / "CAPTIONS.txt").write_text(note + "\n")
    cost = None
    if args.runtime:
        cost = json.loads((args.runtime / "summary.json").read_text())
        if not cost["passed"]:
            raise ValueError("completed cost campaign required")
        fig, ax = plt.subplots(figsize=(5.0, 3.0))
        sizes = sorted({r["workflows"] for r in cost["records"]})
        for i, engine in enumerate(ENGINES):
            values = [
                [
                    r["wall_seconds"]
                    for r in cost["records"]
                    if r["engine"] == engine and r["load"] == 0.75 and r["workflows"] == n
                ]
                for n in sizes
            ]
            means = np.array([np.mean(v) for v in values])
            ax.errorbar(
                sizes,
                means,
                yerr=[
                    means - np.array([min(v) for v in values]),
                    np.array([max(v) for v in values]) - means,
                ],
                color=COLORS[i],
                marker=("o", "s", "^")[i],
                markersize=6,
                markeredgecolor="#4F5660",
                linestyle=("-", "--", ":")[i],
                linewidth=1.5,
                capsize=3,
                label=engine,
            )
        ax.set_xscale("log", base=2)
        ax.set_xticks(sizes, [str(n) for n in sizes])
        ax.set_xlabel("Workflows")
        ax.set_ylabel("Time (s)")
        ax.set_ylim(bottom=0)
        ax.yaxis.grid(True, color="#E9EBEE", linewidth=0.6)
        ax.legend(
            loc="lower center",
            bbox_to_anchor=(0.5, 1.01),
            ncol=3,
            frameon=False,
            fontsize=9,
            columnspacing=1,
            handlelength=1.5,
        )
        fig.tight_layout(pad=0.5)
        for ext in ("pdf", "png"):
            fig.savefig(args.output / ("standalone_cost." + ext), dpi=220, bbox_inches="tight")
        plt.close(fig)
        with (args.output / "CAPTIONS.txt").open("a") as stream:
            stream.write(
                "Cost: noise-off, load 0.75, 0.5x interval. Points are means; whiskers "
                "are observed min–max across repeats, not confidence intervals. "
                "Cold engine-specific imports, startup, validation and report I/O "
                "included. Different model work; not isolated IPC overhead.\n"
            )
    (args.output / "RESULTS.md").write_text(results_text(s, args.interval_ns, cost))


if __name__ == "__main__":
    main()
