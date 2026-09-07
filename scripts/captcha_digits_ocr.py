"""Extract digits from noisy captcha-like images with plus signs and wave lines.

Usage:
  python scripts/captcha_digits_ocr.py --image storage/fssp_captcha_debug.png
  python scripts/captcha_digits_ocr.py --image storage/fssp_captcha_debug.png --json
  python scripts/captcha_digits_ocr.py --images-dir storage --json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytesseract

_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@dataclass
class OcrCandidate:
    text: str
    score: float
    variant: str
    backend: str
    raw_text: str
    confidence: float


def main() -> int:
    parser = argparse.ArgumentParser(description="Recognize digits from noisy captcha image")
    parser.add_argument("--image", default="", help="path to captcha image")
    parser.add_argument("--images-dir", default="", help="folder with captcha images")
    parser.add_argument(
        "--expected-length",
        type=int,
        default=5,
        help="prefer OCR result with this digit length (0 = disabled)",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "rapidocr", "tesseract"),
        default="auto",
        help="OCR backend selection",
    )
    parser.add_argument(
        "--tesseract-cmd",
        default="",
        help="optional explicit path to tesseract executable",
    )
    parser.add_argument(
        "--debug-dir",
        default="",
        help="optional folder to save intermediate masks",
    )
    parser.add_argument("--json", action="store_true", help="print JSON output")
    args = parser.parse_args()

    if not args.image and not args.images_dir:
        print("set --image or --images-dir")
        return 2

    tesseract_cmd = _resolve_tesseract_cmd(args.tesseract_cmd)
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

    rapid_engine = _init_rapidocr() if args.backend in {"auto", "rapidocr"} else None
    use_tesseract = args.backend == "tesseract" or (
        args.backend == "auto" and _is_tesseract_available()
    )

    if args.image:
        image_path = Path(args.image)
        image_bgr = _load_image_bgr(image_path)
        debug_dir = Path(args.debug_dir) if args.debug_dir else None
        best = recognize_digits(
            image_bgr,
            expected_length=args.expected_length,
            debug_dir=debug_dir,
            rapid_engine=rapid_engine,
            use_tesseract=use_tesseract,
        )
        if args.json:
            print(json.dumps(asdict(best), ensure_ascii=False, indent=2))
        else:
            print(f"{image_path}: {best.text} [{best.backend}/{best.variant}] score={best.score:.2f}")
        return 0

    images_dir = Path(args.images_dir)
    rows: list[dict[str, Any]] = []
    for image_path in sorted(p for p in images_dir.iterdir() if p.suffix.lower() in _IMG_EXTS):
        debug_dir = Path(args.debug_dir) / image_path.stem if args.debug_dir else None
        image_bgr = _load_image_bgr(image_path)
        best = recognize_digits(
            image_bgr,
            expected_length=args.expected_length,
            debug_dir=debug_dir,
            rapid_engine=rapid_engine,
            use_tesseract=use_tesseract,
        )
        row = {"image": str(image_path), **asdict(best)}
        rows.append(row)
        if not args.json:
            print(f"{image_path.name}: {best.text} [{best.backend}/{best.variant}] score={best.score:.2f}")

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def recognize_digits(
    image_bgr: np.ndarray,
    expected_length: int = 5,
    debug_dir: Path | None = None,
    rapid_engine: Any | None = None,
    use_tesseract: bool = True,
) -> OcrCandidate:
    mask = _foreground_mask(image_bgr)
    variants = _build_variants(mask)

    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(debug_dir / "raw.png"), image_bgr)
        cv2.imwrite(str(debug_dir / "mask_foreground.png"), mask)

    candidates: list[OcrCandidate] = []

    if rapid_engine is not None:
        candidates.append(
            _ocr_candidate_rapid(
                image_bgr,
                variant="raw",
                expected_length=expected_length,
                engine=rapid_engine,
            )
        )

    for name, candidate_mask in variants.items():
        cleaned = _filter_components(candidate_mask)
        cropped = _crop_to_content(cleaned)
        if cropped.size == 0:
            continue

        if debug_dir:
            cv2.imwrite(str(debug_dir / f"{name}_mask.png"), cropped)

        if rapid_engine is not None:
            rapid_img = cv2.cvtColor(cropped, cv2.COLOR_GRAY2BGR)
            candidates.append(
                _ocr_candidate_rapid(
                    rapid_img,
                    variant=name,
                    expected_length=expected_length,
                    engine=rapid_engine,
                )
            )

        if use_tesseract:
            candidates.append(
                _ocr_candidate_tesseract(
                    cropped,
                    variant=name,
                    expected_length=expected_length,
                )
            )

    if not candidates:
        return OcrCandidate(
            text="",
            score=-1_000_000_000.0,
            variant="none",
            backend="none",
            raw_text="",
            confidence=0.0,
        )

    best = max(candidates, key=lambda x: x.score)
    if debug_dir:
        payload = {
            "best": asdict(best),
            "candidates": [asdict(c) for c in sorted(candidates, key=lambda x: x.score, reverse=True)],
        }
        (debug_dir / "result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return best


def _load_image_bgr(path: Path) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise FileNotFoundError(path)
    if image.ndim == 2:
        return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    if image.shape[2] == 4:
        alpha = image[:, :, 3:4].astype(np.float32) / 255.0
        bgr = image[:, :, :3].astype(np.float32)
        white = np.full_like(bgr, 255.0)
        image = (bgr * alpha + white * (1.0 - alpha)).astype(np.uint8)
    return image


def _foreground_mask(image_bgr: np.ndarray) -> np.ndarray:
    h, w = image_bgr.shape[:2]
    border = np.vstack(
        [
            image_bgr[0, :, :],
            image_bgr[h - 1, :, :],
            image_bgr[:, 0, :],
            image_bgr[:, w - 1, :],
        ]
    ).astype(np.float32)
    bg = np.median(border, axis=0)

    diff = image_bgr.astype(np.float32) - bg
    dist = np.sqrt(np.sum(diff * diff, axis=2))
    dist_norm = cv2.normalize(dist, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    _, dist_mask = cv2.threshold(dist_norm, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    bg_gray = int(np.median(np.concatenate([gray[0, :], gray[h - 1, :], gray[:, 0], gray[:, w - 1]])))
    gray_delta = cv2.absdiff(gray, np.full_like(gray, bg_gray))
    _, gray_mask = cv2.threshold(gray_delta, 22, 255, cv2.THRESH_BINARY)

    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    sat_mask = cv2.inRange(hsv, (0, 18, 0), (179, 255, 255))

    mask = cv2.bitwise_or(dist_mask, gray_mask)
    mask = cv2.bitwise_and(mask, cv2.bitwise_or(sat_mask, gray_mask))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    return mask


def _build_variants(mask: np.ndarray) -> dict[str, np.ndarray]:
    variants: dict[str, np.ndarray] = {}

    base = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    variants["base"] = base

    line_like = cv2.morphologyEx(base, cv2.MORPH_OPEN, np.ones((31, 1), np.uint8), iterations=1)
    no_line = cv2.subtract(base, line_like)
    no_line = cv2.morphologyEx(no_line, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    variants["no_line"] = no_line

    dt = cv2.distanceTransform(base, cv2.DIST_L2, 5)
    core = (dt >= 1.5).astype(np.uint8) * 255
    core = cv2.dilate(core, np.ones((3, 3), np.uint8), iterations=1)
    variants["core"] = core

    return variants


def _filter_components(mask: np.ndarray) -> np.ndarray:
    h, w = mask.shape[:2]
    n_labels, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    out = np.zeros_like(mask)
    min_area = max(8, int(0.00012 * h * w))

    for idx in range(1, n_labels):
        cw = int(stats[idx, cv2.CC_STAT_WIDTH])
        ch = int(stats[idx, cv2.CC_STAT_HEIGHT])
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        if cw > int(0.35 * w) and ch < int(0.22 * h) and cw / max(ch, 1) > 5.0:
            continue
        if ch < int(0.08 * h) and cw < int(0.08 * w):
            continue
        out[labels == idx] = 255

    out = cv2.morphologyEx(out, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
    out = cv2.dilate(out, np.ones((2, 2), np.uint8), iterations=1)
    return out


def _crop_to_content(mask: np.ndarray) -> np.ndarray:
    ys, xs = np.where(mask > 0)
    if len(xs) == 0 or len(ys) == 0:
        return mask

    x0 = max(0, int(xs.min()) - 4)
    x1 = min(mask.shape[1], int(xs.max()) + 5)
    y0 = max(0, int(ys.min()) - 4)
    y1 = min(mask.shape[0], int(ys.max()) + 5)
    return mask[y0:y1, x0:x1]


def _ocr_candidate_rapid(
    image: np.ndarray,
    variant: str,
    expected_length: int,
    engine: Any,
) -> OcrCandidate:
    text_raw = ""
    conf = 0.0
    try:
        res, _ = engine(image)
        if res:
            text_raw = str(res[0][1] or "")
            try:
                conf = float(res[0][2])
            except (TypeError, ValueError):
                conf = 0.0
    except Exception:
        pass

    digits = "".join(ch for ch in text_raw if ch.isdigit())
    non_digits = len([ch for ch in text_raw if not ch.isdigit() and not ch.isspace()])
    score = conf * 100 + len(digits) * 12 - non_digits * 8
    if variant == "raw":
        score += 10
    if expected_length > 0:
        score -= abs(expected_length - len(digits)) * 20
        if len(digits) == expected_length:
            score += 35
    if not digits:
        score -= 60

    return OcrCandidate(
        text=digits,
        score=score,
        variant=variant,
        backend="rapidocr",
        raw_text=text_raw,
        confidence=conf,
    )


def _ocr_candidate_tesseract(mask: np.ndarray, variant: str, expected_length: int) -> OcrCandidate:
    img = 255 - mask
    img = cv2.resize(img, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    config = "--oem 3 --psm 7 -c tessedit_char_whitelist=0123456789"
    try:
        data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
        text_raw = pytesseract.image_to_string(img, config=config)
    except Exception:
        data = {"conf": []}
        text_raw = ""

    text = "".join(ch for ch in text_raw if ch.isdigit())
    conf_values: list[float] = []
    for raw in data.get("conf", []):
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if val >= 0:
            conf_values.append(val)

    conf_avg = float(sum(conf_values) / len(conf_values)) if conf_values else 0.0
    score = conf_avg + len(text) * 8
    if expected_length > 0:
        score -= abs(expected_length - len(text)) * 15
        if len(text) == expected_length:
            score += 25

    return OcrCandidate(
        text=text,
        score=score,
        variant=variant,
        backend="tesseract",
        raw_text=text_raw,
        confidence=conf_avg / 100.0,
    )


def _init_rapidocr() -> Any | None:
    try:
        from rapidocr_onnxruntime import RapidOCR

        return RapidOCR(
            use_text_det=False,
            use_angle_cls=False,
            text_score=0.0,
            width_height_ratio=-1,
        )
    except Exception:
        return None


def _resolve_tesseract_cmd(explicit: str) -> str:
    if explicit:
        return explicit

    common = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    for p in common:
        if Path(p).exists():
            return p
    return ""


def _is_tesseract_available() -> bool:
    try:
        _ = pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
