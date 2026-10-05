# Installation

## Tested environment

| Component | Version |
|---|---|
| OS | Ubuntu 22.04, x86-64 Linux |
| ns-3 | 3.47 (tag `ns-3.47`) |
| Q2NS | `f22ba28f437099ba3cf9956ca332ba5ce8bb14fd` plus the included patches |
| C++ toolchain | GCC 11, C++23 mode selected by Q2NS |
| CMake | 3.22 |
| ns-3 build interpreter | Python 3.10 |
| Quantum runtime | Python 3.7.17, NetSquid 1.1.7, NumPy 1.21.6 |

The exact NetSquid version matters: newer releases or a different Python ABI
need their own compatibility check. The ns-3 wrapper uses the system Python;
the quantum runner uses a separate NetSquid environment. Python 3.7 is retained
here to reproduce the tested NetSquid environment; it is no longer maintained.

Official dependency instructions:
[ns-3](https://www.nsnam.org/docs/installation/singlehtml/index.html),
[Q2NS](https://github.com/QuantumInternet-it/q2ns),
[NetSquid](https://netsquid.org/).

## 1. System packages and source checkouts

On Ubuntu 22.04:

```bash
sudo apt update
sudo apt install -y git g++ cmake ninja-build python3 python3-venv pkg-config

mkdir -p "$HOME/qucl-work"
cd "$HOME/qucl-work"
git clone --branch ns-3.47 --depth 1 https://gitlab.com/nsnam/ns-3-dev.git ns-3.47
cd ns-3.47
git clone https://github.com/QuantumInternet-it/q2ns.git contrib/q2ns
git -C contrib/q2ns checkout --detach f22ba28f437099ba3cf9956ca332ba5ce8bb14fd
git clone https://github.com/johnlimmm/qucl.git contrib/cosim
```

Keep the `contrib/cosim` directory name: it is the ns-3 module name used by the
build and example runners. The repository name on GitHub is QuCl.

Q2NS includes its own required C++ quantum-backend headers. Those headers are
needed to build Q2NS even though QuCl delegates external-session quantum states
to NetSquid.

## 2. Apply the Q2NS integration patches

From the ns-3 root:

```bash
python3 contrib/cosim/scripts/apply_q2ns_patches.py contrib/q2ns
```

The script verifies the pinned Q2NS commit, checks both patches before applying
them, and verifies the resulting file hashes. Re-running on the same patched
checkout verifies it without applying the patches twice. A conflicting checkout
is rejected. Use a separate clean checkout to preserve unrelated Q2NS edits.

The patches add optional asynchronous BSM/correction callbacks and logical
resource handles to SwapApp and TeleportationApp. They preserve the apps' native
quantum execution mode and existing measurement payloads.

## 3. Install NetSquid in a separate Python environment

Register at [NetSquid](https://netsquid.org/) and accept its license terms.
NetSquid is distributed through its authenticated package index; it is not
included in this repository. Use the credentials associated with that account
when pip prompts. Do not put passwords or access tokens in commands or source.

With an existing Python 3.7.17 interpreter:

```bash
python3.7 -m venv "$HOME/.venvs/qucl"
export QUCL_PYTHON="$HOME/.venvs/qucl/bin/python"
"$QUCL_PYTHON" -m pip install --upgrade 'pip<24.1'
"$QUCL_PYTHON" -m pip install --extra-index-url https://pypi.netsquid.org \
  'netsquid==1.1.7' 'numpy==1.21.6'
```

Alternatively, if Conda is already installed, create the tested interpreter with
`conda create -n qucl python=3.7.17 pip`, then use that environment's absolute
Python path as `QUCL_PYTHON`. Keep the system Python available for `./ns3`.

If pip reports that no compatible NetSquid wheel exists, check account access,
the Python version and platform against the NetSquid installation instructions.
An arbitrary newer Python environment is not a substitute for a compatible wheel.

## 4. Build the participants and native tests

From the ns-3 root, with the system Python on `PATH`:

```bash
./ns3 configure --enable-examples --enable-tests \
  --enable-modules=cosim,q2ns --disable-python-bindings
./ns3 build cosim-chained cosim-provisioned q2ns-test test-runner -j 2
```

The Python bindings for ns-3 are not required: QuCl communicates with a separate
ns-3 executable through a local Unix socket. Both processes run on one host.
If ccache reports an unwritable temporary directory, create a writable directory
and set `CCACHE_TEMPDIR` before building.

## 5. Verify and run

```bash
"$QUCL_PYTHON" contrib/cosim/scripts/check_environment.py
"$QUCL_PYTHON" contrib/cosim/python/run_chained.py \
  contrib/cosim/scenarios/chain.json --output results/chain.json
```

The second command checks both execution and the independent state reference.
See [Examples](EXAMPLES.md) for the remaining examples, tests and parameter sweeps.

## Using a checkout outside ns-3

The standard installation above is simplest. For a linked development checkout,
set `QUCL_NS3_ROOT` to the absolute ns-3 root so the runners find the correct build.
You can also pass `--ns3-binary` to either runner. A complete installed and patched
ns-3/Q2NS build is still required.
