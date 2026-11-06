"""Hardware test run, version 2. See Project_Minutes.md (4 Nov 2026) for why this replaced version 1.

Changes from version 1:
  - Qubits are chosen from live calibration data. Version 1 set no layout, so the
    transpiler used physical qubits 0 and 1 regardless of their quality.
  - Each job also measures two control circuits (prepare all-0, prepare all-1) on the
    same qubits. These separate readout errors from gate errors.
  - The calibration values for the chosen qubits are saved with each run.

Usage:
  python Test_run_hardware_2.py          real hardware (uses QPU time)
  python Test_run_hardware_2.py --test   local noisy simulation (uses no QPU time)
"""
import json
import sys
from datetime import datetime, timezone
from math import sqrt
from pathlib import Path

import qiskit
import qiskit_ibm_runtime
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler

SHOTS = 500
OPT_LEVEL = 1
BACKEND_NAME = "ibm_kingston"   # same device as the 20 Oct runs; None = least busy device
CONTROL_WARN = 0.05             # flag a control circuit that is wrong more often than this
TEST_LOCALLY = "--test" in sys.argv


def control_circuit(num_qubits, excited):
    """Prepare all qubits in |0> or |1> and measure. Should give one outcome only."""
    circ = QuantumCircuit(num_qubits)
    if excited:
        circ.x(range(num_qubits))
    circ.measure_all()
    return circ


def single_qubit_score(target, q):
    """Combined readout and SX error for qubit q, or None if either is unavailable."""
    try:
        readout, sx = target["measure"][(q,)].error, target["sx"][(q,)].error
    except KeyError:
        return None
    return None if readout is None or sx is None else readout + sx


def best_qubit(target):
    scores = {q: single_qubit_score(target, q) for q in range(target.num_qubits)}
    return min((q for q, s in scores.items() if s is not None), key=scores.get)


def best_pair(target):
    gate = next(g for g in ("cz", "ecr", "cx") if g in target.operation_names)
    scores = {}
    for (a, b), props in target[gate].items():
        score_a, score_b = single_qubit_score(target, a), single_qubit_score(target, b)
        if props is not None and props.error is not None and None not in (score_a, score_b):
            scores[(a, b)] = props.error + score_a + score_b
    return list(min(scores, key=scores.get))


def calibration_snapshot(target, qubits):
    """The calibration values these qubits were selected on."""
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


# For Experiment A: one qubit, expected 50/50 between 0 and 1
circ_a = QuantumCircuit(1)
circ_a.h(0)
circ_a.measure_all()

# For Experiment B: Bell state, expected 50/50 between 00 and 11
circ_b = QuantumCircuit(2)
circ_b.h(0)
circ_b.cx(0, 1)
circ_b.measure_all()

if TEST_LOCALLY:
    from qiskit_ibm_runtime.fake_provider import FakeMarrakesh
    backend = FakeMarrakesh()
    out = Path(__file__).resolve().parent / "test_runs"
else:
    service = QiskitRuntimeService()
    backend = (service.backend(BACKEND_NAME) if BACKEND_NAME
               else service.least_busy(operational=True, simulator=False, min_num_qubits=2))
    out = Path(__file__).resolve().parent / "hardware_runs"
print("Using:", backend.name)

target = backend.target
experiments = {"A": (circ_a, [best_qubit(target)]), "B": (circ_b, best_pair(target))}
sampler = Sampler(mode=backend)

# Submit both jobs first, then collect results
submitted = {}
for name, (circ, qubits) in experiments.items():
    pm = generate_preset_pass_manager(
        backend=backend, optimization_level=OPT_LEVEL, initial_layout=qubits
    )
    n = circ.num_qubits
    # PUB order: 0 = experiment, 1 = prepare all-0, 2 = prepare all-1
    isa = pm.run([circ, control_circuit(n, False), control_circuit(n, True)])
    job = sampler.run(isa, shots=SHOTS)
    submitted_utc = datetime.now(timezone.utc).isoformat()
    submitted[name] = (job, isa[0], qubits, submitted_utc)
    print(name, "on physical qubits", qubits, "submitted, job ID:", job.job_id())

out.mkdir(exist_ok=True)

for name, (job, isa, qubits, submitted_utc) in submitted.items():
    result = job.result()
    n = len(qubits)

    # Save the raw shots FIRST so nothing later can stop them being written
    for label, pub in zip(("shots", "prep0_shots", "prep1_shots"), result):
        (out / f"hw_{name}_{job.job_id()}_{label}.txt").write_text(
            "\n".join(pub.data.meas.get_bitstrings())
        )

    counts = result[0].data.meas.get_counts()
    error_prep0 = 1 - result[1].data.meas.get_counts().get("0" * n, 0) / SHOTS
    error_prep1 = 1 - result[2].data.meas.get_counts().get("1" * n, 0) / SHOTS

    # Distance of the all-1 outcome share from the ideal 50%, in standard deviations
    if name == "A":
        z_from_50_50 = (counts.get("1", 0) - SHOTS / 2) / sqrt(SHOTS / 4)
    else:
        share_of_correct = counts.get("00", 0) + counts.get("11", 0)
        z_from_50_50 = (counts.get("11", 0) - share_of_correct / 2) / sqrt(max(share_of_correct, 1) / 4)

    print(f"\nExperiment {name}: {counts}")
    print(f"  P(wrong | prepared all-0) = {error_prep0:.3f}")
    print(f"  P(wrong | prepared all-1) = {error_prep1:.3f}")
    print(f"  Distance from 50/50: {z_from_50_50:.1f} sigma")
    if name == "B":
        print(f"  Correlated outcomes (00 or 11): {share_of_correct / SHOTS:.3f}")
    if max(error_prep0, error_prep1) > CONTROL_WARN:
        print(f"  WARNING: a control circuit is wrong more than {CONTROL_WARN:.0%} of the time, "
              "so these qubits were not performing as calibrated during this run.")

    # Usage metrics are nice-to-have: never let them break the run
    try:
        metrics = job.metrics()
    except Exception as e:
        metrics = {"error": str(e)}

    meta = {
        "experiment": name,
        "backend": backend.name,
        "job_id": job.job_id(),
        "shots": SHOTS,
        "physical_qubits": isa.layout.initial_index_layout(filter_ancillas=True),
        "calibration_at_selection": calibration_snapshot(target, qubits),
        "control_error_prep0": error_prep0,
        "control_error_prep1": error_prep1,
        "optimization_level": OPT_LEVEL,
        "submitted_utc": submitted_utc,
        "results_saved_utc": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,                 # stored exactly as IBM returns it
        "qiskit": qiskit.__version__,
        "qiskit_ibm_runtime": qiskit_ibm_runtime.__version__,
    }
    (out / f"hw_{name}_{job.job_id()}_meta.json").write_text(
        json.dumps(meta, indent=2, default=str)
    )

print("\nSaved raw shots and metadata to:", out.resolve())
