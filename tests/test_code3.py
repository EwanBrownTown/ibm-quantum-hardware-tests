"""Checks for Test_run_hardware_3.py. Run with: python -m pytest tests

These use local simulators only and never touch IBM hardware.
"""
import importlib.util
from pathlib import Path

import numpy as np
import pytest
from qiskit_aer import AerSimulator
from qiskit_aer.noise import NoiseModel, ReadoutError
from qiskit_ibm_runtime.fake_provider import FakeMarrakesh

CODE = Path(__file__).resolve().parents[1] / "code" / "Test_run_hardware_3.py"
spec = importlib.util.spec_from_file_location("code3", CODE)
code3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(code3)

# Like the 20 October fault: |0> read as 1 in 40% of shots, |1> read correctly 98% of the time
OCTOBER_FAULT = ReadoutError([[0.60, 0.40], [0.02, 0.98]])


@pytest.fixture(scope="module")
def backend():
    return FakeMarrakesh()


def test_readout_correction_recovers_a_known_distribution():
    matrix = np.array([[0.60, 0.02], [0.40, 0.98]])   # column j = P(read i | prepared j)
    true = np.array([0.5, 0.5])
    assert np.allclose(code3.mitigate(matrix, matrix @ true * 1e6), true)


def test_reference_layout_is_included_but_separate(backend):
    for name in ("A", "B"):
        layouts = code3.candidate_layouts(backend.target, name)
        assert sum(l["reference"] for l in layouts) == 1
        assert len([l for l in layouts if not l["reference"]]) == code3.CANDIDATES
        assert sorted(next(l for l in layouts if l["reference"])["qubits"]) == code3.REFERENCE[name]


def test_faulty_qubit_is_detected_avoided_and_corrected(backend, tmp_path):
    best = code3.candidate_layouts(backend.target, "A")[0]["qubits"][0]
    noise = NoiseModel.from_backend(backend)
    for q in (best, 0):
        noise.add_readout_error(OCTOBER_FAULT, [q])
    faulty_device = AerSimulator.from_backend(backend, noise_model=noise)

    rows = code3.run(backend, faulty_device, tmp_path, names=("A",), local=True)["A"]
    by_qubit = {r["qubits"][0]: r for r in rows}

    # The fault shows up in the calibration circuits, so the faulty qubit is not chosen
    assert by_qubit[best]["control_error"] > 0.1
    assert not by_qubit[best]["main_result"]
    assert by_qubit[0]["reference"] and not by_qubit[0]["main_result"]

    # Raw results on the faulty qubits look like October, and disagree with the calibration model
    for q in (best, 0):
        check = by_qubit[q]["checks"]["P(1)"]
        assert check["raw"] > 0.6 and not by_qubit[q]["expected_raw"]
        # Readout correction brings them back to 50/50 within the expected error
        assert abs(check["z_corrected_vs_ideal"]) <= code3.Z_LIMIT

    main = next(r for r in rows if r["main_result"])
    assert main["expected_raw"] and main["expected_corrected"]


def test_bell_state_end_to_end(backend, tmp_path):
    rows = code3.run(backend, backend, tmp_path, names=("B",), local=True)["B"]
    main = next(r for r in rows if r["main_result"])
    assert main["expected_raw"] and main["expected_corrected"]
    assert main["checks"]["P(00 or 11)"]["raw"] > 0.9
    assert list(tmp_path.glob("hw3_B_*_shots.json")) and list(tmp_path.glob("hw3_B_*_meta.json"))
