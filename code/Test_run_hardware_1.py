import json
from datetime import datetime, timezone
from pathlib import Path

import qiskit
import qiskit_ibm_runtime
from qiskit import QuantumCircuit
from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager
from qiskit_ibm_runtime import QiskitRuntimeService, SamplerV2 as Sampler

SHOTS = 500
OPT_LEVEL = 1

# For Experiment A: one qubit,
circ_a = QuantumCircuit(1)
circ_a.h(0)
circ_a.measure_all()

# For Experiment B: Bell state
circ_b = QuantumCircuit(2)
circ_b.h(0)
circ_b.cx(0, 1)
circ_b.measure_all()

# Connect (uses the credentials you saved earlier) and pick the least busy device
service = QiskitRuntimeService()
backend = service.least_busy(operational=True, simulator=False, min_num_qubits=2)
print("Using:", backend.name)

# Transpile for the chosen device, then set up the sampler
pm = generate_preset_pass_manager(backend=backend, optimization_level=OPT_LEVEL)
sampler = Sampler(mode=backend)

# Submit both jobs first, then collect results
submitted = {}
for name, circ in [("A", circ_a), ("B", circ_b)]:
    isa = pm.run(circ)
    job = sampler.run([isa], shots=SHOTS)
    submitted_utc = datetime.now(timezone.utc).isoformat()
    submitted[name] = (job, isa, submitted_utc)
    print(name, "submitted, job ID:", job.job_id())

out = Path("hardware_runs")
out.mkdir(exist_ok=True)

for name, (job, isa, submitted_utc) in submitted.items():
    data = job.result()[0].data.meas
    bitstrings = data.get_bitstrings()      # one string per shot, in order
    counts = data.get_counts()
    print(name, counts)

    # Save the raw shots FIRST so nothing later can stop them being written
    (out / f"hw_{name}_{job.job_id()}_shots.txt").write_text("\n".join(bitstrings))

    # Usage metrics are nice-to-have: never let them break the run
    try:
        metrics = job.metrics()
    except Exception as e:
        metrics = {"error": str(e)}
    print(f"Experiment {name} metrics:", metrics)

    meta = {
        "experiment": name,
        "backend": backend.name,
        "job_id": job.job_id(),
        "shots": SHOTS,
        "physical_qubits": isa.layout.initial_index_layout(filter_ancillas=True),
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

print("Saved raw shots and metadata to:", out.resolve())