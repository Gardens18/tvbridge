"""Apple Vision OCR helpers used by :class:`tvbridge.gui.driver.MacDriver`.

All coordinates returned here are **pixels, top-left origin**, relative to the (optionally
cropped) image. Vision reports normalized boxes with a bottom-left origin; the conversion
happens in :func:`recognize_with_origin`.

pyobjc (Quartz, Vision, Foundation) is imported lazily inside the functions.
"""

import logging
import math
from typing import Any, List, Optional, Tuple

log = logging.getLogger("tvbridge.gui.ocr")

#: (text, confidence, (x_px, y_px, w_px, h_px))
OcrResult = Tuple[str, float, Tuple[float, float, float, float]]


def crop_rect(region_px: Tuple[float, float, float, float], img_w: int, img_h: int) -> Tuple[int, int, int, int]:
    """Integral crop rectangle (x0, y0, w, h) for ``region_px``, clamped to the image.

    The rectangle is expanded outward to whole pixels. ``w``/``h`` may be 0 when the region
    lies entirely outside the image.
    """
    x, y, w, h = (float(v) for v in region_px)
    if w < 0:
        x, w = x + w, -w
    if h < 0:
        y, h = y + h, -h
    x0 = max(0, min(int(img_w), int(math.floor(x))))
    y0 = max(0, min(int(img_h), int(math.floor(y))))
    x1 = max(x0, min(int(img_w), int(math.ceil(x + w))))
    y1 = max(y0, min(int(img_h), int(math.ceil(y + h))))
    return x0, y0, x1 - x0, y1 - y0


def load_cgimage(png_path: str) -> Any:
    """Decode an image file into a CGImage. Raises ``RuntimeError`` if it cannot be read."""
    import Quartz
    from Foundation import NSURL

    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(png_path)), None)
    if src is None or Quartz.CGImageSourceGetCount(src) < 1:
        raise RuntimeError("cannot read image %s" % png_path)
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        raise RuntimeError("cannot decode image %s" % png_path)
    return img


def _upscaled(img: Any, factor: float) -> Any:
    """Return ``img`` redrawn ``factor`` times larger (high-quality interpolation)."""
    import Quartz

    w = int(Quartz.CGImageGetWidth(img))
    h = int(Quartz.CGImageGetHeight(img))
    nw, nh = max(1, int(round(w * factor))), max(1, int(round(h * factor)))
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, nw, nh, 8, 0, cs, Quartz.kCGImageAlphaPremultipliedLast)
    if ctx is None:
        return img
    # Opaque white underneath, so transparent pixels do not read as black.
    Quartz.CGContextSetRGBFillColor(ctx, 1.0, 1.0, 1.0, 1.0)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, nw, nh))
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, nw, nh), img)
    out = Quartz.CGBitmapContextCreateImage(ctx)
    return out if out is not None else img


def recognize_with_origin(png_path: str, region_px: Optional[Tuple[float, float, float, float]] = None,
                          upscale: float = 1.0) -> Tuple[List[OcrResult], Tuple[int, int]]:
    """Like :func:`recognize` but also returns the crop origin ``(x0, y0)`` in image pixels.

    Results are relative to the cropped image; add the origin to get full-image pixels.
    ``upscale`` > 1 redraws the (cropped) image larger before OCR, which helps Vision with
    small 1x text; returned coordinates are still in the original pixel space.
    """
    import objc
    import Quartz
    import Vision

    with objc.autorelease_pool():
        img = load_cgimage(png_path)
        width = int(Quartz.CGImageGetWidth(img))
        height = int(Quartz.CGImageGetHeight(img))
        x0 = y0 = 0
        if region_px is not None:
            x0, y0, cw, ch = crop_rect(region_px, width, height)
            if cw <= 0 or ch <= 0:
                log.debug("OCR region %r lies outside the %dx%d image", region_px, width, height)
                return [], (x0, y0)
            cropped = Quartz.CGImageCreateWithImageInRect(img, Quartz.CGRectMake(x0, y0, cw, ch))
            if cropped is None:
                raise RuntimeError("cannot crop %s to %r" % (png_path, (x0, y0, cw, ch)))
            img = cropped
            width = int(Quartz.CGImageGetWidth(img))
            height = int(Quartz.CGImageGetHeight(img))

        if max(width, height) > TILE_PX:
            # Vision misreads small UI text in big (e.g. full 4K window) images: read tiles.
            return _recognize_tiled(img, width, height, upscale), (x0, y0)
        return _recognize_cg(img, width, height, upscale, png_path), (x0, y0)


TILE_PX = 1600      # tile edge in pixels
TILE_STRIDE = 800   # half-tile overlap: any line up to 800 px is whole in some tile
EDGE_PX = 3         # observations touching an inner tile edge are cut text: dropped


def _tile_starts(total: int) -> List[int]:
    if total <= TILE_PX:
        return [0]
    starts = list(range(0, total - TILE_PX, TILE_STRIDE))
    starts.append(total - TILE_PX)
    return sorted(set(starts))


def _recognize_tiled(img: Any, width: int, height: int, upscale: float) -> List[OcrResult]:
    import Quartz

    out = []  # type: List[OcrResult]
    for ty in _tile_starts(height):
        for tx in _tile_starts(width):
            tw, th = min(TILE_PX, width - tx), min(TILE_PX, height - ty)
            tile = Quartz.CGImageCreateWithImageInRect(img, Quartz.CGRectMake(tx, ty, tw, th))
            if tile is None:
                continue
            for text, conf, (bx, by, bw, bh) in _recognize_cg(tile, tw, th, upscale, "tile"):
                if ((tx > 0 and bx <= EDGE_PX) or (ty > 0 and by <= EDGE_PX)
                        or (tx + tw < width and bx + bw >= tw - EDGE_PX)
                        or (ty + th < height and by + bh >= th - EDGE_PX)):
                    continue
                out.append((text, conf, (bx + tx, by + ty, bw, bh)))
    # Overlapping tiles see the same text twice: keep one per (text, ~position).
    seen, unique = set(), []
    for r in sorted(out, key=lambda r: -r[1]):
        key = (r[0], round(r[2][0] / 6), round(r[2][1] / 6))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    unique.sort(key=lambda r: (r[2][1], r[2][0]))
    return unique


def _recognize_cg(img: Any, width: int, height: int, upscale: float, label: str) -> List[OcrResult]:
    import Vision

    if True:
        target = _upscaled(img, upscale) if upscale and upscale > 1.0 else img

        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(False)
        try:
            request.setRecognitionLanguages_(["en-US"])
        except Exception:  # pragma: no cover - older Vision revisions
            pass
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(target, {})
        ok, err = handler.performRequests_error_([request], None)
        if not ok:
            raise RuntimeError("Vision text recognition failed for %s: %s" % (label, err))

        results = []  # type: List[OcrResult]
        for obs in request.results() or []:
            candidates = obs.topCandidates_(1)
            if not candidates:
                continue
            cand = candidates[0]
            text = str(cand.string())
            if not text.strip():
                continue
            bb = obs.boundingBox()   # normalized, bottom-left origin
            bx, by = float(bb.origin.x), float(bb.origin.y)
            bw, bh = float(bb.size.width), float(bb.size.height)
            results.append((
                text,
                float(cand.confidence()),
                (bx * width, (1.0 - by - bh) * height, bw * width, bh * height),
            ))
        results.sort(key=lambda r: (r[2][1], r[2][0]))
        return results


def recognize(png_path: str, region_px: Optional[Tuple[float, float, float, float]] = None) -> List[OcrResult]:
    """Vision OCR (accurate level, language correction off) of an image file.

    ``region_px`` = (x, y, w, h) in top-left pixel coordinates crops the image first
    (``CGImageCreateWithImageInRect``). Returns ``(text, confidence, (x_px, y_px, w_px, h_px))``
    in top-left pixel coordinates of the (cropped) image. Overlapping observations (Vision
    sometimes returns a whole line *and* its parts) are all kept.
    """
    results, _origin = recognize_with_origin(png_path, region_px)
    return results
