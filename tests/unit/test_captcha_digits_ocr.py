from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

pytest.importorskip("cv2")


def _load_solver_module():
    module_path = Path(__file__).resolve().parents[2] / "scripts" / "captcha_digits_ocr.py"
    spec = importlib.util.spec_from_file_location("captcha_digits_ocr", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_solver_matches_known_debug_image():
    solver = _load_solver_module()
    image = solver._load_image_bgr(
        Path("storage/captcha_debug_batch/image/raw.png")
    )
    result = solver.recognize_digits(
        image,
        expected_length=5,
        rapid_engine=solver._init_rapidocr(),
        use_tesseract=False,
    )

    assert result.text == "49759"
    assert result.variant == "raw"
    assert result.backend == "rapidocr"


def test_solver_matches_known_debug_image_copy():
    solver = _load_solver_module()
    image = solver._load_image_bgr(
        Path("storage/captcha_debug_batch/image copy/raw.png")
    )
    result = solver.recognize_digits(
        image,
        expected_length=5,
        rapid_engine=solver._init_rapidocr(),
        use_tesseract=False,
    )

    assert result.text == "33255"
    assert result.backend == "rapidocr"


def test_solver_matches_known_debug_image_copy_2():
    solver = _load_solver_module()
    image = solver._load_image_bgr(
        Path("storage/captcha_debug_batch/image copy 2/raw.png")
    )
    result = solver.recognize_digits(
        image,
        expected_length=5,
        rapid_engine=solver._init_rapidocr(),
        use_tesseract=False,
    )

    assert result.text == "11085"
    assert result.backend == "rapidocr"
