#!/usr/bin/env python3
"""Cut the subject out of a picture and put it on a new background — exactly.

Third tool of the images/ group, and the odd one out on purpose: generate_image
and edit_image ask a diffusion model, this one runs a SEGMENTATION network in
this process (onnxruntime, CPU) and then does plain compositing with Pillow.
The distinction is the reason it exists. img2img cannot "keep the person and
replace the background": at any strength it repaints the whole picture, so the
background only half changes while the face drifts — measured on SD-Turbo,
where "uniform blue background" came out as a blue metal wall behind a
different man. A salient-object model answers the question "which pixels are
the subject" once, exactly, at full resolution, in a few seconds, with no image
model configured at all; and its alpha doubles as the mask that lets edit_image
inpaint ONLY behind the subject when a painted scene is wanted after all.

Models: the ONNX exports the rembg project publishes (same preprocessing).
ISNet (DIS, Apache-2.0) is the default — best edges on people and objects;
U2-Net takes a 10x smaller input and is faster on a weak CPU. The file is
fetched on first use into $MYAGENT_CACHE/models (178 MB, once) — install.sh
offers to do it ahead of time, like the fastembed model. No torch, no rembg
(which drags scipy, scikit-image, opencv and pymatting for features this tool
does not use).

Outputs, next to the source and never over it: the composite (RGBA when the
background is transparent) and a MASK of the background (white = background,
black = subject), the convention edit_image / A1111 expect for "repaint here".
"""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

TIMEOUT_DOWNLOAD = 240  # under tool.json's 300 so we fail with a sentence
RELEASES = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"

#: name -> how to feed it. size is the network input; the picture is resized
#: to it and the prediction resized back, so the cut-out keeps full resolution.
MODELS = {
    "isnet-general-use": {"file": "isnet-general-use.onnx", "size": (1024, 1024),
                          "mean": (0.5, 0.5, 0.5), "std": (1.0, 1.0, 1.0), "div": 255.0},
    "u2net": {"file": "u2net.onnx", "size": (320, 320),
              "mean": (0.485, 0.456, 0.406), "std": (0.229, 0.224, 0.225), "div": "max"},
    "u2net_human_seg": {"file": "u2net_human_seg.onnx", "size": (320, 320),
                        "mean": (0.485, 0.456, 0.406), "std": (0.229, 0.224, 0.225), "div": "max"},
}
DEFAULT_MODEL = "isnet-general-use"
# Words that say "a colour" without naming one; what is left is joined and
# tried as a Pillow colour name ("light blue" -> "lightblue").
COLOUR_FILLER = {"a", "an", "the", "solid", "uniform", "plain", "flat", "simple", "pure",
                 "background", "backdrop", "colour", "color", "coloured", "colored"}


def fail(msg):
    print(f"ERROR: {msg}")
    sys.exit(1)


def slug(text, fallback="image"):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (s[:40].rstrip("-") or fallback)


def free_name(base, ext=".png"):
    """`base.png`, or the first `base-N.png` that does not exist yet."""
    name = f"{base}{ext}"
    n = 2
    while os.path.exists(name):
        name = f"{base}-{n}{ext}"
        n += 1
    return name


def model_name():
    name = (os.environ.get("MYAGENT_BGREMOVE_MODEL") or DEFAULT_MODEL).strip()
    if name not in MODELS:
        fail(f"unknown background-removal model '{name}' (MYAGENT_BGREMOVE_MODEL); "
             f"one of: {', '.join(MODELS)}")
    return name


def models_dir():
    base = os.environ.get("MYAGENT_CACHE") or os.path.join(
        os.environ.get("MYAGENT_HOME") or os.path.expanduser("~/myagent"), "cache")
    return os.path.join(base, "models")


def model_path(name=None):
    return os.path.join(models_dir(), MODELS[name or model_name()]["file"])


def ensure_model(name):
    """Path of the ONNX file, downloading it once. Returns (path, downloaded)."""
    path = model_path(name)
    if os.path.isfile(path) and os.path.getsize(path) > 1_000_000:
        return path, False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    url = RELEASES + MODELS[name]["file"]
    part = path + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "myagent-remove-background"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as out:
            started = time.time()
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                if time.time() - started > TIMEOUT_DOWNLOAD:
                    raise TimeoutError("download too slow")
        os.replace(part, path)
    except Exception as e:
        try:
            os.remove(part)
        except OSError:
            pass
        fail(f"the segmentation model '{name}' is not on this machine and could not "
             f"be downloaded from {url}: {e}. Fetch it by hand into {path} "
             "(178 MB) or run install.sh, which offers the download.")
    return path, True


def load_image(param, what):
    """Pillow image of a workspace file, or a sentence saying why not.

    cwd IS the workspace (the tool contract), so a bare name resolves there; a
    leading '~' is expanded because small models write the workspace that way.
    """
    from PIL import Image, UnidentifiedImageError
    p = Path(str(param)).expanduser()
    if not p.is_file():
        fail(f"{what} not found: {param}. Use the exact path printed by list_dir "
             "or by the tool that produced it, including any folder such as "
             "'_attachments/'; the file must already exist in the workspace.")
    try:
        img = Image.open(p)
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        fail(f"{what} {param} is not an image Pillow can read: {e}")
    return p, img


def predict_alpha(img, name):
    """Subject probability per pixel as a Pillow 'L' image the size of img.

    Same preprocessing as rembg for these exports: resize to the network input,
    normalise, NCHW float32; the single output map is min-max scaled and
    resized back with a smooth filter so edges stay soft.
    """
    import numpy as np
    from PIL import Image
    import onnxruntime as ort

    spec = MODELS[name]
    path, _ = ensure_model(name)
    small = img.convert("RGB").resize(spec["size"], Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float32)
    if spec["div"] == "max":
        arr = arr / (arr.max() if arr.max() > 0 else 255.0)
    else:
        arr = arr / spec["div"]
    arr = (arr - np.asarray(spec["mean"], dtype=np.float32)) / np.asarray(spec["std"], dtype=np.float32)
    x = arr.transpose(2, 0, 1)[None]  # 1 x 3 x H x W

    so = ort.SessionOptions()
    so.log_severity_level = 3
    sess = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
    pred = out[0][0] if out.ndim == 4 else out[0]
    lo, hi = float(pred.min()), float(pred.max())
    pred = (pred - lo) / (hi - lo) if hi > lo else np.zeros_like(pred)
    alpha = Image.fromarray((pred * 255).clip(0, 255).astype("uint8"), mode="L")
    return alpha.resize(img.size, Image.LANCZOS)


def parse_background(value):
    """('transparent', None) | ('colour', (r,g,b)) | ('image', Path) | ('scene', str).

    A colour is anything Pillow's ImageColor understands (names, #hex, rgb());
    then a workspace file; anything else that reads like words is a scene to
    draw with the image model. Decided in this order because a file called
    'blue' is far less likely than the model writing 'blue'. Something that
    looks like a path or a hex code but matches nothing is an error, not a
    scene: drawing "#1e3a8" or "photo.pnf" would hide a typo behind a minute
    of generation.
    """
    from PIL import ImageColor
    v = (value or "").strip()
    if not v or v.lower() in ("transparent", "none", "alpha", "cutout", "cut-out"):
        return "transparent", None
    try:
        return "colour", ImageColor.getrgb(v)[:3]
    except ValueError:
        pass
    p = Path(v).expanduser()
    if p.is_file():
        return "image", p
    # "solid blue", "light blue", "a uniform dark green background": a colour
    # said in words. Seen live: a 4B wrote 'solid blue' and, before this, the
    # tool drew a "solid blue" scene with the image model — 30 s for a fill.
    words = [w for w in re.split(r"[\s_-]+", v.lower()) if w and w not in COLOUR_FILLER]
    if words:
        try:
            return "colour", ImageColor.getrgb("".join(words))[:3]
        except ValueError:
            pass
    looks_like_path = v.startswith(("#", "/", "~", ".")) or "/" in v or re.search(
        r"\.(png|jpe?g|webp|gif|bmp|tiff?)$", v, re.I)
    if looks_like_path or len(v.split()) < 2:
        fail(f"background '{value}' is neither a colour (e.g. 'blue', '#1e3a8a', "
             "'transparent'), nor the path of an existing workspace image, nor a "
             "description of a scene (at least two words in English, e.g. 'tropical "
             "beach at sunset').")
    return "scene", v


def find_tool(tool_id):
    """The `run` of another tool, the way the app resolves it: user layer first
    (MYAGENT_TOOLS or $MYAGENT_HOME/tools, flat or grouped), then the bundled
    tree this file lives in."""
    user_root = os.environ.get("MYAGENT_TOOLS") or os.path.join(
        os.environ.get("MYAGENT_HOME") or os.path.expanduser("~/myagent"), "tools")
    for root in (Path(user_root), Path(__file__).resolve().parent.parent.parent):
        if root.is_dir():
            hits = sorted(root.glob(f"**/{tool_id}/run"))
            if hits:
                return hits[0]
    return None


def draw_scene(prompt):
    """Have generate_image draw an EMPTY scene; return the path it saved.

    The two-step recipe (draw the scene, then cut the subject onto it) was
    what a 4B model could not hold across two calls: it drew the person into
    the scene and stopped. So the second step calls the first one itself,
    through the same overlay the app uses, so a per-machine wrapper in the
    user layer (contrib/jetson-orin-8gb) is honoured."""
    import subprocess
    run = find_tool("generate_image")
    if run is None:
        fail("the generate_image tool is not installed, so a scene cannot be drawn: "
             "pass a colour or the path of an existing image as background.")
    params = {"prompt": f"{prompt}, empty scene, wide view, no people",
              "negative_prompt": "person, people, man, woman, child, face, figure, "
                                 "silhouette, text, watermark",
              "filename": f"scene-{slug(prompt, 'background')[:40]}"}
    try:
        proc = subprocess.run([str(run)], input=json.dumps(params), text=True,
                              capture_output=True, timeout=240, env=os.environ)
    except subprocess.TimeoutExpired:
        fail("the image model took more than 4 minutes to draw the scene")
    out = proc.stdout.strip()
    m = re.search(r"^\[\[resource:([^|\]\n]+)\|", out, re.M)
    if proc.returncode != 0 or not m:
        first = (out or proc.stderr.strip()).splitlines()[:1]
        fail(f"generate_image could not draw the scene: {first[0] if first else 'no output'}")
    return Path(m.group(1))


def cover(bg, size):
    """Scale bg to cover size, then centre-crop: no stretching, no borders."""
    from PIL import Image
    w, h = size
    bw, bh = bg.size
    scale = max(w / bw, h / bh)
    bg = bg.resize((max(1, round(bw * scale)), max(1, round(bh * scale))), Image.LANCZOS)
    left = (bg.width - w) // 2
    top = (bg.height - h) // 2
    return bg.crop((left, top, left + w, top + h))


def compose(img, alpha, kind, value):
    """The subject (img masked by alpha) over the requested background, RGBA."""
    from PIL import Image
    cut = img.convert("RGBA")
    cut.putalpha(alpha)
    if kind == "transparent":
        return cut
    if kind == "colour":
        base = Image.new("RGBA", img.size, tuple(value) + (255,))
    else:
        base = cover(Image.open(value).convert("RGBA"), img.size)
    return Image.alpha_composite(base, cut)


def coverage(alpha):
    """Share of pixels the model calls subject (alpha > 50%)."""
    hist = alpha.histogram()
    total = sum(hist) or 1
    return sum(hist[128:]) / total


def prefetch():
    """`remove_background.py --prefetch`: fetch the model now (install.sh offers it)."""
    name = model_name()
    path, downloaded = ensure_model(name)
    mb = os.path.getsize(path) // (1024 * 1024)
    print(f"{'Downloaded' if downloaded else 'Already present'}: {path} ({mb} MB)")


def main():
    if sys.argv[1:] == ["--prefetch"]:
        prefetch()
        return
    try:
        params = json.load(sys.stdin)
    except Exception as e:
        fail(f"invalid parameters: {e}")
    if not params.get("image"):
        fail("image is required: the path of the picture whose background to remove")

    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        from PIL import Image, ImageOps  # noqa: F401
    except ImportError as e:
        fail(f"a Python package this tool needs is missing ({e.name}). Install it in "
             f"the app virtualenv: {os.path.dirname(sys.executable)}/pip install "
             "onnxruntime pillow numpy")

    name = model_name()
    src, img = load_image(params["image"], "source image")
    kind, value = parse_background(params.get("background"))
    if kind == "image" and value.resolve() == src.resolve():
        fail("background must be a different file from the source image")
    scene = None
    if kind == "scene":
        scene, value, kind = value, draw_scene(value), "image"
        if not value.is_file():
            fail(f"generate_image reported {value} but the file is not in the workspace")

    started = time.time()
    had_model = os.path.isfile(model_path(name))
    alpha = predict_alpha(img, name)
    elapsed = time.time() - started

    result = compose(img, alpha, kind, value)
    if kind == "transparent":
        default_base = f"{src.stem}-cutout"
        where = "a transparent background (PNG with alpha)"
    elif kind == "colour":
        default_base = f"{src.stem}-on-{slug(params.get('background'), 'colour')}"
        where = f"a uniform {params['background'].strip()} background"
    elif scene:
        default_base = f"{src.stem}-in-{slug(scene, 'scene')[:40]}"
        where = (f"a scene the image model drew from '{scene}' (also kept as {value.name}), "
                 "scaled and centre-cropped to fit")
    else:
        default_base = f"{src.stem}-on-{slug(value.stem, 'background')}"
        where = f"the picture {value.name} (scaled and centre-cropped to fit)"
    out_name = free_name(slug(params.get("filename")) if params.get("filename") else default_base)
    mask_name = free_name(f"{src.stem}-mask")
    try:
        result.save(out_name, "PNG")
        # White = background = "repaint here" for edit_image and any A1111 backend.
        ImageOps.invert(alpha).save(mask_name, "PNG")
    except OSError as e:
        fail(f"could not write the result into the workspace: {e}")

    share = coverage(alpha)
    kb = os.path.getsize(out_name) // 1024
    w, h = img.size
    fetched = "" if had_model else " (first use: the segmentation model was downloaded, later calls are faster)"
    print(f"Subject cut out by {name} in {elapsed:.1f}s{fetched}, kept at full resolution "
          f"({w}x{h}), placed on {where} and saved as {out_name} ({kb} KB); the original "
          f"{params['image']} is unchanged. The subject covers {share:.0%} of the picture.")
    if share < 0.03:
        print("WARNING: almost nothing was recognised as subject — the result is probably "
              "empty. Tell the user the picture has no clear foreground subject.")
    elif share > 0.97:
        print("WARNING: almost the whole picture was taken as subject — there is hardly any "
              "background to remove. Tell the user.")
    print(f"Mask of the background saved as {mask_name} (white = background): pass it to "
          "edit_image as 'mask' to have the image model paint a new scene ONLY behind the "
          "subject, which stays untouched.")
    print("The result is already displayed to the user: describe it or offer a further "
          "change, do not paste the path.")
    print(f"[[resource:{out_name}|image/png|{Path(out_name).stem}]]")


if __name__ == "__main__":
    main()
