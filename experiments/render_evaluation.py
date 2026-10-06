#!/usr/bin/env python3
"""Check completed reports and render Figures 3, 4, 5(a) and 5(b).

This postprocessing program requires NumPy and Matplotlib, not NetSquid.
It never changes simulation reports and writes only to a new output directory.
"""
import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.ticker import MaxNLocator, FormatStrFormatter
import numpy as np
from analysis import chain_cells, compare, summarize, model_rows, DELAY_FIELDS

COLORS = ["#DCE7F1", "#F5E3D3", "#F0DEE6"]
EDGES = ["#91A4B7", "#B8A18D", "#B39AA6"]
HATCHES = ["///", "\\\\", ".."]
MARKERS = ["o", "s", "D"]
MODELS = ["No-Dq-R", "Fixed-Dc", "Decoupled"]
LABELS = ["No R contention", "Fixed classical delay", "Decoupled timing"]


def style():
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10.5,
            "axes.labelsize": 11,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": "#9099A2",
            "axes.linewidth": 0.7,
            "text.color": "#303942",
            "xtick.color": "#4C5862",
            "ytick.color": "#4C5862",
            "legend.frameon": False,
            "axes.axisbelow": True,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "savefig.facecolor": "white",
            "hatch.linewidth": 0.65,
        }
    )


def figure_axes():
    fig, ax = plt.subplots(figsize=(5.4, 3.45))
    fig.subplots_adjust(left=0.155, right=0.98, bottom=0.19, top=0.83)
    ax.set_xlabel("Background load", labelpad=8)
    ax.grid(axis="y", color="#E7EAEE", linewidth=0.65)
    ax.tick_params(axis="x", length=0, pad=7)
    ax.tick_params(axis="y", width=0.6)
    return fig, ax


def legend(fig, ax):
    handles, labels = ax.get_legend_handles_labels()
    wrapped = {
        "No R contention": "No R\ncontention",
        "Fixed classical delay": "Fixed classical\ndelay",
        "Decoupled timing": "Decoupled\ntiming",
    }
    labels = [wrapped.get(label, label) for label in labels]
    fig.legend(
        handles,
        labels,
        loc="lower center",
        bbox_to_anchor=(0.5, 1.025),
        bbox_transform=ax.transAxes,
        borderaxespad=0,
        ncol=min(3, len(labels)),
        fontsize=9.5,
        handlelength=1.5,
        handleheight=1.15,
        columnspacing=1.0,
        handletextpad=0.5,
    )


def save_figure(fig, out, name, book):
    fig.savefig(out / "figures" / (name + ".pdf"))
    fig.savefig(out / "figures" / (name + ".png"), dpi=400)
    book.savefig(fig)
    plt.close(fig)


def bars(series, ylabel, loads, out, name, book, tick_format=None):
    fig, ax = figure_axes()
    positions = np.arange(len(loads)) * 1.15
    width = 0.245
    upper = max(v[2] if v[2] is not None else v[0] for _, values in series for v in values)
    height = upper * 1.13 if upper else 1
    for i, (label, values) in enumerate(series):
        x = positions + (i - (len(series) - 1) / 2) * width
        ys = [v[0] for v in values]
        ax.bar(
            x,
            ys,
            width * 0.90,
            label=label,
            color=COLORS[i],
            zorder=3,
            edgecolor=EDGES[i],
            linewidth=0.8,
            hatch=HATCHES[i],
        )
        for xp, (y, lo, hi) in zip(x, values):
            if lo is not None:
                ax.errorbar(
                    xp,
                    y,
                    yerr=[[max(0, y - lo)], [max(0, hi - y)]],
                    fmt="none",
                    ecolor="#414B55",
                    capsize=3,
                    elinewidth=0.9,
                    zorder=5,
                )
            if abs(y) < 1e-9:
                ax.plot(
                    [xp - width * 0.45, xp + width * 0.45],
                    [0, 0],
                    color=EDGES[i],
                    lw=1.6,
                    zorder=6,
                    clip_on=False,
                )
    ax.set_ylim(0, height)
    ax.set_xticks(positions, [f"{l:.2f}" for l in loads])
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
    if tick_format is not None:
        ax.yaxis.set_major_formatter(FormatStrFormatter(tick_format))
    ax.set_ylabel(ylabel, labelpad=8)
    legend(fig, ax)
    save_figure(fig, out, name, book)


def plots(s, cells, out):
    style()
    p = s["plan"]
    loads = p["classical_loads"]
    with PdfPages(out / "FIGURES.pdf") as book:
        series = []
        for interval, ratio in zip(p["request_intervals_ns"], p["interval_factors"]):
            values = []
            for load in loads:
                r = next(c for c in cells if c["load"] == load and c["interval_ns"] == interval)
                values.append(
                    tuple(
                        r[k] / 1000 if r[k] is not None else None
                        for k in [
                            "extra_latency_ns",
                            "extra_latency_ns_low",
                            "extra_latency_ns_high",
                        ]
                    )
                )
            series.append((f"{ratio:g}× interval", values))
        bars(series, "Additional latency (μs)", loads, out, "fig3_chain_latency", book)
        fidelity_plot(s, cells, out, book)
        for metric, scale, models, labels, ylabel, name, tick_format in (
            ("latency_mae_ns", 1000, MODELS, LABELS, "Latency MAE (μs)", "fig5_a", None),
            (
                "expected_fidelity_mae",
                1,
                MODELS[:2],
                LABELS[:2],
                "Fidelity MAE",
                "fig5_b",
                "%.3f",
            ),
        ):
            series = []
            for model, label in zip(models, labels):
                values = []
                for load in loads:
                    g = next(
                        g
                        for g in s["ablation_groups"]
                        if g["classical_load"] == load
                        and g["request_interval_ns"] == p["request_intervals_ns"][0]
                        and g["model"] == model
                    )
                    e = g[metric]
                    lo, hi = e["ci95_cluster_bootstrap"] or [None, None]
                    values.append(
                        (
                            e["mean"] / scale,
                            None if lo is None else lo / scale,
                            None if hi is None else hi / scale,
                        )
                    )
                series.append((label, values))
            bars(series, ylabel, loads, out, name, book, tick_format)


def interval_text(e, scale=1, digits=2):
    m = e["mean"] / scale
    ci = e["ci95_cluster_bootstrap"]
    return (
        f"{m:.{digits}f} [{ci[0]/scale:.{digits}f}, {ci[1]/scale:.{digits}f}]"
        if ci
        else f"{m:.{digits}f} (deterministic)"
    )


def fidelity_plot(s, cells, out, book):
    """Absolute fidelity on an explicitly expanded axis, without misleading bars."""
    p = s["plan"]
    loads = p["classical_loads"]
    fig, ax = figure_axes()
    bounds = []
    for i, (interval, ratio) in enumerate(
        zip(p["request_intervals_ns"], p["interval_factors"])
    ):
        own = [
            next(c for c in cells if c["load"] == load and c["interval_ns"] == interval)
            for load in loads
        ]
        ys = [c["expected_fidelity"] for c in own]
        lo = [
            (
                c["expected_fidelity_low"]
                if c["expected_fidelity_low"] is not None
                else c["expected_fidelity"]
            )
            for c in own
        ]
        hi = [
            (
                c["expected_fidelity_high"]
                if c["expected_fidelity_high"] is not None
                else c["expected_fidelity"]
            )
            for c in own
        ]
        bounds += lo + hi
        # Categorical groups match Fig. 3. The 0.5×/1× means can be identical;
        # adjacent markers keep both visible without implying distinct fidelity.
        x = np.arange(len(loads)) * 1.15 + (i - 1) * 0.23
        ax.plot(
            x,
            ys,
            color=EDGES[i],
            marker=MARKERS[i],
            ms=7.5,
            linestyle="none",
            markerfacecolor=COLORS[i],
            markeredgewidth=1.1,
            zorder=4,
            label=f"{ratio:g}× interval",
        )
        ax.errorbar(
            x[1:],
            ys[1:],
            yerr=[
                np.maximum(0, np.array(ys[1:]) - lo[1:]),
                np.maximum(0, np.array(hi[1:]) - ys[1:]),
            ],
            fmt="none",
            ecolor=EDGES[i],
            capsize=3.5,
            lw=1,
            zorder=3,
        )
    span = max(bounds) - min(bounds)
    pad = max(span * 0.18, 0.0005)
    ax.set_ylim(max(0, min(bounds) - pad), min(1, max(bounds) + pad))
    ax.set_xlim(-0.5, (len(loads) - 1) * 1.15 + 0.5)
    ax.set_xticks(np.arange(len(loads)) * 1.15, [f"{x:.2f}" for x in loads])
    ax.set_ylabel("Fidelity", labelpad=8)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.3f"))
    legend(fig, ax)
    save_figure(fig, out, "fig4_teleport_fidelity", book)


def read(path):
    with gzip.open(path, "rt") if path.suffix == ".gz" else path.open() as stream:
        return json.load(stream)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def write_csv(path, rows):
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def audit(source):
    """Trace every CSV point back to a completed report before plotting."""
    summary = read(source / "summary.json")
    require(summary["passed"], "campaign did not pass")
    plan = summary["plan"]
    for name, expected in summary["data_sha256"].items():
        require(digest(source / name) == expected, "changed data: " + name)
    truth = {}
    model_reports = {}
    for name, expected in summary["report_sha256"].items():
        require(digest(source / name) == expected, "changed report: " + name)
        report = read(source / name)
        require(report["validation"]["passed"], "invalid report: " + name)
        if "cross_validation" in report:
            require(report["cross_validation"]["passed"], "invalid state reference: " + name)
        if name.startswith("chains/"):
            for metric in report["metrics"]["chains"]:
                cid = str(metric["chain_id"])
                reference = report["cross_validation"]["chains"][cid]["reference"]
                branches = list(reference["branches"].values())
                require(len(branches) == 16, "missing branches")
                require(
                    abs(sum(b["probability"] for b in branches) - 1) < 1e-12,
                    "branch probability",
                )
                # Both the stored branch fidelity and sampled output are produced
                # by NetSquid's fidelity(..., squared=True), not a decay formula.
                expected_fidelity = sum(
                    b["probability"] * b["stages"][1]["checkpoints"]["usable"]["fidelity"]
                    for b in branches
                )
                require(
                    abs(expected_fidelity - reference["expected_fidelity"]) < 1e-12,
                    "branch-weighted fidelity",
                )
                truth[(name, metric["chain_id"])] = (metric, expected_fidelity)
        elif name.startswith("ablations/"):
            model_reports[name] = report
    with (source / "chains.csv").open() as stream:
        rows = [
            {k: v if k == "input_state" else float(v) for k, v in row.items()}
            for row in csv.DictReader(stream)
        ]
    require(len(rows) == summary["chain_transactions"] == len(truth), "chain count")
    for row in rows:
        name = "chains/load-{}-interval-{}-phase-{}/input-{}.json.gz".format(
            row["classical_load"],
            int(row["request_interval_ns"]),
            int(row["traffic_seed"]),
            plan["input_states"].index(row["input_state"]),
        )
        metric, fidelity = truth[(name, int(row["chain_id"]))]
        require(metric["latency_ns"] == row["latency_ns"], "CSV latency differs from trace")
        require(all(metric["delays"][k] == row[k] for k in DELAY_FIELDS), "CSV decomposition")
        require(
            abs(metric["output_fidelity"] - row["observed_fidelity"]) < 1e-12,
            "sampled fidelity",
        )
        require(abs(fidelity - row["expected_fidelity"]) < 1e-12, "CSV expected fidelity")
    cells = chain_cells(rows, plan)
    errors, couplings = [], []
    for path in sorted((source / "ablations").glob("*/comparison.json")):
        case = read(path)
        metadata = {
            k: case[k]
            for k in ("classical_load", "request_interval_ns", "traffic_seed", "quantum_seed")
        }
        for model in ("Full-Sync", "No-Dq-R", "Fixed-Dc"):
            name = "ablations/" + path.parent.name + "/" + model + ".json.gz"
            raw = model_rows(model_reports[name], plan["fidelity_min"], plan["deadline_ns"])
            require(raw == case["models"][model], "comparison differs from trace/reference")
        full = case["models"]["Full-Sync"]
        for model in MODELS:
            errors.extend(
                compare(
                    full,
                    case["models"][model],
                    dict(metadata, model=model),
                    plan["deadline_ns"],
                )
            )
        for a, b, c in zip(full, case["models"]["No-Dq-R"], case["models"]["Decoupled"]):
            change = b["result_delay_ns"] - a["result_delay_ns"]
            require(
                c["transaction_latency_ns"] - a["transaction_latency_ns"] == change,
                "Decoupled error must equal the changed result-path delay",
            )
            couplings.append(
                dict(
                    metadata,
                    session_id=a["session_id"],
                    full_R_wait_ns=a["quantum_wait_ns"],
                    full_result_delay_ns=a["result_delay_ns"],
                    no_R_result_delay_ns=b["result_delay_ns"],
                    delta_result_delay_ns=change,
                )
            )
    require(len(errors) == 3 * summary["transactions_per_ablation_model"], "ablation count")
    groups = summarize(errors, plan["bootstrap_samples"])
    require(groups == summary["ablation_groups"], "comparison aggregates differ")
    return summary, cells, couplings


def render(source, output):
    require(not output.exists(), "choose a new figure output directory")
    summary, cells, couplings = audit(source)
    output.mkdir(parents=True)
    (output / "figures").mkdir()
    write_csv(output / "chain_cells.csv", cells)
    write_csv(output / "result_phase_coupling.csv", couplings)
    plots(summary, cells, output)
    lines = [
        "# Reproduced evaluation",
        "",
        "All figures are generated from the named completed run. Original reports are unchanged.",
        "",
        "| Load | Start interval (ns) | Mean latency (us) | Mean fidelity | Traffic phases |",
        "|---:|---:|---:|---:|---:|",
    ]
    for c in cells:
        lines.append(
            "| {} | {} | {:.3f} | {:.6f} | {} |".format(
                c["load"],
                c["interval_ns"],
                c["latency_ns"] / 1000,
                c["expected_fidelity"],
                c["traffic_phases"],
            )
        )
    lines += [
        "",
        "Intervals are 95% percentile bootstrap intervals over traffic-phase means.",
        "Zero load is deterministic. Six input states and sessions are averaged within each phase.",
        "Calibration uncertainty is not included. These are finite batches with periodic traffic.",
        "",
        "Figure 3: additional latency relative to the longest-interval zero-load condition.",
        "Figure 4: absolute branch-weighted final teleportation fidelity; the vertical axis is expanded.",
        "Figure 5(a): swapping-stage latency MAE against Full Sync at the shortest start interval.",
        "Figure 5(b): swapping-stage Bell-pair fidelity MAE under the same conditions.",
        "Fidelity errors compare probability-weighted branch expectations, not individual sampled outcomes.",
        "Decoupled has no quantum-state prediction and is omitted from Figure 5(b).",
        "The swapping ablation and complete chained workflow are different experiments.",
        "",
        "## Model errors at the shortest start interval",
        "",
        "MAE takes the absolute paired error before averaging sessions and traffic phases.",
        "Bias retains its sign (model minus Full Sync); positive bias means overprediction.",
        "All fidelity values below are dimensionless, with 95% phase-bootstrap intervals.",
        "",
        "| Load | Model | Latency MAE (us) [CI] | Fidelity MAE [CI] | Fidelity bias [CI] |",
        "|---:|---|---:|---:|---:|",
    ]
    error_cells = []
    for g in summary["ablation_groups"]:
        if g["request_interval_ns"] != summary["plan"]["request_intervals_ns"][0]:
            continue
        row = {k: g[k] for k in ("classical_load", "request_interval_ns", "model")}
        for key in ("latency_mae_ns", "expected_fidelity_mae", "expected_fidelity_bias"):
            estimate = g[key]
            row[key] = None if estimate is None else estimate["mean"]
            bounds = estimate["ci95_cluster_bootstrap"] if estimate is not None else None
            row[key + "_low"], row[key + "_high"] = bounds or [None, None]
        error_cells.append(row)
        lines.append(
            "| {} | {} | {} | {} | {} |".format(
                g["classical_load"],
                LABELS[MODELS.index(g["model"])],
                interval_text(g["latency_mae_ns"], scale=1000),
                (
                    interval_text(g["expected_fidelity_mae"], digits=5)
                    if g["expected_fidelity_mae"] is not None
                    else "not defined"
                ),
                (
                    interval_text(g["expected_fidelity_bias"], digits=5)
                    if g["expected_fidelity_bias"] is not None
                    else "not defined"
                ),
            )
        )
    write_csv(output / "model_error_cells.csv", error_cells)
    (output / "RESULTS.md").write_text("\n".join(lines) + "\n")
    provenance = dict(
        passed=True,
        source=str(source.resolve()),
        summary_sha256=digest(source / "summary.json"),
        renderer_sha256=digest(Path(__file__)),
        analysis_sha256=digest(Path(__file__).with_name("analysis.py")),
        matplotlib=matplotlib.__version__,
        numpy=np.__version__,
        python=sys.version,
        raw_reports_checked=len(summary["report_sha256"]),
        files={str(p.relative_to(output)): digest(p) for p in output.rglob("*") if p.is_file()},
    )
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print("PASS: verified reports and rendered Figures 3, 4, 5(a) and 5(b) in " + str(output))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    render(args.run, args.output_dir)
