# Testing Quantum Circuits on IBM Hardware

Running simple quantum circuits on IBM's 156-qubit `ibm_kingston` processor, finding out why the results differ from theory, and building controls into the experiments to diagnose it. This is part of my 4th year university project, and it is ongoing.

## Key results

My first runs gave a 69–71% share of 1s where theory predicts 50%, even though the circuit was correct. Rewriting the experiment to select qubits from live calibration data and add control circuits gave a result consistent with 50%.

![Share of shots measured as 1 in each single-qubit run, compared with the ideal 50%](docs/experiment_a_results.png)

### Experiment A: single-qubit superposition

A Hadamard gate followed by a measurement, which should give 0 and 1 equally.

| Test | Code | Physical qubit | Read 0 | Read 1 | P(1), 95% CI | Distance from 50/50 |
|---|---|---|---|---|---|---|
| 1 | 1 | 0 | 144 | 356 | 0.712 ± 0.040 | 9.5σ |
| 3 | 1 | 0 | 155 | 345 | 0.690 ± 0.041 | 8.5σ |
| 5 | 2 | 59 | 241 | 259 | 0.518 ± 0.044 | 0.8σ |

In Test 5's control circuits, qubit 59 gave the wrong outcome in 0.2% of shots when prepared in |0⟩ and 0.4% when prepared in |1⟩.

### Experiment B: Bell state

A Hadamard and CNOT, which should give only `00` and `11`, with equal probability.

| Test | Code | Physical qubits | 00 | 01 | 10 | 11 | Correlated outcomes |
|---|---|---|---|---|---|---|---|
| 2 | 1 | 0, 1 | 95 | 89 | 55 | 261 | 71.2% |
| 4 | 1 | 0, 1 | 106 | 89 | 51 | 254 | 72.0% |
| 6 | 2 | 147, 148 | 241 | 5 | 40 | 214 | 91.0% |

Bitstrings read second qubit then first. All tests used 500 shots per circuit.

## What this does and doesn't show

- **The early bias was not shot noise or a circuit error.** It was far outside statistical expectation, and the transpiled circuit gives exactly 50/50 in a noiseless simulation.
- **IBM's calibration did not predict it.** Three minutes before Test 1, qubit 0 was listed with 0.85% readout error and 0.03% SX error.
- **The cause is not yet established.** Test 5 changed the qubit and the code, and also ran 15 days later, so these effects can't be separated.
- **Test 5 is a single run and may show drift.** Its counts of 1 per 100 shots were 59, 60, 48, 52 and 40 (χ² = 10.9, 4 d.o.f., p = 0.027). This may be chance, and needs repeat runs.
- **Test 6 is improved but not clean.** One control circuit was wrong 11.8% of the time, mostly because qubit 147 read 0 when prepared in |1⟩.

## How the experiments work

Code 2 improves on Code 1 in three main ways:

- **Qubit selection:** it picks qubits with the lowest combined error from live calibration data, rather than leaving the transpiler to use qubits 0 and 1.
- **Control circuits:** each job also measures prepare-all-0 and prepare-all-1 circuits on the same qubits, which gives a direct check on readout.
- **Provenance:** every run saves its raw shots, job ID, software versions and the calibration values used to select its qubits.

Code 3 goes further, aiming for results that match theory within a stated error:

- **Measured qubit choice:** each experiment runs on three candidate qubits (or pairs) in the same job. The main result comes from the candidate with the lowest measured readout error, chosen from the calibration circuits alone, never from the experiment's outcome. All candidates are reported.
- **Reference qubits:** qubit 0 (or the pair 0, 1) runs in the same job but is never chosen. This compares it with the selected qubits at the same time, separating the qubit from the date.
- **Readout correction:** every basis state is prepared and measured, giving a full confusion matrix. Raw and corrected results are both reported, with bootstrap standard errors.
- **A defined expectation:** each result is compared with the ideal value and with a noise model built from IBM's calibration data. It counts as expected when it is within 3 standard errors.
- **Drift check:** each experiment's shots are split into 10 blocks and tested for a change during the run.

Measurement twirling is deliberately left off, because it would push Experiment A towards 50/50 even on a faulty qubit, hiding the problem rather than fixing it.

The tests in `tests/` simulate a fault like the one seen on 20 October: |0⟩ read as 1 in 40% of shots. They check that Code 3 detects the fault, avoids that qubit, and that readout correction recovers 50/50. Code 3 has been tested on simulators only and has not yet run on hardware.

## Repository layout

| Path | Contents |
|---|---|
| `code/Test_run_hardware_1.py` | Code 1: the original experiment, used for Tests 1–4 |
| `code/Test_run_hardware_2.py` | Code 2: qubit selection and control circuits |
| `code/Test_run_hardware_3.py` | Code 3: candidate qubits, reference qubits, readout correction and a noise-model comparison |
| `code/Calibration.py` | Reads the current readout errors for `ibm_kingston` |
| `tests/` | Simulator tests for Code 3, including a simulated readout fault |
| `code/hardware_runs/` | Raw shots and metadata for Tests 5 and 6 |
| `docs/` | Figures |

Raw shot files for Tests 1–4 aren't included yet. The counts above come from IBM's job records.

## Running it

Tested with Python 3.11.

```bash
pip install -r requirements.txt
```

To run a local noisy simulation, which needs no IBM account and uses no QPU time:

```bash
python code/Test_run_hardware_3.py --test
```

To choose qubits and predict results on the real device without submitting anything, after saving your IBM Quantum credentials with `QiskitRuntimeService.save_account()`:

```bash
python code/Test_run_hardware_3.py --dry-run
```

Running without either option submits to real hardware, using QPU time from your account. The script shows what it will submit and asks first.

To run the tests, which take a few minutes on a laptop:

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

## Next steps

- Run Code 3 on hardware. It covers the earlier next steps: repeat runs with a drift check, qubit 0 compared with selected qubits in the same job, and pair selection based on measured readout rather than calibration alone.
