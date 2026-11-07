"""Hardware test run, version 3.

Changes from version 2:
  - Candidate qubits: each experiment runs on several candidate qubits (or pairs)
    in the same job. The main result comes from the candidate with the lowest
    measured readout error in that job. This is decided from the calibration
    circuits only, never from the experiment's outcome.
  - Reference qubits: qubit 0 (Experiment A) and the pair 0, 1 (Experiment B)
    also run in every job but are never chosen. This compares them with the
    chosen qubits at the same time.
  - Full readout calibration: every basis state is prepared and measured, so
    readout errors can be corrected. Raw and corrected results are both reported,
    with bootstrap standard errors.
  - Defined expectation: each result is compared with the ideal value and with a
    noise model built from IBM's calibration data. A result counts as expected
    when it is within Z_LIMIT standard errors of the model.
  - Drift check: each experiment's shots are split into blocks and tested for a
    change during the run.
  - Jobs are tagged automatically: "Test N using Code 3 - Experiment X (...)".

Measurement twirling is left off on purpose. It would push Experiment A towards
50/50 even with a faulty qubit, hiding the readout bias this code is meant to find.

Usage:
  python Test_run_hardware_3.py --test      local noisy simulation (no QPU time)
  python Test_run_hardware_3.py --dry-run   real device: choose qubits and predict
                                            results, but submit nothing
  python Test_run_hardware_3.py             real hardware (asks before submitting)
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import qiskit
import qiskit_aer
import qiskit_ibm_runtime
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_aer import AerSimulator
from qiskit_aer.noise import NoiseModel
from qiskit_ibm_runtime import SamplerV2 as Sampler
from scipy.stats import chi2

SHOTS = 1000                    # per circuit
OPT_LEVEL = 1
BACKEND_NAME = "ibm_kingston"
CANDIDATES = 3                  # candidate qubits (or pairs) per experiment
REFERENCE = {"A": [0], "B": [0, 1]}   # always run, never chosen
Z_LIMIT = 3.0                   # "as expected" means within this many standard errors
BOOTSTRAP = 2000                # resamples for standard errors
MODEL_SHOTS = 200_000           # noise-model simulation shots
DRIFT_BLOCKS = 10
DRIFT_ALPHA = 0.01              # flag drift when the block test's p-value is below this
SEED = 1234

DESCRIPTIONS = {"A": "H gate", "B": "Bell state"}
IDEAL = {"A": {"P(1)": 0.5},
         "B": {"P(00 or 11)": 1.0, "P(11 | 00 or 11)": 0.5}}


# ---------------------------------------------------------------- circuits --
def experiment_circuit(name):
    """A: H then measure (expect 50/50). B: Bell state (expect 00 and 11 only, 50/50)."""
    circ = QuantumCircuit(1 if name == "A" else 2)
    circ.h(0)
    if name == "B":
        circ.cx(0, 1)
    circ.measure_all()
    return circ


def basis_circuit(num_qubits, index):
    """Prepare the basis state whose bitstring is `index` (Qiskit bit order) and measure."""
    circ = QuantumCircuit(num_qubits)
    for k in range(num_qubits):
        if index >> k & 1:
            circ.x(k)
    circ.measure_all()
    return circ


# -------------------------------------------------------- qubit candidates --
def qubit_score(target, q):
    """Combined readout and SX error for qubit q, or None if either is unavailable."""
    try:
        readout, sx = target["measure"][(q,)].error, target["sx"][(q,)].error
    except KeyError:
        return None
    return None if readout is None or sx is None else readout + sx


def two_qubit_gate(target):
    return next(g for g in ("cz", "ecr", "cx") if g in target.operation_names)


def candidate_layouts(target, name):
    """Best CANDIDATES layouts by calibration score, plus the reference layout."""
    if name == "A":
        scores = {(q,): qubit_score(target, q) for q in range(target.num_qubits)}
    else:
        gate, scores = two_qubit_gate(target), {}
        for (a, b), props in target[gate].items():
            sa, sb = qubit_score(target, a), qubit_score(target, b)
            if props is None or props.error is None or None in (sa, sb):
                continue
            if (b, a) not in scores:   # count each physical pair once
                scores[(a, b)] = props.error + sa + sb
    ranked = sorted((k for k, s in scores.items() if s is not None), key=scores.get)

    reference = tuple(REFERENCE[name])
    layouts = [{"qubits": list(k), "reference": False, "score": scores[k]}
               for k in ranked if set(k) != set(reference)][:CANDIDATES]
    ref_key = next((k for k in scores if set(k) == set(reference)), None)
    if ref_key is not None:
        layouts.append({"qubits": list(reference), "reference": True, "score": scores[ref_key]})
    return layouts


def calibration_snapshot(target, qubits):
    snapshot = {}
    for q in qubits:
        props = target.qubit_properties[q] if target.qubit_properties else None
        snapshot[q] = {
            "readout_error": target["measure"][(q,)].error,
            "sx_error": target["sx"][(q,)].error,
            "t1_s": getattr(props, "t1", None),
            "t2_s": getattr(props, "t2", None),
        }
    return snapshot


# -------------------------------------------------------------- statistics --
def counts_vector(bitstrings, num_qubits):
    return np.bincount([int(b, 2) for b in bitstrings], minlength=2 ** num_qubits).astype(float)


def metrics(name, p):
    if name == "A":
        return {"P(1)": float(p[1])}
    correlated = p[0] + p[3]
    return {"P(00 or 11)": float(correlated),
            "P(11 | 00 or 11)": float(p[3] / correlated) if correlated > 0 else float("nan")}


def confusion_matrix(cal_counts):
    """Column j holds P(read i | prepared j)."""
    return np.column_stack([c / c.sum() for c in cal_counts])


def mitigate(matrix, counts):
    """Undo readout error, then clip to a valid probability distribution."""
    p = np.linalg.lstsq(matrix, counts / counts.sum(), rcond=None)[0]
    p = np.clip(p, 0, None)
    return p / p.sum()


def bootstrap_se(name, exp_counts, cal_counts, rng):
    """Standard errors of raw and corrected metrics, resampling all circuits' shots."""
    raw, corrected = [], []
    n, p_exp = exp_counts.sum(), exp_counts / exp_counts.sum()
    for _ in range(BOOTSTRAP):
        e = rng.multinomial(int(n), p_exp).astype(float)
        cals = [rng.multinomial(int(c.sum()), c / c.sum()).astype(float) for c in cal_counts]
        raw.append(metrics(name, e / n))
        corrected.append(metrics(name, mitigate(confusion_matrix(cals), e)))
    se = lambda rows, k: float(np.nanstd([r[k] for r in rows], ddof=1))
    return {k: se(raw, k) for k in raw[0]}, {k: se(corrected, k) for k in corrected[0]}


_NOISE_SIMULATORS = {}


def model_predictions(backend, isas, name, num_qubits, readout, rng):
    """Expected metrics and standard errors from IBM's calibration-based noise model.

    All circuits go in one simulator call, because each call has a fixed cost of
    several seconds for handling the noise model of every qubit on the device.
    """
    key = (id(backend), readout)
    if key not in _NOISE_SIMULATORS:   # building the model for every qubit is slow, so do it once
        noise = NoiseModel.from_backend(backend, readout_error=readout)
        _NOISE_SIMULATORS[key] = AerSimulator(noise_model=noise, seed_simulator=SEED)
    result = _NOISE_SIMULATORS[key].run(list(isas), shots=MODEL_SHOTS).result()
    predictions = []
    for i in range(len(isas)):
        counts = np.zeros(2 ** num_qubits)
        for bits, value in result.get_counts(i).items():
            counts[int(bits.replace(" ", ""), 2)] += value
        p = counts / counts.sum()
        resampled = [metrics(name, rng.multinomial(MODEL_SHOTS, p) / MODEL_SHOTS) for _ in range(300)]
        predictions.append((metrics(name, p),
                            {k: float(np.nanstd([r[k] for r in resampled], ddof=1)) for k in resampled[0]}))
    return predictions


def z_score(value, se_a, expected, se_b=0.0):
    se = np.hypot(se_a, se_b)
    return float((value - expected) / se) if se > 0 else float("nan")


def drift_p_value(name, bitstrings):
    """Chi-squared test that the success rate is constant across DRIFT_BLOCKS blocks."""
    outcomes = np.array([int(b, 2) for b in bitstrings])
    success = outcomes == 1 if name == "A" else (outcomes == 0) | (outcomes == 3)
    blocks = np.array_split(success, DRIFT_BLOCKS)
    p = success.mean()
    if p in (0.0, 1.0):
        return 1.0
    stat = sum((b.sum() - len(b) * p) ** 2 / (len(b) * p * (1 - p)) for b in blocks)
    return float(chi2.sf(stat, DRIFT_BLOCKS - 1))


# --------------------------------------------------------------- the plan ---
def build_plan(backend, name):
    """Transpile the experiment and calibration circuits for every candidate layout."""
    target = backend.target
    n = 1 if name == "A" else 2
    plan = []
    for layout in candidate_layouts(target, name):
        pm = generate_preset_pass_manager(backend=backend, optimization_level=OPT_LEVEL,
                                          initial_layout=layout["qubits"])
        circuits = [experiment_circuit(name)] + [basis_circuit(n, j) for j in range(2 ** n)]
        isa = pm.run(circuits)
        used = isa[0].layout.initial_index_layout(filter_ancillas=True)
        if used != layout["qubits"]:
            raise RuntimeError(f"Transpiler used {used}, not the requested {layout['qubits']}")
        plan.append({**layout, "isa": isa})
    return plan


def layout_label(qubits):
    return "q" + "-q".join(str(q) for q in qubits)


def analyse(backend, name, plan, result, rng):
    """Analyse every candidate, then choose the main one by measured control error only."""
    n = 1 if name == "A" else 2
    per_circuit = 1 + 2 ** n
    experiments = [layout["isa"][0] for layout in plan]
    full_models = model_predictions(backend, experiments, name, n, True, rng)
    gate_models = model_predictions(backend, experiments, name, n, False, rng)
    rows = []
    for i, layout in enumerate(plan):
        pubs = result[i * per_circuit:(i + 1) * per_circuit]
        bitstrings = [pub.data.meas.get_bitstrings() for pub in pubs]
        exp_counts = counts_vector(bitstrings[0], n)
        cal_counts = [counts_vector(b, n) for b in bitstrings[1:]]
        matrix = confusion_matrix(cal_counts)
        raw = metrics(name, exp_counts / exp_counts.sum())
        corrected = metrics(name, mitigate(matrix, exp_counts))
        se_raw, se_corrected = bootstrap_se(name, exp_counts, cal_counts, rng)
        model_full, se_full = full_models[i]
        model_gates, se_gates = gate_models[i]

        checks = {}
        for k in raw:
            checks[k] = {
                "raw": raw[k], "raw_se": se_raw[k],
                "corrected": corrected[k], "corrected_se": se_corrected[k],
                "ideal": IDEAL[name][k],
                "model_full": model_full[k], "model_gates_only": model_gates[k],
                "z_raw_vs_model": z_score(raw[k], se_raw[k], model_full[k], se_full[k]),
                "z_corrected_vs_model": z_score(corrected[k], se_corrected[k], model_gates[k], se_gates[k]),
                "z_raw_vs_ideal": z_score(raw[k], se_raw[k], IDEAL[name][k]),
                "z_corrected_vs_ideal": z_score(corrected[k], se_corrected[k], IDEAL[name][k]),
            }
        rows.append({
            "qubits": layout["qubits"], "reference": layout["reference"],
            "calibration_score": layout["score"],
            "control_error": float(1 - np.mean(np.diag(matrix))),
            "confusion_matrix": matrix.tolist(),
            "experiment_counts": exp_counts.tolist(),
            "drift_p_value": drift_p_value(name, bitstrings[0]),
            "checks": checks,
            "expected_raw": all(abs(c["z_raw_vs_model"]) <= Z_LIMIT for c in checks.values()),
            "expected_corrected": all(abs(c["z_corrected_vs_model"]) <= Z_LIMIT for c in checks.values()),
            "shots": {label: b for label, b in zip(
                ["experiment"] + [f"prep_{j:0{n}b}" for j in range(2 ** n)], bitstrings)},
        })
    eligible = [r for r in rows if not r["reference"]]
    main = min(eligible, key=lambda r: r["control_error"])
    for r in rows:
        r["main_result"] = r is main
    return rows


def print_report(name, rows):
    print(f"\n=== Experiment {name} ({DESCRIPTIONS[name]}) ===")
    for r in rows:
        role = "MAIN" if r["main_result"] else ("reference" if r["reference"] else "candidate")
        print(f"\n[{role}] {layout_label(r['qubits'])}: control error {r['control_error']:.3%}, "
              f"drift p = {r['drift_p_value']:.3f}"
              + ("  <-- possible drift" if r["drift_p_value"] < DRIFT_ALPHA else ""))
        for k, c in r["checks"].items():
            print(f"  {k}: raw {c['raw']:.4f} +/- {c['raw_se']:.4f} (model {c['model_full']:.4f}, "
                  f"z = {c['z_raw_vs_model']:+.1f}) | corrected {c['corrected']:.4f} +/- "
                  f"{c['corrected_se']:.4f} (model {c['model_gates_only']:.4f}, "
                  f"z = {c['z_corrected_vs_model']:+.1f}) | ideal {c['ideal']}")
        verdict = lambda ok: "as expected" if ok else f"NOT as expected (beyond {Z_LIMIT:g} SE)"
        print(f"  Raw: {verdict(r['expected_raw'])}. Corrected: {verdict(r['expected_corrected'])}.")


# ------------------------------------------------------------------ running --
def next_test_number(service):
    numbers = [int(m.group(1)) for job in service.jobs(limit=100) for tag in (job.tags or [])
               if (m := re.match(r"Test (\d+) using", tag))]
    return max(numbers, default=0) + 1


def run(backend, run_mode, out_dir, names=("A", "B"), tags=None, confirm=None, local=False):
    """Plan, submit, analyse and save. Returns {experiment: rows}."""
    plans = {name: build_plan(backend, name) for name in names}
    total = sum(len(p) * len(p[0]["isa"]) for p in plans.values())
    print("Using:", backend.name)
    for name, plan in plans.items():
        print(f"Experiment {name} layouts:",
              ", ".join(layout_label(l["qubits"]) + (" (reference)" if l["reference"] else "")
                        for l in plan))
    print(f"{total} circuits x {SHOTS} shots = {total * SHOTS:,} shots in total")
    if confirm is not None and not confirm():
        print("Cancelled. Nothing was submitted.")
        return None

    sampler = Sampler(mode=run_mode)
    sampler.options.twirling.enable_measure = False
    sampler.options.twirling.enable_gates = False
    sampler.options.dynamical_decoupling.enable = False
    if local:
        sampler.options.simulator.seed_simulator = SEED   # repeatable local runs

    submitted = {}
    for name, plan in plans.items():
        tag = tags[name] if tags else None
        if tag:
            sampler.options.environment.job_tags = [tag]
        job = sampler.run([c for layout in plan for c in layout["isa"]], shots=SHOTS)
        submitted[name] = (job, tag, datetime.now(timezone.utc).isoformat())
        print(f"Experiment {name} submitted, job ID: {job.job_id()}" + (f", tag: {tag}" if tag else ""))

    out_dir.mkdir(exist_ok=True)
    rng = np.random.default_rng(SEED)
    all_rows = {}
    for name, (job, tag, submitted_utc) in submitted.items():
        result = job.result()
        # Save the raw shots FIRST so nothing later can stop them being written
        shots_path = out_dir / f"hw3_{name}_{job.job_id()}_shots.json"
        n = 1 if name == "A" else 2
        shots_path.write_text(json.dumps({
            f"{layout_label(l['qubits'])}/{label}": result[i * (1 + 2 ** n) + j].data.meas.get_bitstrings()
            for i, l in enumerate(plans[name])
            for j, label in enumerate(["experiment"] + [f"prep_{k:0{n}b}" for k in range(2 ** n)])
        }))

        rows = analyse(backend, name, plans[name], result, rng)
        print_report(name, rows)
        try:
            metrics_ibm = job.metrics()
        except Exception as e:
            metrics_ibm = {"error": str(e)}
        meta = {
            "experiment": name, "code_version": 3, "tag": tag,
            "backend": backend.name, "job_id": job.job_id(), "shots_per_circuit": SHOTS,
            "optimization_level": OPT_LEVEL, "z_limit": Z_LIMIT, "bootstrap": BOOTSTRAP,
            "model_shots": MODEL_SHOTS, "seed": SEED,
            "twirling": False, "dynamical_decoupling": False,
            "submitted_utc": submitted_utc,
            "results_saved_utc": datetime.now(timezone.utc).isoformat(),
            "calibration_at_selection": calibration_snapshot(
                backend.target, sorted({q for l in plans[name] for q in l["qubits"]})),
            "results": [{k: v for k, v in r.items() if k != "shots"} for r in rows],
            "metrics": metrics_ibm,
            "qiskit": qiskit.__version__, "qiskit_aer": qiskit_aer.__version__,
            "qiskit_ibm_runtime": qiskit_ibm_runtime.__version__,
        }
        (out_dir / f"hw3_{name}_{job.job_id()}_meta.json").write_text(json.dumps(meta, indent=2, default=str))
        all_rows[name] = rows
    print("\nSaved raw shots and results to:", out_dir.resolve())
    return all_rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="local noisy simulation, no QPU time")
    mode.add_argument("--dry-run", action="store_true", help="plan on the real device without submitting")
    parser.add_argument("--yes", action="store_true", help="submit without asking")
    parser.add_argument("--only", choices=["A", "B"], help="run one experiment")
    args = parser.parse_args(argv)
    names = (args.only,) if args.only else ("A", "B")
    here = Path(__file__).resolve().parent

    if args.test:
        from qiskit_ibm_runtime.fake_provider import FakeMarrakesh
        backend = FakeMarrakesh()
        return run(backend, backend, here / "test_runs", names, local=True)

    from qiskit_ibm_runtime import QiskitRuntimeService
    service = QiskitRuntimeService()
    backend = service.backend(BACKEND_NAME)
    if args.dry_run:
        rng = np.random.default_rng(SEED)
        for name in names:
            n = 1 if name == "A" else 2
            print(f"\nExperiment {name}: noise-model predictions from current calibration")
            plan = build_plan(backend, name)
            predictions = model_predictions(backend, [l["isa"][0] for l in plan], name, n, True, rng)
            for layout, (full, _) in zip(plan, predictions):
                print(f"  {layout_label(layout['qubits'])}{' (reference)' if layout['reference'] else ''}: "
                      + ", ".join(f"{k} = {v:.4f}" for k, v in full.items()))
        print("\nDry run only: nothing was submitted.")
        return None

    first = next_test_number(service)
    tags = {name: f"Test {first + i} using Code 3 - Experiment {name} ({DESCRIPTIONS[name]})"
            for i, name in enumerate(names)}
    confirm = None if args.yes else (
        lambda: input("Submit these jobs to real hardware? This uses QPU time. [y/N] ").strip().lower()
        in ("y", "yes"))
    return run(backend, backend, here / "hardware_runs", names, tags, confirm)


if __name__ == "__main__":
    main()
