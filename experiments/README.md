# Execution-coupling experiments

Start with [the complete-workflow guide](../docs/CHAIN-EVALUATION.md). It covers installation,
the physical profile and its sources, full and quick runs, three comparison models,
statistical aggregation and figure generation.

- [chain-profile.json](chain-profile.json): current complete-workflow settings.
- [run_chain_evaluation.py](run_chain_evaluation.py): all models through final teleportation.
- [render_chain_evaluation.py](render_chain_evaluation.py): Figures 3, 4, 5(a), 5(b).

The earlier swapping-stage comparison remains available:

- [profile.json](profile.json): complete experiment configuration.
- [run_evaluation.py](run_evaluation.py): native simulation, calibration and validation.
- [render_evaluation.py](render_evaluation.py): audit and plot a completed campaign.
- [requirements-plot.txt](requirements-plot.txt): optional plotting dependencies.

Run simulations with the NetSquid Python environment. Plotting can use a separate
Python 3.10 environment without NetSquid. Outputs are generated locally and are
not part of this source repository.
