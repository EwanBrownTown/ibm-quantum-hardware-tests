import statistics
from qiskit_ibm_runtime import QiskitRuntimeService

backend = QiskitRuntimeService().backend("ibm_kingston")

errs = {}
for q in range(backend.num_qubits):
    try:
        e = backend.target["measure"][(q,)].error
    except KeyError:
        continue
    if e is not None:
        errs[q] = e

print("Qubit 0 readout error:", errs.get(0))
print("Median readout error:", statistics.median(errs.values()))
best = sorted(errs, key=errs.get)[:5]
print("Best 5 qubits:", [(q, round(errs[q], 4)) for q in best])