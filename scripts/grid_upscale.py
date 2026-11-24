#!/usr/bin/env python3
"""Mechanical helpers only; AI editing and visual review are agent responsibilities."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import shutil

import numpy as np
from PIL import Image, ImageCms, ImageFilter, ImageOps


def rounded(value):
    return int(math.floor(value + 0.5))


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path):
    return Path(path).read_text(encoding="utf-8")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(2**20), b""):
            h.update(block)
    return h.hexdigest()


def valid_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("IDs must contain only letters, digits, _ or -")
    return value


def box_check(box, size):
    if len(box) != 4 or not all(math.isfinite(v) for v in box):
        raise ValueError(f"Invalid box: {box}")
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= size[0] and 0 <= y0 < y1 <= size[1]):
        raise ValueError(f"Box outside image: {box}")
    return box


def mapped(box, original, target):
    result = [rounded(v * target[i % 2] / original[i % 2]) for i, v in enumerate(box)]
    box_check(result, target)
    return result


def prepare(args):
    if not math.isfinite(args.scale) or args.scale <= 1:
        raise ValueError("--scale must be finite and greater than 1")
    if min(args.rows, args.cols) < 1:
        raise ValueError("Grid dimensions must be positive")
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise FileExistsError("Use an empty work directory; existing runs are preserved")
    with Image.open(args.source) as raw:
        im = ImageOps.exif_transpose(raw)
        has_alpha = "A" in im.getbands() or "transparency" in im.info
        original_icc = im.info.get("icc_profile")
        alpha = im.convert("RGBA").getchannel("A") if has_alpha else None
        rgb = im.convert("RGB")
        if original_icc:
            import io
            rgb = ImageCms.profileToProfile(rgb, ImageCms.ImageCmsProfile(io.BytesIO(original_icc)),
                                           ImageCms.createProfile("sRGB"), outputMode="RGB")
    w, h = rgb.size
    if w < args.cols or h < args.rows:
        raise ValueError("Image dimensions must be at least the grid dimensions")
    context = args.context if args.context is not None else max(1, rounded(min(w / args.cols, h / args.rows) * .1875))
    if context < 1:
        raise ValueError("Context must be at least one source pixel")
    rgb.save(work / "source.png")
    if alpha is not None:
        alpha.save(work / "alpha.png")
    xs = [i * w // args.cols for i in range(args.cols + 1)]
    ys = [i * h // args.rows for i in range(args.rows + 1)]
    jobs = []
    for r in range(args.rows):
        for c in range(args.cols):
            name = f"r{r+1}_c{c+1}"
            core = [xs[c], ys[r], xs[c+1], ys[r+1]]
            crop = [max(0, core[0]-context), max(0, core[1]-context),
                    min(w, core[2]+context), min(h, core[3]+context)]
            rgb.crop(crop).save(work / f"{name}_source.png")
            jobs.append({"id": name, "core": core, "crop": crop,
                         "source": f"{name}_source.png", "ai": f"{name}_ai.png"})
    manifest = {"version": 1, "source": str(args.source.resolve()), "source_sha256": digest(args.source),
                "size": [w, h], "scale_each_axis": args.scale,
                "target_size": [rounded(w*args.scale), rounded(h*args.scale)],
                "rows": args.rows, "cols": args.cols, "context": context,
                "alpha": "alpha.png" if has_alpha else None, "jobs": jobs}
    write_json(work / "manifest.json", manifest)
    print(json.dumps({"manifest": str(work / "manifest.json"), "tiles": len(jobs),
                      "source_size": [w, h], "target_size": manifest["target_size"]}))


def axis_weights(start, end, core_start, core_end, length, original_length):
    p = start + (np.arange(length, dtype=np.float32) + .5) * (end-start) / length
    weights = np.ones(length, dtype=np.float32)
    if start > 0 and core_start > start:
        t = np.clip((p-start)/(core_start-start), 0, 1)
        weights *= .5-.5*np.cos(np.pi*t)
    if end < original_length and end > core_end:
        t = np.clip((end-p)/(end-core_end), 0, 1)
        weights *= .5-.5*np.cos(np.pi*t)
    return weights


def apply_repairs(stage, path, original_size):
    if path is None:
        return []
    path = path.resolve()
    records = []
    for spec in json.loads(read_json(path)):
        name = valid_id(spec["id"])
        box_check(spec["box"], original_size)
        box = mapped(spec["box"], original_size, stage.size)
        protected = [mapped(box_check(b, original_size), original_size, stage.size)
                     for b in spec.get("protected_boxes", [])]
        before = [hashlib.sha256(stage.crop(b).tobytes()).hexdigest() for b in protected]
        image_path, mask_path = path.parent / spec["image"], path.parent / spec["mask"]
        with Image.open(image_path) as im, Image.open(mask_path) as mask:
            if im.size != mask.size:
                raise ValueError(f"Repair and mask dimensions differ: {name}")
            if abs((im.width/im.height) / ((box[2]-box[0])/(box[3]-box[1]))-1) > .02:
                raise ValueError(f"Repair aspect ratio mismatch: {name}")
            size = (box[2]-box[0], box[3]-box[1])
            patch = im.convert("RGB").resize(size, Image.Resampling.LANCZOS)
            matte = mask.convert("L").resize(size, Image.Resampling.LANCZOS)
            stage.paste(patch, box[:2], matte)
        after = [hashlib.sha256(stage.crop(b).tobytes()).hexdigest() for b in protected]
        if before != after:
            raise ValueError(f"Repair changed a protected region: {name}")
        records.append({**spec, "image_sha256": digest(image_path), "mask_sha256": digest(mask_path),
                        "protected_before": before, "protected_after": after})
    return records


def assemble(args):
    work = args.work.resolve()
    m = json.loads(read_json(work / "manifest.json"))
    original, target = m["size"], m["target_size"]
    if args.working_max_edge < 16 or not 0 <= args.tone_strength <= 1:
        raise ValueError("Working edge must be >=16 and tone strength in [0,1]")
    factor = min(4, m["scale_each_axis"], args.working_max_edge / max(original))
    size = [rounded(v*factor) for v in original]
    for j in m["jobs"]:
        mapped(j["core"], original, size)
        ai_path = work / j["ai"]
        if not ai_path.is_file():
            raise FileNotFoundError(f"Missing AI result: {ai_path}")
        with Image.open(ai_path) as im:
            crop = j["crop"]
            if abs((im.width/im.height)/((crop[2]-crop[0])/(crop[3]-crop[1]))-1) > .02:
                raise ValueError(f"AI aspect ratio changed >2%: {j['id']}")
    base = args.output.resolve()
    base.parent.mkdir(parents=True, exist_ok=True)
    png, jpg = Path(str(base)+".png"), Path(str(base)+".jpg")
    preview_path, report_path = Path(str(base)+"_preview.jpg"), Path(str(base)+"_report.json")
    if any(p.exists() for p in [png, jpg, preview_path, report_path]):
        raise FileExistsError("Output prefix already exists; select a new version")
    print(json.dumps({"working_size": size, "target_size": target,
                      "target_pixels": target[0]*target[1],
                      "base_canvas_bytes": target[0]*target[1]*(4 if m["alpha"] else 3),
                      "free_work_disk_bytes": shutil.disk_usage(work).free}), flush=True)
    accumulator_path, weights_path = work / "accumulator.bin", work / "weights.bin"
    acc = np.memmap(accumulator_path, dtype="float32", mode="w+", shape=(size[1], size[0], 3))
    weights = np.memmap(weights_path, dtype="float32", mode="w+", shape=(size[1], size[0]))
    acc[:] = 0
    weights[:] = 0
    records = []
    for j in m["jobs"]:
        crop, core = j["crop"], j["core"]
        b = mapped(crop, original, size)
        pw, ph = b[2]-b[0], b[3]-b[1]
        with Image.open(work / j["ai"]) as raw:
            ai = raw.convert("RGB")
        native = ai.size
        with Image.open(work / j["source"]) as src:
            reference = src.convert("RGB").resize(native, Image.Resampling.LANCZOS)
        # Match broad lighting only; do not confuse this with registration or anatomy repair.
        radius = max(1, min(native)*.025)
        ar = np.asarray(ai, dtype=np.float32)
        low = np.asarray(ai.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
        ref = np.asarray(reference.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
        matched = Image.fromarray(np.clip(ar+args.tone_strength*(ref-low), 0, 255).astype("uint8"))
        patch = matched.resize((pw, ph), Image.Resampling.LANCZOS)
        vx = axis_weights(crop[0], crop[2], core[0], core[2], pw, original[0])
        vy = axis_weights(crop[1], crop[3], core[1], core[3], ph, original[1])
        for y in range(0, ph, 128):
            end = min(ph, y+128)
            a = np.asarray(patch.crop((0, y, pw, end)), dtype=np.float32)
            mask = vy[y:end, None]*vx[None, :]
            acc[b[1]+y:b[1]+end, b[0]:b[2]] += a*mask[:, :, None]
            weights[b[1]+y:b[1]+end, b[0]:b[2]] += mask
        records.append({"id": j["id"], "ai_native_size": list(native), "ai_sha256": digest(work / j["ai"])})
        del ar, low, ref, matched, patch, ai, reference
        print(f"Blended {j['id']}", flush=True)
    stage = Image.new("RGB", size)
    for y in range(0, size[1], 128):
        wt = weights[y:y+128]
        if np.any(wt <= 0):
            raise ValueError("Uncovered pixels in blend")
        data = np.clip(acc[y:y+128]/wt[:, :, None], 0, 255).astype("uint8")
        stage.paste(Image.fromarray(data), (0, y))
    del acc, weights, data, wt
    accumulator_path.unlink()
    weights_path.unlink()
    repairs = apply_repairs(stage, args.repairs, original)
    if m["alpha"]:
        with Image.open(work / m["alpha"]) as alpha:
            stage.putalpha(alpha.resize(size, Image.Resampling.LANCZOS))
    stage.save(work / "stitched_working.png")
    # One shared sampling grid: exact target cores, no gaps and no independent tile phase.
    Image.MAX_IMAGE_PIXELS = None  # Only for dimensions computed from this local manifest.
    out = Image.new(stage.mode, target)
    tile_dir = work / "final_tiles"
    tile_dir.mkdir(exist_ok=True)
    for j in m["jobs"]:
        b = mapped(j["core"], original, target)
        sampling = (b[0]*size[0]/target[0], b[1]*size[1]/target[1],
                    b[2]*size[0]/target[0], b[3]*size[1]/target[1])
        tile = stage.resize((b[2]-b[0], b[3]-b[1]), Image.Resampling.LANCZOS, box=sampling)
        tile.save(tile_dir / f"{j['id']}.png")
        out.paste(tile, b[:2])
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    out.save(png, compress_level=6, icc_profile=icc)
    files = [png]
    if not m["alpha"]:
        out.save(jpg, quality=95, subsampling=0, icc_profile=icc)
        files.append(jpg)
    preview = out.copy()
    preview.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
    if preview.mode == "RGBA":
        background = Image.new("RGB", preview.size, "white")
        background.paste(preview, mask=preview.getchannel("A"))
        preview = background
    preview.save(preview_path, quality=95, icc_profile=icc)
    out.close()
    stage.close()
    qa_specs = json.loads(read_json(args.qa)) if args.qa else []
    half = min(384, max(1, min(target)//4))
    def add_seam(name, x, y):
        qa_specs.append({"id": name, "target_box": [max(0,x-half), max(0,y-half),
                                                  min(target[0],x+half), min(target[1],y+half)]})
    for c in range(1, m["cols"]):
        x = rounded((c*original[0]//m["cols"])*target[0]/original[0])
        for r in range(m["rows"]):
            add_seam(f"seam_v{c}_r{r+1}", x, rounded((r+.5)*target[1]/m["rows"]))
    for r in range(1, m["rows"]):
        y = rounded((r*original[1]//m["rows"])*target[1]/original[1])
        for c in range(m["cols"]):
            add_seam(f"seam_h{r}_c{c+1}", rounded((c+.5)*target[0]/m["cols"]), y)
    qa_dir = work / "qa"
    qa_dir.mkdir(exist_ok=True)
    qa_records = []
    with Image.open(png) as final:
        if list(final.size) != target:
            raise ValueError("Unexpected final dimensions")
        final.load()
        for spec in qa_specs:
            name = valid_id(spec["id"])
            b = (mapped(box_check(spec["box"], original), original, target) if "box" in spec
                 else box_check(spec["target_box"], target))
            path = qa_dir / f"{name}.png"
            final.crop(b).save(path, icc_profile=icc)
            qa_records.append({"id": name, "target_box": b, "file": str(path)})
    file_records = []
    for path in files:
        with Image.open(path) as check:
            if list(check.size) != target:
                raise ValueError("Unexpected output size")
            check.verify()
        file_records.append({"path": str(path), "bytes": path.stat().st_size, "sha256": digest(path)})
    write_json(report_path, {"manifest": m, "working_size": size, "ai_tiles": records,
                            "repairs": repairs, "qa": qa_records, "files": file_records,
                            "method": "AI tiles + broad tone matching + overlap blend + Lanczos target sampling",
                            "visual_review": "Required: agent must inspect final crops; script does not judge anatomy"})
    print(json.dumps({"files": file_records, "report": str(report_path)}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("prepare")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--scale", type=float, required=True)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--rows", type=int, default=4)
    p.add_argument("--cols", type=int, default=4)
    p.add_argument("--context", type=int)
    p.set_defaults(func=prepare)
    p = subs.add_parser("assemble")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--working-max-edge", type=int, default=8192)
    p.add_argument("--tone-strength", type=float, default=.85)
    p.add_argument("--repairs", type=Path)
    p.add_argument("--qa", type=Path)
    p.set_defaults(func=assemble)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
