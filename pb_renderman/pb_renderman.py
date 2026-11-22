# -*- coding: utf-8 -*-
# SPDX-License-Identifier: MIT
"""
pb_renderman.py
================
Browser for the https://physicallybased.info database in Maya + RenderMan.
Creates or updates a PxrSurface from the database values.
Specular: Primary Specular with physical Fresnel (IOR / n,k). Transmissive materials
(glass, water, liquids, plastics...) use the Glass lobe for both refraction and
reflection and leave the Primary Specular untouched,
roughness 0.45 by default (0.001 for transmissive materials), or the database
roughness when it is meaningful (not 0 / 0.5).

Installation:
    1. Copy the pb_renderman folder (this file, __init__.py, README.txt) to
       Documents/maya/scripts/
    2. Create the shelf button once (Script Editor, Python tab):
           import pb_renderman; pb_renderman.install_shelf_button()
    3. Click the button. It runs pb_renderman.show(), which reloads the tool
       each time: updates need no restart.

Usage:
    - Pick a material in the list (search + category filter).
    - "Apply to Selected Materials": updates the PxrSurface nodes that are selected,
      or that are assigned to the selected objects. Other attributes are reset to
      their defaults first (option); connected attributes are kept.
    - "Create & Assign Material": creates a new PxrSurface + shading group and
      assigns it to the selected objects (or creates it unassigned).
    - MyLibrary category: your own PxrSurface presets, stored in
      Documents/maya/physicallybased_MyLibrary (all Maya versions), applied as saved.
      "Save Selected to MyLibrary..." adds the selected PxrSurface to it.

Maya 2022+ (Python 3, PySide2 or PySide6) / RenderMan for Maya 24+.

Author:   Julien Duval
Version:  1.0.0 (2026-11-21)
License:  MIT (see below)
Credits:  Developed with the assistance of Claude (Anthropic).
          Material data: Physically Based API, CC0 (Anton Palmqvist and contributors).

Copyright (c) 2026 Julien Duval

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

__author__ = "Julien Duval"
__version__ = "1.0.0"
__license__ = "MIT"


import json
import math
import os
import re
import ssl
import sys
import threading
import time

try:
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover
    from urllib2 import Request, urlopen

API_URL = "https://api.physicallybased.info/v2/materials"
CACHE_MAX_AGE = 7 * 24 * 3600  # one week
WINDOW_OBJECT = "PhysicallyBasedRenderManWindow"

# Linear sRGB (D65) -> ACEScg (AP1, D60), Bradford adaptation
SRGB_TO_ACESCG = (
    (0.6130974, 0.3395231, 0.0473795),
    (0.0701937, 0.9163539, 0.0134524),
    (0.0206156, 0.1095698, 0.8698147),
)

# --------------------------------------------------------------------------- #
#  Data: download, cache, tolerant parsing
# --------------------------------------------------------------------------- #


_CACHE_DIR = None
_APP_DIR = None


def maya_app_dir():
    """
    Maya's user folder (Documents/maya on Windows). Asked to Maya itself on the
    main thread: inside Maya on Windows, HOME points to Documents, so
    expanduser("~") is not the user folder. Falls back to MAYA_APP_DIR,
    then to the platform default (Windows: USERPROFILE/Documents/maya).
    """
    global _APP_DIR
    if _APP_DIR:
        return _APP_DIR
    folder = None
    if threading.current_thread() is threading.main_thread():
        try:
            from maya import cmds
            folder = cmds.internalVar(userAppDir=True)
        except Exception:
            folder = None
    if not folder:
        folder = os.environ.get("MAYA_APP_DIR")
    if folder:
        _APP_DIR = os.path.normpath(folder)
        return _APP_DIR
    if sys.platform.startswith("win"):
        root = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return os.path.join(root, "Documents", "maya")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Preferences", "Autodesk", "maya")
    return os.path.join(os.path.expanduser("~"), "maya")


def _cache_path():
    """
    Cache folder: always an absolute, writable path.
    Does not call maya.cmds (not thread-safe): uses MAYA_APP_DIR,
    then the user's Documents/maya, home, then the temp folder.
    """
    global _CACHE_DIR
    if _CACHE_DIR:
        return _CACHE_DIR
    import tempfile
    candidates = [maya_app_dir(), tempfile.gettempdir()]
    for base in candidates:
        if not base or not os.path.isabs(base):
            continue
        folder = os.path.join(base, "physicallybased_cache")
        try:
            if not os.path.isdir(folder):
                os.makedirs(folder)
            test = os.path.join(folder, ".write_test")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            _CACHE_DIR = folder
            return folder
        except (OSError, IOError):
            continue
    raise IOError("No writable cache folder found")


def _http_get(url, timeout=20):
    req = Request(url, headers={"User-Agent": "pb_renderman/1.0 (Maya)"})
    try:
        return urlopen(req, timeout=timeout).read()
    except Exception as exc:
        # Some Maya installs (Windows) lack root certificates.
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            print("[PB] SSL certificate could not be verified, retrying without verification.")
            ctx = ssl._create_unverified_context()
            return urlopen(req, timeout=timeout, context=ctx).read()
        raise


def _extract_list(payload):
    """The response can be a list, or a dict {header, data/materials}."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "materials", "items", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
        # V2: {header: {...}, <any key>: [ {name: ...}, ... ]}
        for val in payload.values():
            if isinstance(val, list) and val and isinstance(val[0], dict) and "name" in val[0]:
                return val
        # dict keyed by name
        vals = [v for v in payload.values() if isinstance(v, dict) and "name" in v]
        if vals:
            return vals
    raise ValueError("Unexpected response format")


def load_materials(force_refresh=False, local_file=None):
    """Returns the raw list of materials (dicts)."""
    if local_file:
        with open(local_file, "rb") as f:
            return _extract_list(json.loads(f.read().decode("utf-8")))

    cache = os.path.join(_cache_path(), "materials_v2.json")
    fresh = os.path.isfile(cache) and (time.time() - os.path.getmtime(cache) < CACHE_MAX_AGE)
    if fresh and not force_refresh:
        with open(cache, "rb") as f:
            return _extract_list(json.loads(f.read().decode("utf-8")))
    try:
        raw = _http_get(API_URL)
        data = _extract_list(json.loads(raw.decode("utf-8")))
        with open(cache, "wb") as f:
            f.write(raw)
        return data
    except Exception as exc:
        if os.path.isfile(cache):
            print("[PB] Network unavailable (%s), using cache." % exc)
            with open(cache, "rb") as f:
                return _extract_list(json.loads(f.read().decode("utf-8")))
        raise


def _norm_cs(name):
    n = (name or "").lower().replace("_", "-").replace(" ", "-")
    if "aces" in n:
        return "acescg"
    if "srgb" in n or "rec709" in n or "rec.709" in n or "linear" in n:
        return "srgb-linear"
    return n


def _is_vec3(v):
    return isinstance(v, (list, tuple)) and len(v) == 3 and all(isinstance(x, (int, float)) for x in v)


def _variants(value):
    """
    Normalizes a "color" value into a list of dicts {cs, fmt, value}.
    Accepts: [r,g,b] | [{color:[..], colorSpace:..., format:...}, ...] | {"acescg": [..], ...}
    """
    out = []
    if value is None:
        return out
    if _is_vec3(value):
        out.append({"cs": "srgb-linear", "fmt": None, "value": [float(x) for x in value]})
    elif isinstance(value, dict):
        inner = None
        for k in ("color", "value", "values", "rgb", "ior"):
            if k in value:
                inner = value[k]
                break
        if inner is not None:
            out.append({"cs": _norm_cs(value.get("colorSpace") or value.get("colorspace") or "srgb-linear"),
                        "fmt": value.get("format"), "value": inner})
        else:
            for k, v in value.items():
                out.append({"cs": _norm_cs(k), "fmt": None, "value": v})
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, dict):
                out.extend(_variants(item))
        if not out and value and not isinstance(value[0], dict):
            # e.g. raw complexIor without color space
            out.append({"cs": "srgb-linear", "fmt": None, "value": value})
    return out


def srgb_to_acescg(rgb):
    return [sum(SRGB_TO_ACESCG[i][j] * rgb[j] for j in range(3)) for i in range(3)]


def _fmt_match(fmt_field, wanted):
    if not wanted:
        return True
    if isinstance(fmt_field, (list, tuple)):
        return any(str(f).lower() == wanted.lower() for f in fmt_field)
    return str(fmt_field or "").lower() == wanted.lower()


def pick_color(value, colorspace, fmt=None, allow_convert=True):
    """
    Picks the variant in the requested color space; converts sRGB->ACEScg if needed.
    Accepted formats:
      [r,g,b]
      [{colorSpace, color:[r,g,b]}, ...]                                   (color V2)
      [{format:[...], color:[{colorSpace, color:[r,g,b]}, ...]}, ...]       (specularColor V2)
    """
    if value is None:
        return None
    # specularColor V2: pick the right format first
    if isinstance(value, list) and value and isinstance(value[0], dict) and "format" in value[0] \
            and isinstance(value[0].get("color"), list) and value[0]["color"] and isinstance(value[0]["color"][0], dict):
        entry = next((e for e in value if _fmt_match(e.get("format"), fmt)), value[0])
        return pick_color(entry.get("color"), colorspace)
    vs = [v for v in _variants(value) if _is_vec3(v["value"])]
    if not vs:
        return None
    for v in vs:
        if v["cs"] == colorspace:
            return list(v["value"])
    src = vs[0]
    if allow_convert and colorspace == "acescg" and src["cs"] == "srgb-linear":
        return srgb_to_acescg(src["value"])
    return list(src["value"])


def pick_complex_ior(value, colorspace):
    """Returns (n_rgb, k_rgb) or None."""
    if isinstance(value, list) and value and isinstance(value[0], dict) and "n" in value[0]:
        e = next((x for x in value if _norm_cs(x.get("colorSpace")) == colorspace), value[0])
        try:
            return [float(x) for x in e["n"]], [float(x) for x in e["k"]]
        except Exception:
            return None
    vs = _variants(value)
    if not vs:
        return None
    chosen = next((v for v in vs if v["cs"] == colorspace), vs[0])
    d = chosen["value"]
    try:
        if isinstance(d, dict):
            return [float(x) for x in d["n"]], [float(x) for x in d["k"]]
        flat = d
        if len(d) == 3 and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in d):
            return [float(p[0]) for p in d], [float(p[1]) for p in d]
        if len(flat) == 6:
            return [float(flat[0]), float(flat[2]), float(flat[4])], [float(flat[1]), float(flat[3]), float(flat[5])]
        if len(d) == 2 and all(_is_vec3(x) for x in d):
            return [float(x) for x in d[0]], [float(x) for x in d[1]]
    except Exception:
        pass
    return None


def _num(mat, key, default=None):
    v = mat.get(key, default)
    if isinstance(v, (list, tuple)):
        v = v[0] if v and isinstance(v[0], (int, float)) else default
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def categories(mat):
    c = mat.get("category") or []
    if isinstance(c, str):
        c = [c]
    return [str(x) for x in c if x]


def reference_url(mat):
    """Render preview URL (V2: images=[{format:'jpeg', '300':url, '600':url}])."""
    imgs = mat.get("images") or []
    if isinstance(imgs, dict):
        imgs = [imgs]
    for fmt in ("jpeg", "jpg", "png"):
        for im in imgs:
            if isinstance(im, dict) and str(im.get("format", "")).lower() == fmt:
                for size in ("300", "600"):
                    if im.get(size):
                        return im[size]
    # legacy V1 format
    ref = mat.get("reference")
    if isinstance(ref, str) and ref.lower().endswith((".jpeg", ".jpg", ".png")):
        return ref
    if isinstance(ref, list):
        for r in ref:
            if isinstance(r, str) and r.lower().endswith((".jpeg", ".jpg", ".png")):
                return r
    return None


# --------------------------------------------------------------------------- #
#  Mapping to PxrSurface
# --------------------------------------------------------------------------- #


def gulbrandsen_to_nk(reflectivity, edge_tint):
    """
    Inverse Gulbrandsen mapping (2014, "Artist Friendly Metallic Fresnel"):
    (reflectivity r, edge tint g) -> per-channel (n, k), for physical Fresnel.
    """
    n_out, k_out = [], []
    for r, g in zip(reflectivity, edge_tint):
        r = min(max(float(r), 0.0), 0.99)
        g = min(max(float(g), 0.0), 1.0)
        sr = math.sqrt(r)
        n_min = (1.0 - r) / (1.0 + r)
        n_max = (1.0 + sr) / (1.0 - sr)
        n = g * n_min + (1.0 - g) * n_max
        k2 = (r * (n + 1.0) ** 2 - (n - 1.0) ** 2) / (1.0 - r)
        n_out.append(n)
        k_out.append(math.sqrt(max(k2, 0.0)))
    return n_out, k_out


def fresnel_f0(n, k):
    """Normal-incidence reflectance per channel from (n, k)."""
    return [((a - 1.0) ** 2 + b * b) / ((a + 1.0) ** 2 + b * b) for a, b in zip(n, k)]


DEFAULT_SPEC_ROUGHNESS = 0.45
# Subsurface directionality (Henyey-Greenstein g) by material-name prefix.
# Human skin: g ~0.73 (380 nm) to ~0.84 (780 nm) in the literature.
SSS_DIRECTIONALITY = {"Skin": 0.8}
# Materials always rendered with isotropic scattering, by name prefix.
# Blood (g ~0.99, ~900 scattering events per cm) and coffee (g ~0.89) need far too
# many light bounces in directional mode; isotropic reduced scattering gives the same
# look at a sane cost.
FORCE_ISOTROPIC = ("Blood", "Coffee")
# Thin-film thickness overrides (nm), by material name
THIN_FILM_OVERRIDES = {"Titanium (Anodized)": 300.0}
DEFAULT_SSS_SCALE = 10.0
GLASS_ROUGHNESS = 0.001   # exception for transmissive materials (glass, water...)


def _pick_raw(value, colorspace):
    """(vec3, colorspace_found) without any conversion; prefers the requested space."""
    vs = [v for v in _variants(value) if _is_vec3(v["value"])]
    if not vs:
        return None, None
    for v in vs:
        if v["cs"] == colorspace:
            return [float(x) for x in v["value"]], v["cs"]
    return [float(x) for x in vs[0]["value"]], vs[0]["cs"]


def _convert_coefficients(sigma, ref_cm=None):
    """
    sRGB-linear -> ACEScg for extinction coefficients. Coefficients are not colors:
    the matrix is applied to the transmittance over a reference distance,
    then converted back with -ln(T) / d.
    """
    positive = [x for x in sigma if x > 0]
    if not positive:
        return [0.0, 0.0, 0.0]
    d = ref_cm or (math.log(2.0) / (sum(positive) / len(positive)))   # ~50% transmittance
    t = [math.exp(-max(x, 0.0) * d) for x in sigma]
    t2 = [min(max(x, 1e-6), 1.0) for x in srgb_to_acescg(t)]
    return [max(0.0, -math.log(x) / d) for x in t2]


def _diffuse_reflectance(alpha_r):
    """Multiple-scattering diffuse reflectance of a semi-infinite medium from its
    reduced albedo (van de Hulst / Jensen approximation)."""
    sq = math.sqrt(max(0.0, 1.0 - min(max(alpha_r, 0.0), 1.0)))
    return (1.0 - sq) * (1.0 - 0.139 * sq) / (1.0 + 1.17 * sq)


def _invert_reflectance(r):
    """Reduced albedo giving diffuse reflectance r (bisection)."""
    r = min(max(r, 0.0), _diffuse_reflectance(1.0))
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _diffuse_reflectance(mid) < r:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def interior_coefficients(mat, color, colorspace, reduce_scattering=False):
    """
    Interior volume of transmissive materials (Beer-Lambert).
    Returns (extinction, ssAlbedo, g0, source). Coefficients are per cm.
      1. volumeCoefficients {absorption, scattering, anisotropy} from the database.
         The anisotropy goes to g0. With reduce_scattering (Non-directional option),
         sigma_s' = sigma_s * (1 - g) is used instead, with g0 = 0 (faster).
      2. otherwise from color + transmissionDepth (mm): sigma = -ln(color) / depth
    Color-space conversion (sRGB -> ACEScg) is never applied to coefficients or albedos
    directly: extinction goes through transmittance, albedo through the
    multiple-scattering diffuse reflectance, which are the quantities we actually see.
    """
    vc = mat.get("volumeCoefficients") or {}
    absorption, a_cs = _pick_raw(vc.get("absorption"), colorspace) if isinstance(vc, dict) else (None, None)
    if absorption:
        # work in the space the coefficients come from, convert at the end
        sig_a = [max(x, 0.0) for x in absorption]
        sig_s, _ = _pick_raw(vc.get("scattering"), a_cs)
        sig_s = [max(x, 0.0) for x in (sig_s or [0.0, 0.0, 0.0])]
        g, _ = _pick_raw(vc.get("anisotropy"), a_cs)
        g = [min(max(x, -0.999), 0.999) for x in (g or [0.0, 0.0, 0.0])]
        scatters = any(x > 0 for x in sig_s)
        g_mean = sum(g) / 3.0 if scatters else 0.0
        src = "database coefficients"

        # reduced (transport) quantities, valid in both modes
        sig_s_r = [x * (1.0 - gi) for x, gi in zip(sig_s, g)]
        sig_t_r = [a + x for a, x in zip(sig_a, sig_s_r)]
        alb_r = [(x / t) if t > 0 else 0.0 for x, t in zip(sig_s_r, sig_t_r)]

        if colorspace == "acescg" and a_cs == "srgb-linear":
            depth_mm = _num(mat, "transmissionDepth")
            ref = depth_mm / 10.0 if depth_mm else None
            if scatters:
                sig_t_r = _convert_coefficients(sig_t_r, ref)
                refl = srgb_to_acescg([_diffuse_reflectance(x) for x in alb_r])
                alb_r = [_invert_reflectance(x) for x in refl]
                src += ", sRGB->ACEScg via diffuse reflectance"
            else:
                sig_t_r = _convert_coefficients(sig_a, ref)
                src += ", sRGB->ACEScg via transmittance"
            g_c = [g_mean] * 3
        else:
            g_c = g

        if not scatters:
            return sig_t_r, [0.0, 0.0, 0.0], 0.0, src
        if reduce_scattering:
            src += ", non directional: reduced scattering (g %.3g -> g0 0)" % g_mean
            return sig_t_r, alb_r, 0.0, src
        # directional: expand back to the real scattering with the database anisotropy
        sa = [t * (1.0 - ar) for t, ar in zip(sig_t_r, alb_r)]
        ss = [t * ar / (1.0 - gi) for t, ar, gi in zip(sig_t_r, alb_r, g_c)]
        ext = [x + y for x, y in zip(sa, ss)]
        alb = [(y / e) if e > 0 else 0.0 for y, e in zip(ss, ext)]
        src += ", g0 %.3g" % g_mean
        return ext, alb, g_mean, src
    depth_mm = _num(mat, "transmissionDepth")
    if depth_mm and depth_mm > 0:
        depth_cm = depth_mm / 10.0
        ext = [max(0.0, -math.log(min(max(c, 1e-4), 1.0))) / depth_cm for c in color]
        return ext, [0.0, 0.0, 0.0], 0.0, "color at %.3g mm" % depth_mm
    # no depth given: color reached after 1 cm
    ext = [max(0.0, -math.log(min(max(c, 1e-4), 1.0))) for c in color]
    return ext, [0.0, 0.0, 0.0], 0.0, "color at 1 cm (no depth in database)"


def build_pxr_values(mat, colorspace="acescg", sss_scale=DEFAULT_SSS_SCALE, roughness=DEFAULT_SPEC_ROUGHNESS,
                     extinction_scale=1.0, reduce_scattering=False, use_db_roughness=True):
    """
    Builds a {PxrSurface attribute: value} dict from a material.
    Opaque materials: Primary Specular with physical Fresnel.
    Transmissive materials: Glass lobe only (refraction + reflection + interior).
    roughness : default roughness (0.45; transmissive materials use 0.001).
    use_db_roughness : use the database roughness when it is meaningful (not 0 / 0.5).
    sss_scale : multiplier applied to the database subsurface radius (mm) -> subsurfaceDmfp.
    extinction_scale : multiplier on interior extinction (coefficients are per cm;
                       1.0 for a scene in cm, 100 for a scene in meters).
    """
    v = {}
    notes = []
    color = pick_color(mat.get("color"), colorspace) or [0.18, 0.18, 0.18]
    db_rough = _num(mat, "roughness", 0.5)
    roughness = DEFAULT_SPEC_ROUGHNESS if roughness is None else roughness
    metal = _num(mat, "metalness", 0.0) or 0.0
    ior = _num(mat, "ior", 1.5) or 1.5
    trans = _num(mat, "transmission", 0.0) or 0.0
    db_meaningful = mat.get("roughness") is not None and db_rough not in (0.0, 0.5)
    if use_db_roughness and db_meaningful:
        # 0 and 0.5 are generic placeholders in the database; other values are deliberate
        rough, tag = db_rough, " (database)"
    elif trans > 0.0 and metal < 0.5:
        rough, tag = GLASS_ROUGHNESS, " (glass exception)"
    else:
        rough, tag = float(roughness), ""

    # Reset lobes so switching materials is clean
    v.update({
        "diffuseGain": 1.0, "diffuseRoughness": 0.0,
        "refractionGain": 0.0,
        "reflectionGain": 0.0,           # Glass lobe reflection off unless transmissive
        "subsurfaceGain": 0.0, "subsurfaceDirectionality": 0.0,
        "extinction": [0.0, 0.0, 0.0], "ssAlbedo": [0.0, 0.0, 0.0], "g": 0.0, "multiScatter": 0,
        "refractionColor": [1.0, 1.0, 1.0],
        "iridescenceFaceGain": 0.0, "iridescenceEdgeGain": 0.0,
        "specularModelType": 1,          # GGX
        "specularFresnelMode": 1,        # Physical, always
        "specularFaceColor": [1.0, 1.0, 1.0],
        "specularEdgeColor": [1.0, 1.0, 1.0],
        "specularRoughness": rough,
    })
    notes.append("Primary Specular: physical Fresnel, roughness %.3f%s" % (rough, tag))

    if metal >= 0.5:
        # ---- Conductor ----
        v["diffuseGain"] = 0.0
        nk = pick_complex_ior(mat.get("complexIor"), colorspace)
        if nk and max(abs(a - b) for a, b in zip(fresnel_f0(*nk), color)) > 0.02:
            # e.g. anodized aluminum: tinted color, but n,k of raw aluminum
            nk = None
            notes.append("Metal: database n, k do not match its color (tinted variant)")
        if nk:
            notes.append("Metal: complex IOR (n, k) from database")
        else:
            edge = (pick_color(mat.get("specularColor"), colorspace, fmt="gulbrandsen")
                    or pick_color(mat.get("specularColor"), colorspace) or [1.0, 1.0, 1.0])
            nk = gulbrandsen_to_nk(color, edge)
            notes.append("Metal: n, k derived from color + specularColor (Gulbrandsen)")
        v["specularIor"] = nk[0]
        v["specularExtinctionCoeff"] = nk[1]
    else:
        # ---- Dielectric ----
        if trans <= 0.0:
            v["diffuseColor"] = color      # transmissive: tint is carried by the interior extinction
        v["diffuseRoughness"] = _num(mat, "diffuseRoughness", 0.0) or 0.0
        v["specularIor"] = [ior, ior, ior]
        v["specularExtinctionCoeff"] = [0.0, 0.0, 0.0]
        notes.append("Dielectric: IOR %.3f" % ior)

        if trans > 0.0:
            v["refractionGain"] = trans
            v["refractionColor"] = [1.0, 1.0, 1.0]   # tint comes from the interior extinction
            v["glassIor"] = ior
            v["glassRoughness"] = rough
            v["diffuseGain"] = max(0.0, 1.0 - trans)
            # Anything using the Glass lobe also takes its reflection from the Glass lobe.
            # Primary Specular is left untouched (reset brings it back to its default, off).
            v["reflectionGain"] = 1.0
            for k in [k for k in v if k.startswith("specular")]:
                del v[k]
            notes[:] = [n for n in notes if not n.startswith("Primary Specular") and not n.startswith("Dielectric")]
            notes.insert(0, "Transmissive: Glass lobe refraction + reflection (IOR %.4g, roughness %.3f), "
                            "Primary Specular untouched" % (ior, rough))
            forced = any(mat.get("name", "").startswith(p) for p in FORCE_ISOTROPIC)
            ext, alb, g0, src = interior_coefficients(mat, color, colorspace, reduce_scattering or forced)
            if forced and not reduce_scattering:
                src += " [always non directional for this material]"
            v["extinction"] = [x * extinction_scale for x in ext]
            v["ssAlbedo"] = alb
            v["g"] = g0                  # the interior "g0" parameter is the Maya attribute "g"
            notes.append("Interior extinction %s /cm x %.3g (%s)"
                         % (", ".join("%.4g" % x for x in ext), extinction_scale, src))
            if max(alb) > 0.0:
                # without it the interior is single-scatter only: dense media (blood) go black
                v["multiScatter"] = 1
                notes.append("Interior: single scatter albedo %s, g0 %.3g, Multiscattering ON (not supported in XPU)"
                             % (", ".join("%.3g" % x for x in alb), g0))

        radius = mat.get("subsurfaceRadius")
        if _is_vec3(radius) and max(radius) > 0:
            mx = float(max(radius))
            v["subsurfaceGain"] = 1.0
            v["subsurfaceColor"] = color
            v["subsurfaceDmfp"] = mx * sss_scale
            v["subsurfaceDmfpColor"] = [float(r) / mx for r in radius]
            v["diffuseGain"] = 0.0
            notes.append("SSS: max radius %.3g mm x %.3g" % (mx, sss_scale))
            g_sss = next((g for prefix, g in SSS_DIRECTIONALITY.items()
                          if mat.get("name", "").startswith(prefix)), None)
            if g_sss:
                if reduce_scattering:
                    notes.append("SSS: directionality %.2g -> 0 (Non-directional option)" % g_sss)
                else:
                    v["subsurfaceDirectionality"] = g_sss
                    notes.append("SSS: directionality %.2g" % g_sss)

    tf_raw = mat.get("thinFilmThickness")
    tf = float(tf_raw[2]) if isinstance(tf_raw, list) and len(tf_raw) == 3 else _num(mat, "thinFilmThickness")
    tf_override = THIN_FILM_OVERRIDES.get(mat.get("name"))
    if tf and tf_override:
        tf = tf_override
    if tf:
        # PxrSurface Iridescence lobe, Physical mode (film thickness in nm).
        # PxrSurface has no film IOR: it sets the face reflectance instead,
        # F0 = ((n - 1) / (n + 1))^2, rising to 1 at grazing angles (Schlick, shape 5).
        film_ior = _num(mat, "thinFilmIor") or 1.5
        f0 = ((film_ior - 1.0) / (film_ior + 1.0)) ** 2
        v["iridescenceMode"] = 1
        v["iridescenceThickness"] = tf
        v["iridescenceFaceGain"] = f0
        v["iridescenceEdgeGain"] = 1.0
        v["iridescenceFresnelShape"] = 5.0
        v["iridescenceRoughness"] = rough
        v["iridescenceDoubleSided"] = 1 if trans > 0.0 else 0
        rng = ""
        if tf_override:
            rng = " (override, database %g nm)" % (_num(mat, "thinFilmThickness") or 0)
        elif isinstance(tf_raw, list) and len(tf_raw) == 3:
            rng = " (database range %g-%g nm)" % (tf_raw[0], tf_raw[1])
        notes.append("Iridescence: physical, %.0f nm%s, film IOR %.3g -> face %.3f, edge 1%s"
                     % (tf, rng, film_ior, f0, ", double sided" if trans > 0.0 else ""))

    return v, notes


# --------------------------------------------------------------------------- #
#  Maya
# --------------------------------------------------------------------------- #


def _ensure_renderman():
    from maya import cmds
    if not cmds.pluginInfo("RenderMan_for_Maya", q=True, loaded=True):
        cmds.loadPlugin("RenderMan_for_Maya")


def maya_rendering_space():
    """Maya rendering space name ('' if color management is off)."""
    try:
        from maya import cmds
        if cmds.colorManagementPrefs(q=True, cmEnabled=True):
            return cmds.colorManagementPrefs(q=True, renderingSpaceName=True) or ""
    except Exception:
        pass
    return ""


def detect_colorspace():
    """
    'acescg'      if the Maya rendering space is ACEScg (AP1),
    'srgb-linear' if it is linear Rec.709 / sRGB,
    None          otherwise (ACES2065-1, Rec.2020, ...) -> pick manually.
    """
    n = maya_rendering_space().lower().replace(" ", "")
    if not n:
        return "srgb-linear"
    if "acescg" in n or "ap1" in n:
        return "acescg"
    if "2065" in n or "ap0" in n or "2020" in n or "p3" in n:
        return None
    if "709" in n or "srgb" in n or "linear" in n:
        return "srgb-linear"
    return None


# Args parameter names that differ from the Maya attribute names
_ATTR_ALIASES = {"g": "g0", "g0": "g"}


def _set(node, attr, value, log):
    from maya import cmds
    if not cmds.attributeQuery(attr, node=node, exists=True) and attr in _ATTR_ALIASES \
            and cmds.attributeQuery(_ATTR_ALIASES[attr], node=node, exists=True):
        attr = _ATTR_ALIASES[attr]
    if not cmds.attributeQuery(attr, node=node, exists=True):
        log.append("  (skipped) %s does not exist in this version" % attr)
        return
    plug = "%s.%s" % (node, attr)
    if cmds.listConnections(plug, s=True, d=False):
        log.append("  (skipped) %s is connected" % attr)
        return
    parent = cmds.attributeQuery(attr, node=node, listParent=True)
    if parent and cmds.listConnections("%s.%s" % (node, parent[0]), s=True, d=False):
        log.append("  (skipped) %s is connected" % parent[0])
        return
    if isinstance(value, str):
        cmds.setAttr(plug, value, type="string")
    elif isinstance(value, (list, tuple)):
        cmds.setAttr(plug, *[float(x) for x in value], type="double3")
    else:
        cmds.setAttr(plug, value)


def _safe_name(name):
    out = "".join(c if c.isalnum() else "_" for c in name)
    return ("PB_" + out).strip("_")


def _shaders_from_geometry(nodes):
    """PxrSurface nodes assigned to the given objects / components."""
    from maya import cmds
    found = []
    shapes = set()
    for n in nodes:
        obj = n.split(".")[0]
        if cmds.objectType(obj, isAType="shape"):
            shapes.add(obj)
        else:
            shapes.update(cmds.listRelatives(obj, allDescendents=True, shapes=True,
                                             fullPath=True, noIntermediate=True) or [])
    for shape in shapes:
        for sg in set(cmds.listConnections(shape, type="shadingEngine") or []):
            for plug in ("surfaceShader", "rman__surface"):
                if not cmds.attributeQuery(plug, node=sg, exists=True):
                    continue
                for src in cmds.listConnections("%s.%s" % (sg, plug), s=True, d=False) or []:
                    if cmds.nodeType(src) == "PxrSurface" and src not in found:
                        found.append(src)
    return found


_NUMERIC_TYPES = ("float", "double", "long", "short", "bool", "enum", "byte", "char",
                  "doubleLinear", "doubleAngle", "floatLinear", "floatAngle", "time")
_DG_ATTRS = {"caching", "frozen", "nodeState", "isHistoricallyInteresting", "binMembership"}


def reset_to_defaults(node, log):
    """
    Resets every numeric, unlocked, unconnected attribute of the node to its
    default value. Connected attributes (textures, patterns) are left untouched.
    """
    from maya import cmds
    kept = []
    for attr in cmds.listAttr(node, settable=True, scalar=True) or []:
        if attr in _DG_ATTRS or "." in attr:
            continue
        plug = "%s.%s" % (node, attr)
        try:
            if cmds.getAttr(plug, type=True) not in _NUMERIC_TYPES:
                continue
            if cmds.getAttr(plug, lock=True):
                continue
            if cmds.listConnections(plug, s=True, d=False):
                kept.append(attr)
                continue
            default = cmds.attributeQuery(attr, node=node, listDefault=True)
            if default:
                cmds.setAttr(plug, default[0])
        except Exception:
            continue
    # a connected color also has connected children: report the parent only once
    if kept:
        log.append("  (kept, connected) " + ", ".join(sorted(set(kept))))


def _set_values(shaders, mat, values, log):
    from maya import cmds
    for sh in shaders:
        for attr, val in values.items():
            _set(sh, attr, val, log)
        if not cmds.attributeQuery("pbMaterial", node=sh, exists=True):
            cmds.addAttr(sh, ln="pbMaterial", dt="string")
        cmds.setAttr(sh + ".pbMaterial", mat.get("name", ""), type="string")


def _report(mat, shaders, colorspace, notes, log):
    msg = "[PB] %s -> %s (%s)" % (mat.get("name"), ", ".join(s.split("|")[-1] for s in shaders), colorspace)
    print("\n".join([msg] + ["  " + n for n in notes] + log))


def apply_to_selection(mat, colorspace="acescg", sss_scale=DEFAULT_SSS_SCALE, roughness=DEFAULT_SPEC_ROUGHNESS,
                       extinction_scale=1.0, reduce_scattering=False, use_db_roughness=True,
                       reset=True):
    """
    Updates existing PxrSurface nodes: the selected ones, plus those assigned
    to the selected objects. Creates nothing.
    reset : first reset all other (unconnected) attributes to their defaults.
    """
    from maya import cmds
    _ensure_renderman()
    values, notes = build_pxr_values(mat, colorspace, sss_scale, roughness, extinction_scale, reduce_scattering,
                                     use_db_roughness)
    sel = cmds.ls(sl=True, long=True) or []
    shaders = [s for s in sel if cmds.nodeType(s) == "PxrSurface"]
    for sh in _shaders_from_geometry([s for s in sel if s not in shaders]):
        if sh not in shaders:
            shaders.append(sh)
    if not shaders:
        raise RuntimeError("No PxrSurface selected or assigned to the selection.")
    log = []
    cmds.undoInfo(openChunk=True, chunkName="PhysicallyBased_apply")
    try:
        if reset:
            for sh in shaders:
                reset_to_defaults(sh, log)
            notes.append("Other attributes reset to defaults")
        _set_values(shaders, mat, values, log)
    finally:
        cmds.undoInfo(closeChunk=True)
    _report(mat, shaders, colorspace, notes, log)
    return shaders, notes


def create_on_selection(mat, colorspace="acescg", sss_scale=DEFAULT_SSS_SCALE, roughness=DEFAULT_SPEC_ROUGHNESS,
                        extinction_scale=1.0, reduce_scattering=False, use_db_roughness=True):
    """
    Creates a new PxrSurface + shading group and assigns it to the selected
    objects / faces. With nothing selected, the material is created unassigned.
    """
    from maya import cmds
    _ensure_renderman()
    values, notes = build_pxr_values(mat, colorspace, sss_scale, roughness, extinction_scale, reduce_scattering,
                                     use_db_roughness)
    sel = cmds.ls(sl=True, long=True) or []
    geo = [s for s in sel if cmds.nodeType(s.split(".")[0]) not in ("PxrSurface", "shadingEngine")]
    log = []
    cmds.undoInfo(openChunk=True, chunkName="PhysicallyBased_create")
    try:
        base = _safe_name(mat.get("name", "Material"))
        shader = cmds.shadingNode("PxrSurface", asShader=True, name=base + "_PxrSurface")
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=base + "_SG")
        cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader", force=True)
        if geo:
            cmds.sets(geo, edit=True, forceElement=sg)
        _set_values([shader], mat, values, log)
    finally:
        cmds.undoInfo(closeChunk=True)
        if sel:
            cmds.select(sel, r=True)
    _report(mat, [shader], colorspace, notes, log)
    return [shader], notes


# --------------------------------------------------------------------------- #
#  MyLibrary: personal PxrSurface presets, kept outside Maya's preset folders
# --------------------------------------------------------------------------- #

LIBRARY_CATEGORY = "MyLibrary"
LIBRARY_FOLDER = "physicallybased_MyLibrary"
PRESET_PREFIX = "PxrSurface_"
_RE_NUM = re.compile(r'blendAttr\s+"(\w+)"\s+([-+0-9.eE]+)\s*;')
_RE_STR = re.compile(r'blendAttrStr\s+"(\w+)"\s+"((?:[^"\\]|\\.)*)"\s*;')
_PRESET_SKIP = {"frozen", "inputMaterial", "caching", "nodeState", "isHistoricallyInteresting"}


def library_dir(create=True):
    """
    Personal library folder, next to the cache: Documents/maya/physicallybased_MyLibrary
    (Maya's user folder). Version independent, outside Maya's own preset folders.
    """
    folder = os.path.join(maya_app_dir(), LIBRARY_FOLDER)
    if create and not os.path.isdir(folder):
        os.makedirs(folder)
    return folder


def parse_preset(path):
    """Reads a Maya attribute preset (.mel) into {attribute: value}."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    attrs = {}
    for name, val in _RE_NUM.findall(text):
        try:
            v = float(val)
            attrs[name] = int(v) if v.is_integer() and "." not in val else v
        except ValueError:
            pass
    for name, val in _RE_STR.findall(text):
        attrs[name] = val.replace('\\"', '"').replace("\\\\", "\\")
    return attrs


def load_library(folder=None):
    """Lists the presets of the library folder as entries of the MyLibrary category."""
    folder = folder or library_dir()
    out = []
    if not os.path.isdir(folder):
        return out
    for fn in sorted(os.listdir(folder), key=str.lower):
        if not fn.lower().endswith(".mel"):
            continue
        stem = fn[:-4]
        display = stem[len(PRESET_PREFIX):] if stem.startswith(PRESET_PREFIX) else stem
        path = os.path.join(folder, fn)
        try:
            attrs = parse_preset(path)
        except Exception as exc:
            print("[PB] Could not read preset %s: %s" % (fn, exc))
            continue
        image = next((os.path.join(folder, stem + ext) for ext in (".png", ".jpg", ".jpeg")
                      if os.path.isfile(os.path.join(folder, stem + ext))), None)
        out.append({"name": display, "category": [LIBRARY_CATEGORY], "_library": True,
                    "_preset": stem, "_path": path, "_attrs": attrs, "_image": image})
    return out


def _preset_color(a, name):
    keys = [name + c for c in "RGB"]
    return [float(a[k]) for k in keys] if all(k in a for k in keys) else None


def preset_swatch(a):
    """Approximate color of a preset, for the list and the swatch."""
    if a.get("refractionGain", 0) > 0:
        return _preset_color(a, "refractionColor")
    if a.get("subsurfaceGain", 0) > 0:
        return _preset_color(a, "subsurfaceColor")
    if a.get("diffuseGain", 0) > 0:
        return _preset_color(a, "diffuseColor")
    n, k = _preset_color(a, "specularIor"), _preset_color(a, "specularExtinctionCoeff")
    if a.get("specularFresnelMode") == 1 and n and k and max(k) > 0:
        return fresnel_f0(n, k)
    return _preset_color(a, "specularFaceColor") or _preset_color(a, "diffuseColor")


def non_directional(attrs):
    """
    Non-directional version of a preset: directional scattering is replaced by
    isotropic scattering. The preset file is not changed.
      - Glass interior with a phase function (g, or g/g1/blend double lobe):
        reduced scattering sigma_s * (1 - g_eff), extinction and single-scatter
        albedo recomputed, g = g1 = blend = 0.
      - Subsurface and Single Scatter Directionality -> 0.
    Returns (new attrs, notes).
    """
    a = dict(attrs)
    notes = []
    gkey = "g" if "g" in a else ("g0" if "g0" in a else None)
    g = float(a.get(gkey, 0) or 0) if gkey else 0.0
    g1 = float(a.get("g1", 0) or 0)
    blend = float(a.get("blend", 0) or 0)
    g_eff = (1.0 - blend) * g + blend * g1
    ext, alb = _preset_color(a, "extinction"), _preset_color(a, "ssAlbedo")
    if abs(g_eff) > 1e-6 and ext and alb and max(alb) > 0:
        g_eff = min(max(g_eff, -0.999), 0.999)
        new_e, new_a = [], []
        for e, al in zip(ext, alb):
            sig_s = e * al
            sig_a = e - sig_s
            sig_s_r = sig_s * (1.0 - g_eff)
            t = sig_a + sig_s_r
            new_e.append(t)
            new_a.append(sig_s_r / t if t > 0 else 0.0)
        for i, c in enumerate("RGB"):
            a["extinction" + c] = new_e[i]
            a["ssAlbedo" + c] = new_a[i]
        a[gkey or "g"] = 0.0
        if "g1" in a:
            a["g1"] = 0.0
        if "blend" in a:
            a["blend"] = 0.0
        notes.append("Non-directional: interior g %.3g -> 0, extinction %s, albedo %s"
                     % (g_eff, ", ".join("%.4g" % x for x in new_e), ", ".join("%.3g" % x for x in new_a)))
    for key, label in (("subsurfaceDirectionality", "subsurface"), ("singlescatterDirectionality", "single scatter")):
        val = float(a.get(key, 0) or 0)
        if abs(val) > 1e-6:
            notes.append("Non-directional: %s directionality %.3g -> 0" % (label, val))
            a[key] = 0.0
    return a, notes


def _apply_preset(shaders, entry, log, nondirectional=False):
    from maya import cmds
    attrs = entry["_attrs"]
    if nondirectional:
        attrs, notes = non_directional(attrs)
        log.extend("  " + n for n in notes)
    for sh in shaders:
        for attr, val in attrs.items():
            if attr in _PRESET_SKIP or attr == "pbMaterial":
                continue
            _set(sh, attr, val, log)
        if not cmds.attributeQuery("pbMaterial", node=sh, exists=True):
            cmds.addAttr(sh, ln="pbMaterial", dt="string")
        cmds.setAttr(sh + ".pbMaterial", "%s/%s" % (LIBRARY_CATEGORY, entry["name"]), type="string")


def apply_preset_to_selection(entry, nondirectional=False):
    """Library preset -> the selected PxrSurface nodes (or those assigned to the selection)."""
    from maya import cmds
    _ensure_renderman()
    sel = cmds.ls(sl=True, long=True) or []
    shaders = [s for s in sel if cmds.nodeType(s) == "PxrSurface"]
    for sh in _shaders_from_geometry([s for s in sel if s not in shaders]):
        if sh not in shaders:
            shaders.append(sh)
    if not shaders:
        raise RuntimeError("No PxrSurface selected or assigned to the selection.")
    log = []
    cmds.undoInfo(openChunk=True, chunkName="Library_apply")
    try:
        _apply_preset(shaders, entry, log, nondirectional)
    finally:
        cmds.undoInfo(closeChunk=True)
    print("\n".join(["[PB] %s/%s -> %s" % (LIBRARY_CATEGORY, entry["name"], ", ".join(s.split("|")[-1] for s in shaders))] + log))
    return shaders, []


def create_preset_on_selection(entry, nondirectional=False):
    """Library preset -> new PxrSurface + shading group assigned to the selection."""
    from maya import cmds
    _ensure_renderman()
    sel = cmds.ls(sl=True, long=True) or []
    geo = [s for s in sel if cmds.nodeType(s.split(".")[0]) not in ("PxrSurface", "shadingEngine")]
    log = []
    cmds.undoInfo(openChunk=True, chunkName="Library_create")
    try:
        base = "Lib_" + "".join(c if c.isalnum() else "_" for c in entry["name"]).strip("_")
        shader = cmds.shadingNode("PxrSurface", asShader=True, name=base + "_PxrSurface")
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name=base + "_SG")
        cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader", force=True)
        if geo:
            cmds.sets(geo, edit=True, forceElement=sg)
        _apply_preset([shader], entry, log, nondirectional)
    finally:
        cmds.undoInfo(closeChunk=True)
        if sel:
            cmds.select(sel, r=True)
    print("\n".join(["[PB] %s/%s -> %s (new)" % (LIBRARY_CATEGORY, entry["name"], shader)] + log))
    return [shader], []


def selected_pxrsurface():
    """First PxrSurface selected, or assigned to the selection."""
    from maya import cmds
    sel = cmds.ls(sl=True, long=True) or []
    shaders = [s for s in sel if cmds.nodeType(s) == "PxrSurface"]
    shaders += _shaders_from_geometry([s for s in sel if s not in shaders])
    return shaders[0] if shaders else None


def preset_file_name(name):
    clean = "".join(c if (c.isalnum() or c == "_") else "_" for c in name.strip()).strip("_")
    return PRESET_PREFIX + clean if clean else ""


def delete_from_library(entry):
    """
    Moves a preset (and its preview image) to the library's _trash subfolder.
    Recoverable: move the files back to restore it. Returns the trash folder.
    """
    import shutil
    trash = os.path.join(library_dir(), "_trash")
    if not os.path.isdir(trash):
        os.makedirs(trash)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for path in [entry["_path"], entry.get("_image")]:
        if path and os.path.isfile(path):
            stem, ext = os.path.splitext(os.path.basename(path))
            shutil.move(path, os.path.join(trash, "%s_%s%s" % (stem, stamp, ext)))
    return trash


THUMBNAIL_SIZE = 256              # rendered resolution (Preset Browser: 128 px), shown 1:1
THUMBNAIL_MIN_SAMPLES = 16        # Preset Browser: 32
THUMBNAIL_MAX_SAMPLES = 512       # Preset Browser: 64
THUMBNAIL_PIXEL_VARIANCE = 0.02   # Preset Browser: 0.01
THUMBNAIL_HDR = "Annecy"          # RenderMan factory environment map lighting the thumbnails
THUMBNAIL_HDR_ROTATE_Y = 90.0     # degrees, around the world Y axis (as Maya's rotateY)


def thumbnail_hdris():
    """
    HDRIs available for thumbnails, as {name: asset.json path}, found under
    RMANTREE whatever the RenderMan version:
      - the Preset Browser preview light rigs (rmanAssetResources/assets)
      - the factory library environment maps (RenderManAssetLibrary/EnvironmentMaps)
    """
    rmantree = os.environ.get("RMANTREE")
    found = {}
    if not rmantree:
        return found
    roots = [os.path.join(rmantree, "lib", "rmanAssetResources", "assets"),
             os.path.join(rmantree, "lib", "RenderManAssetLibrary", "EnvironmentMaps")]
    for root in roots:
        for folder, dirs, files in os.walk(root):
            if folder.endswith(".rma") and "asset.json" in files:
                try:
                    with open(os.path.join(folder, "asset.json"), "r") as f:
                        if "envMap" not in json.load(f)["RenderManAsset"]["asset"]:
                            continue
                except Exception:
                    continue
                name = os.path.basename(folder)[:-4]
                found.setdefault(name, os.path.join(folder, "asset.json"))
                dirs[:] = []
    return found


def _ocio_display_args(asset):
    """
    rmanoiiotool argument sets converting the render (rendering space) for display,
    with the OCIO config prman rendered with: Maya's display/view first, then the
    config's default display/view.
    """
    from maya import cmds
    config = (getattr(asset, "ocio", None) or {}).get("path") or os.environ.get("OCIO")
    if not config or not os.path.isfile(str(config)):
        return []
    display = view = ""
    try:
        display = cmds.colorManagementPrefs(query=True, displayName=True) or ""
        view = cmds.colorManagementPrefs(query=True, viewName=True) or ""
    except Exception:
        pass
    base = ["--colorconfig", str(config), "--iscolorspace", "rendering"]
    sets = []
    if display and view:
        sets.append(base + ["--ociodisplay", display, view])
    sets.append(base + ["--ociodisplay", "", ""])          # config defaults
    return sets


def _rotate_light_rib(rib, degrees):
    """Rotates the dome light around the world Y axis (Maya rotateY convention)."""
    if not degrees:
        return rib
    c, si = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    ry = [[c, 0, -si, 0], [0, 1, 0, 0], [si, 0, c, 0], [0, 0, 0, 1]]   # row-vector matrix, as RIB

    def concat(match):
        v = [float(x) for x in match.group(1).split()]
        m = [v[i * 4:i * 4 + 4] for i in range(4)]
        out = [[sum(m[i][k] * ry[k][j] for k in range(4)) for j in range(4)] for i in range(4)]
        return "Transform [ %s ]" % " ".join("%.6g" % (0.0 if abs(x) < 1e-9 else x) for row in out for x in row)
    new, n = re.subn(r"Transform\s*\[\s*((?:[-+0-9.eE]+\s+){15}[-+0-9.eE]+)\s*\]", concat, rib, count=1)
    if n:
        return new
    return re.sub(r'(\n\s*)(Light\s+"PxrDomeLight")', r'\1Rotate %g 0 1 0\1\2' % degrees, rib, count=1)


def render_library_thumbnail(entry):
    """
    Renders a MyLibrary preset in the RenderMan Preset Browser preview scene
    (same scene, lighting and renderer), at THUMBNAIL_SIZE px with more samples,
    converted to Maya's display/view transform, and saves it next to the .mel
    as <preset>.png. Needs RenderMan for Maya. Returns the image path.
    """
    import shutil
    import subprocess
    import tempfile
    import multiprocessing
    from maya import cmds
    _ensure_renderman()
    import rfm2.ui.assets_browser as rab
    import rman_utils.rman_assets.lib as ral
    from rman_utils.rman_assets.core import RmanAsset, FilePath
    from rman_utils.rman_assets.common.external_files import Storage
    from rman_utils.rman_assets.common.utils import app, startup_info

    # RenderMan's install locations come from its own environment variables,
    # so the thumbnails follow whatever RenderMan version is loaded.
    rmantree = os.environ.get("RMANTREE")
    if not rmantree or not os.path.isdir(rmantree):
        raise RuntimeError("RMANTREE is not set: is RenderMan for Maya loaded?")
    rmanbin = os.path.join(rmantree, "bin")
    tmpdir = tempfile.mkdtemp(prefix="pb_thumb_")
    undo_state = cmds.undoInfo(query=True, stateWithoutFlush=True)
    cmds.undoInfo(stateWithoutFlush=False)          # temporary nodes stay out of the undo queue
    shader = sg = None
    try:
        # 1. the material, exported like the Preset Browser does
        shader = cmds.shadingNode("PxrSurface", asShader=True, name="pbThumb_PxrSurface")
        sg = cmds.sets(renderable=True, noSurfaceShader=True, empty=True, name="pbThumb_SG")
        cmds.connectAttr(shader + ".outColor", sg + ".surfaceShader", force=True)
        _apply_preset([shader], entry, [])
        asset = RmanAsset(assetType="nodeGraph", label=entry["name"], previewType="std",
                          storage=Storage(Storage.k_asset, asset_path=tmpdir))
        try:
            import rfm2.ui.maya_color_manager as mcm
            mgr = mcm.color_manager()
            asset.ocio = {"config": mgr.cfg_name, "path": mgr.config_file_path(),
                          "rules": mgr.conversion_rules, "aliases": mgr.aliases}
        except Exception:
            pass
        rab.parse_node_graph([sg], asset)
        matdir = FilePath(tmpdir)
        asset.save(matdir.join("asset.json"))
        # Maya hands colors over as tuples, which RenderMan's RIB writer silently turns
        # into empty arrays ("color diffuseColor" []). Reloading the saved json gives
        # plain lists, as when the Preset Browser imports an asset.
        asset = RmanAsset()
        asset.load(matdir.join("asset.json"))
    finally:
        for node in (sg, shader):
            if node and cmds.objExists(node):
                cmds.delete(node)
        cmds.undoInfo(stateWithoutFlush=undo_state)

    progress = rab.MayaProgress()
    try:
        # 2. the Preset Browser preview scene, in its own folder
        scene = FilePath(os.path.join(tmpdir, "scene"))
        shutil.copytree(ral.ribPath(None).os_path(), scene.os_path())
        # the scene file name carries a version (materialSample_v1.1.rib in RenderMan 27)
        candidates = sorted(f for f in os.listdir(scene.os_path())
                            if f.startswith("materialSample") and f.endswith(".rib"))
        if not candidates:
            raise RuntimeError("Preview scene not found in %s" % scene.os_path())
        ribfile = scene.join(candidates[-1])
        ral._writeMaterialRIB(asset, matdir)
        ral._writeConfigRIB(scene, FilePath(rmantree), FilePath(os.environ.get("RMSTREE", "")), matdir, "std")
        ral._writeDisplayChannelRIB(asset, ribfile)
        ral._writeDisplayFilterRIB(asset, scene)
        # light rig: a RenderMan factory environment map, written as the Preset Browser
        # writes its own preview rigs, then rotated
        hdris = thumbnail_hdris()
        if THUMBNAIL_HDR in hdris:
            rig = RmanAsset()
            rig.load(FilePath(hdris[THUMBNAIL_HDR]))
            light_rib = rig.getRIB()
        else:
            print("[PB] HDRI '%s' not found, using the Preset Browser's." % THUMBNAIL_HDR)
            ral._writeLightRIB(asset, scene, hdr=ral.previewHdrAsset(ral.getPreviewHdrIdx()))
            with open(scene.join("m_lightrig_envMap.rib").os_path(), "r") as f:
                light_rib = f.read()
        with open(scene.join("m_lightrig_envMap.rib").os_path(), "w") as f:
            f.write(_rotate_light_rib(light_rib, THUMBNAIL_HDR_ROTATE_Y))

        # 3. higher resolution and more samples than the Preset Browser
        with open(ribfile.os_path(), "r") as f:
            rib = f.read()
        rib = re.sub(r'("int\[2\] FormatResolution"\s*)\[\s*\d+\s+\d+\s*\]',
                     r'\g<1>[%d %d]' % (THUMBNAIL_SIZE, THUMBNAIL_SIZE), rib)
        rib = re.sub(r'("int maxsamples"\s*)\[\s*\d+\s*\]', r'\g<1>[%d]' % THUMBNAIL_MAX_SAMPLES, rib)
        rib = re.sub(r'("int minsamples"\s*)\[\s*\d+\s*\]', r'\g<1>[%d]' % THUMBNAIL_MIN_SAMPLES, rib)
        rib = re.sub(r'("float PixelVariance"\s*)\[[^\]]*\]', r'\g<1>[%g]' % THUMBNAIL_PIXEL_VARIANCE, rib)
        with open(ribfile.os_path(), "w") as f:
            f.write(rib)

        # 4. render
        progress.Start()
        progress.Update(0, "Rendering thumbnail: %s" % entry["name"])
        ncpu = max(1, multiprocessing.cpu_count() - 1)
        cmd = [os.path.join(rmanbin, app("prman")), "-t:%d" % ncpu, "-cwd", scene.os_path(), ribfile.os_path()]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startup_info())
        tif = os.path.join(tmpdir, "asset_100.tif")
        if not os.path.isfile(tif):
            raise RuntimeError("prman did not produce an image:\n%s"
                               % proc.stderr.decode(errors="replace")[-1500:])

        # 5. tif (rendering space) -> png (Maya's display/view transform)
        target = os.path.join(os.path.dirname(entry["_path"]), entry["_preset"] + ".png")
        oiio = os.path.join(rmanbin, app("rmanoiiotool"))
        attempts = [[oiio, tif] + args + ["-d", "uint8", "-o", target] for args in _ocio_display_args(asset)]
        n_ocio = len(attempts)
        attempts.append([oiio, tif, "--colorconvert", "linear", "sRGB", "-d", "uint8", "-o", target])
        attempts.append([os.path.join(rmanbin, app("sho")), "-dspy", "png", "-dspyfile", target, tif])
        errors = []
        for conv in attempts:
            res = subprocess.run(conv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, startupinfo=startup_info())
            if res.returncode == 0 and os.path.isfile(target):
                if attempts.index(conv) >= n_ocio:
                    print("[PB] Thumbnail converted without an OCIO view transform.")
                return target
            errors.append(res.stderr.decode(errors="replace").strip())
        raise RuntimeError("Could not convert the render to PNG:\n" + "\n".join(errors))
    finally:
        progress.End()
        # keep the last render's working files (RIBs, asset, raw tif) for diagnosis
        try:
            last = os.path.join(_cache_path(), "thumbnail_last_render")
            shutil.rmtree(last, ignore_errors=True)
            shutil.copytree(tmpdir, last, ignore=shutil.ignore_patterns("*.zip", "*.tex", "*.tx"))
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


def save_to_library(shader, name):
    """
    Saves every numeric and string attribute of the shader as a preset file
    (Maya attribute-preset format) in the library folder. Returns the path.
    """
    from maya import cmds
    stem = preset_file_name(name)
    if not stem:
        raise ValueError("Invalid preset name.")
    lines = ['// requires maya "%s";' % cmds.about(majorVersion=True), 'startAttrPreset( "PxrSurface" );']
    for attr in cmds.listAttr(shader, settable=True, scalar=True) or []:
        if attr in _PRESET_SKIP or attr in _DG_ATTRS or "." in attr or "[" in attr:
            continue
        plug = "%s.%s" % (shader, attr)
        try:
            kind = cmds.getAttr(plug, type=True)
            if kind == "string":
                val = cmds.getAttr(plug) or ""
                lines.append('blendAttrStr "%s" "%s"; ' % (attr, val.replace("\\", "\\\\").replace('"', '\\"')))
            elif kind in _NUMERIC_TYPES:
                val = cmds.getAttr(plug)
                lines.append('blendAttr "%s" %s; ' % (attr, ("%.10g" % val) if isinstance(val, float) else int(val)))
        except Exception:
            continue
    lines.append("endAttrPreset();")
    path = os.path.join(library_dir(), stem + ".mel")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


# --------------------------------------------------------------------------- #
#  Icon and shelf button
# --------------------------------------------------------------------------- #

def icon_path(suffix=""):
    """pb_renderman icon next to this file (suffix "", "_150" or "_200")."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pb_renderman%s.png" % suffix)
    return path if os.path.isfile(path) else None


def install_shelf_button(shelf=None):
    """
    Adds a pb_renderman button with its icon to a shelf (the current one by
    default), replacing a previous pb_renderman button on that shelf.
    Maya saves the shelf when it quits.
    """
    from maya import cmds, mel
    if not shelf:
        top = mel.eval("$pb_tmp = $gShelfTopLevel")
        shelf = cmds.tabLayout(top, query=True, selectTab=True)
    for child in cmds.shelfLayout(shelf, query=True, childArray=True) or []:
        if cmds.objectTypeUI(child) == "shelfButton" and \
                cmds.shelfButton(child, query=True, label=True) == "pb_renderman":
            cmds.deleteUI(child)
    cmds.shelfButton(parent=shelf, label="pb_renderman", imageOverlayLabel="",
                     annotation="Physically Based -> PxrSurface",
                     image=icon_path() or "commandButton.png",
                     command="import pb_renderman\npb_renderman.show()", sourceType="python")
    print("[PB] Shelf button added to '%s'." % shelf)
    return shelf


# --------------------------------------------------------------------------- #
#  Qt UI
# --------------------------------------------------------------------------- #

try:
    from PySide6 import QtCore, QtGui, QtWidgets
    from shiboken6 import wrapInstance
except ImportError:
    try:
        from PySide2 import QtCore, QtGui, QtWidgets
        from shiboken2 import wrapInstance
    except ImportError:  # outside Maya
        QtCore = QtGui = QtWidgets = wrapInstance = None


def _maya_main_window():
    from maya import OpenMayaUI
    ptr = OpenMayaUI.MQtUtil.mainWindow()
    return wrapInstance(int(ptr), QtWidgets.QWidget)


def _to_display(rgb, colorspace):
    """Swatch approximation: -> 8-bit display sRGB."""
    if colorspace == "acescg":
        # ACEScg -> linear sRGB (inverse Bradford)
        m = ((1.7050510, -0.6217921, -0.0832590),
             (-0.1302564, 1.1408047, -0.0105483),
             (-0.0240033, -0.1289690, 1.1529723))
        rgb = [sum(m[i][j] * rgb[j] for j in range(3)) for i in range(3)]

    def enc(c):
        c = min(max(c, 0.0), 1.0)
        return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return QtGui.QColor(*[int(round(enc(c) * 255)) for c in rgb])


if QtWidgets is not None:

    class _Signals(QtCore.QObject):
        loaded = QtCore.Signal(object, object)       # (materials, error)
        image = QtCore.Signal(str, bytes)            # (name, data)

    class _ResetLabel(QtWidgets.QLabel):
        """Form label: clicking it resets its control to the default value."""
        def __init__(self, text, reset_fn, parent=None):
            super(_ResetLabel, self).__init__(text, parent)
            self._reset_fn = reset_fn
            self.setCursor(QtCore.Qt.PointingHandCursor)
            self.setToolTip("Click to reset to default value")

        def mousePressEvent(self, event):
            if event.button() == QtCore.Qt.LeftButton:
                self._reset_fn()
            super(_ResetLabel, self).mousePressEvent(event)

        def enterEvent(self, event):
            f = self.font(); f.setUnderline(True); self.setFont(f)
            super(_ResetLabel, self).enterEvent(event)

        def leaveEvent(self, event):
            f = self.font(); f.setUnderline(False); self.setFont(f)
            super(_ResetLabel, self).leaveEvent(event)

    def _checkbox_reset_menu(cb, default):
        """Right-click on a checkbox -> 'Reset to default value'."""
        cb.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)

        def menu(pos):
            m = QtWidgets.QMenu(cb)
            act = m.addAction("Reset to default value (%s)" % ("on" if default else "off"))
            act.triggered.connect(lambda: cb.setChecked(default))
            m.exec_(cb.mapToGlobal(pos)) if hasattr(m, "exec_") else m.exec(cb.mapToGlobal(pos))
        cb.customContextMenuRequested.connect(menu)
        cb.setToolTip((cb.toolTip() + "\n" if cb.toolTip() else "") + "Right-click: reset to default value")

    class PBWindow(QtWidgets.QDialog):
        def __init__(self, parent=None):
            super(PBWindow, self).__init__(parent)
            self.setObjectName(WINDOW_OBJECT)
            self.setWindowTitle("Physically Based → PxrSurface  v%s — %s" % (__version__, __author__))
            self.setWindowFlags(self.windowFlags() | QtCore.Qt.Window)
            self.resize(720, 520)
            icon = QtGui.QIcon()
            for suffix in ("", "_150", "_200"):
                if icon_path(suffix):
                    icon.addFile(icon_path(suffix))
            if not icon.isNull():
                self.setWindowIcon(icon)
            self.materials = []
            self._img_cache = {}
            self.sig = _Signals()
            self.sig.loaded.connect(self._on_loaded)
            self.sig.image.connect(self._on_image)
            _cache_path()  # resolved on the main thread
            self._build()
            self.pb_materials = []
            self._rebuild()          # library presets show at once, even offline
            self.reload(False)

        # -- UI --
        def _build(self):
            lay = QtWidgets.QHBoxLayout(self)

            left = QtWidgets.QVBoxLayout()
            row = QtWidgets.QHBoxLayout()
            self.search = QtWidgets.QLineEdit()
            self.search.setPlaceholderText("Search (name, tag)…")
            self.search.textChanged.connect(self._filter)
            self.cat = QtWidgets.QComboBox()
            self.cat.currentIndexChanged.connect(self._filter)
            row.addWidget(self.search, 2)
            row.addWidget(self.cat, 1)
            left.addLayout(row)
            self.list = QtWidgets.QListWidget()
            self.list.currentItemChanged.connect(self._on_select)
            left.addWidget(self.list)
            row2 = QtWidgets.QHBoxLayout()
            b_ref = QtWidgets.QPushButton("Refresh")
            b_ref.clicked.connect(lambda: self.reload(True))
            b_ref.setToolTip("Download the database again and rescan MyLibrary")
            row2.addWidget(b_ref)
            left.addLayout(row2)
            lay.addLayout(left, 1)

            right = QtWidgets.QVBoxLayout()
            self.preview = QtWidgets.QLabel()
            self.preview.setFixedSize(256, 256)
            self.preview.setAlignment(QtCore.Qt.AlignCenter)
            self.preview.setStyleSheet("background:#222;border:1px solid #444;")
            right.addWidget(self.preview)
            self.swatch = QtWidgets.QLabel()
            self.swatch.setFixedHeight(24)
            right.addWidget(self.swatch)
            self.info = QtWidgets.QTextBrowser()
            self.info.setOpenExternalLinks(True)
            right.addWidget(self.info, 1)

            form = QtWidgets.QFormLayout()
            self.cs = QtWidgets.QComboBox()
            self.cs.addItems(["acescg", "srgb-linear"])
            detected = detect_colorspace()
            self.cs.setCurrentText(detected or "acescg")
            self.cs.currentIndexChanged.connect(lambda *_: self._on_select(self.list.currentItem()))
            form.addRow(_ResetLabel("Color space:", lambda: self.cs.setCurrentText(detect_colorspace() or "acescg")),
                        self.cs)
            rs = maya_rendering_space() or "color management off"
            self.rs_label = QtWidgets.QLabel("Maya rendering space: <b>%s</b>%s" % (
                rs, "" if detected else
                "<br><span style='color:#e8a33d'>Not recognized: the database only provides ACEScg "
                "and linear sRGB. Set the scene to ACEScg.</span>"))
            self.rs_label.setWordWrap(True)
            form.addRow(self.rs_label)
            rrow = QtWidgets.QHBoxLayout()
            self.rough = QtWidgets.QDoubleSpinBox()
            self.rough.setDecimals(3)
            self.rough.setRange(0.0, 1.0)
            self.rough.setSingleStep(0.05)
            self.rough.setValue(DEFAULT_SPEC_ROUGHNESS)
            self.rough.setToolTip("Primary Specular roughness (default 0.45, glass 0.001)")
            self.rough_db = QtWidgets.QCheckBox("Database value if meaningful")
            self.rough_db.setChecked(True)
            self.rough_db.setToolTip("Use the physicallybased.info roughness when it is not 0 or 0.5\n"
                                     "(generic placeholders), e.g. brick 1.0, paper 0.8, pearl 0.35.\n"
                                     "Otherwise the value on the left is used (glass: 0.001).")
            self.rough_db.toggled.connect(lambda *_: self._on_select(self.list.currentItem()))
            self.rough.valueChanged.connect(lambda *_: self._on_select(self.list.currentItem()))
            rrow.addWidget(self.rough)
            rrow.addWidget(self.rough_db)
            _checkbox_reset_menu(self.rough_db, True)
            form.addRow(_ResetLabel("Specular roughness:",
                                    lambda: (self.rough_db.setChecked(True),
                                             self.rough.setValue(DEFAULT_SPEC_ROUGHNESS))), rrow)
            self.sss = QtWidgets.QDoubleSpinBox()
            self.sss.setDecimals(4)
            self.sss.setRange(0.0001, 1000)
            self.sss.setValue(DEFAULT_SSS_SCALE)
            self.sss.setToolTip("Multiplier on the database subsurface radius (mm) -> subsurfaceDmfp (default 10)")
            self.sss.valueChanged.connect(lambda *_: self._on_select(self.list.currentItem()))
            form.addRow(_ResetLabel("SSS scale:", lambda: self.sss.setValue(DEFAULT_SSS_SCALE)), self.sss)
            self.ext = QtWidgets.QDoubleSpinBox()
            self.ext.setDecimals(3)
            self.ext.setRange(0.001, 100000)
            self.ext.setValue(1.0)
            self.ext.setToolTip("Multiplier on the interior extinction of transmissive materials.\n"
                                "Database coefficients are per cm: 1 for a scene in cm, 100 in meters.\n"
                                "Raise it to exaggerate the tint on small objects.")
            self.ext.valueChanged.connect(lambda *_: self._on_select(self.list.currentItem()))
            form.addRow(_ResetLabel("Extinction scale:", lambda: self.ext.setValue(1.0)), self.ext)
            self.reduce = QtWidgets.QCheckBox("Non-directional (faster SSS / Glass Single Scatter)")
            self.reduce.setToolTip("Replace directional (forward) scattering by isotropic scattering.\n"
                                   "Similar look, much faster; works in XPU. Applies to the database\n"
                                   "and to MyLibrary presets:\n"
                                   " - Glass interior with g0 > 0 (blood, coffee, juices...): g0 = 0 with\n"
                                   "   reduced scattering sigma_s * (1 - g0), extinction and albedo recomputed.\n"
                                   " - Subsurface: Directionality = 0 (skin: instead of 0.8).")
            self.reduce.toggled.connect(lambda *_: self._on_select(self.list.currentItem()))
            _checkbox_reset_menu(self.reduce, False)
            form.addRow(self.reduce)
            self.reset_cb = QtWidgets.QCheckBox("Reset other attributes to defaults")
            self.reset_cb.setChecked(True)
            self.reset_cb.setToolTip("Apply to Selected Materials: reset every attribute not driven by the\n"
                                     "database to its default first. Connected attributes (textures) are kept.")
            _checkbox_reset_menu(self.reset_cb, True)
            form.addRow(self.reset_cb)
            right.addLayout(form)

            brow = QtWidgets.QHBoxLayout()
            self.apply_btn = QtWidgets.QPushButton("Apply to Selected Materials")
            self.apply_btn.setToolTip("Update the PxrSurface nodes that are selected,\n"
                                      "or assigned to the selected objects.")
            self.apply_btn.setMinimumHeight(34)
            self.apply_btn.clicked.connect(
                lambda: self._run(lambda *a: apply_to_selection(*a, reset=self.reset_cb.isChecked()),
                                  apply_preset_to_selection))
            self.create_btn = QtWidgets.QPushButton("Create && Assign Material")
            self.create_btn.setToolTip("Create a new PxrSurface + shading group\n"
                                       "and assign it to the selected objects / faces.")
            self.create_btn.setMinimumHeight(34)
            self.create_btn.clicked.connect(lambda: self._run(create_on_selection, create_preset_on_selection))
            brow.addWidget(self.apply_btn)
            brow.addWidget(self.create_btn)
            right.addLayout(brow)
            self.save_btn = QtWidgets.QPushButton("Save Selected to MyLibrary…")
            self.save_btn.setToolTip("Save the selected PxrSurface (or the one assigned to the selected object)\n"
                                     "as a preset in MyLibrary (" + library_dir(create=False) + ").\n"
                                     "Tip: put a .jpg or .png with the same name next to the .mel\n"
                                     "to get a preview.")
            self.save_btn.clicked.connect(lambda *_: self._save_library())
            self.delete_btn = QtWidgets.QPushButton("Delete from MyLibrary…")
            self.delete_btn.setToolTip("Move the selected MyLibrary preset to the _trash subfolder\n"
                                       "of the library (recoverable).")
            self.delete_btn.setEnabled(False)
            self.delete_btn.clicked.connect(lambda *_: self._delete_library())
            self.thumb_btn = QtWidgets.QPushButton("Render Thumbnail")
            self.thumb_btn.setToolTip("Render the selected MyLibrary preset with the RenderMan\n"
                                      "Preset Browser preview scene (256 px), saved next to the .mel.")
            self.thumb_btn.setEnabled(False)
            self.thumb_btn.clicked.connect(lambda *_: self._render_thumbnail())
            lrow = QtWidgets.QHBoxLayout()
            lrow.addWidget(self.save_btn)
            lrow.addWidget(self.thumb_btn)
            lrow.addWidget(self.delete_btn)
            right.addLayout(lrow)
            self.status = QtWidgets.QLabel("")
            right.addWidget(self.status)
            lay.addLayout(right, 1)

        # -- data --
        def reload(self, force):
            if force:
                self._rebuild()      # rescan MyLibrary at once, even if the download fails
            self.status.setText("Loading…")

            def job():
                try:
                    self.sig.loaded.emit(load_materials(force_refresh=force), None)
                except Exception as exc:
                    self.sig.loaded.emit(None, exc)
            threading.Thread(target=job, daemon=True).start()

        def _on_loaded(self, mats, err):
            if err is not None:
                self.status.setText("Database unavailable (%s). MyLibrary still works." % err)
                return
            self.pb_materials = sorted(mats, key=lambda m: m.get("name", "").lower())
            self._rebuild()

        def _rebuild(self, select=None):
            """Merges the database and the library presets, refreshes categories and list."""
            try:
                self.library = load_library()
            except Exception as exc:
                self.library = []
                print("[PB] Library unavailable: %s" % exc)
            self.materials = self.library + getattr(self, "pb_materials", [])
            cats = sorted({c for m in self.materials for c in categories(m)} - {LIBRARY_CATEGORY})
            current = self.cat.currentText()
            self.cat.blockSignals(True)
            self.cat.clear()
            self.cat.addItems(["All categories", LIBRARY_CATEGORY] + cats)
            if current and self.cat.findText(current) >= 0:
                self.cat.setCurrentText(current)
            self.cat.blockSignals(False)
            self._filter()
            self.status.setText("%d materials, %d in MyLibrary" % (len(self.materials) - len(self.library), len(self.library)))
            self.status.setToolTip("MyLibrary folder: " + library_dir())
            if select:
                for i in range(self.list.count()):
                    m = self.list.item(i).data(QtCore.Qt.UserRole)
                    if m.get("_library") and m.get("_preset") == select:
                        self.list.setCurrentRow(i)
                        break

        def _filter(self, *_):
            txt = self.search.text().lower().strip()
            cat = self.cat.currentText()
            self.list.clear()
            for m in self.materials:
                hay = (m.get("name", "") + " " + " ".join(map(str, m.get("tags") or []))).lower()
                if txt and txt not in hay:
                    continue
                if self.cat.currentIndex() > 0 and cat not in categories(m):
                    continue
                it = QtWidgets.QListWidgetItem(m.get("name", "?"))
                it.setData(QtCore.Qt.UserRole, m)
                if m.get("_library"):
                    f = it.font(); f.setBold(True); it.setFont(f)
                    it.setToolTip("MyLibrary: " + m["_path"])
                col = preset_swatch(m["_attrs"]) if m.get("_library") else pick_color(m.get("color"), self.cs.currentText())
                if col:
                    pm = QtGui.QPixmap(14, 14)
                    pm.fill(_to_display(col, self.cs.currentText()))
                    it.setIcon(QtGui.QIcon(pm))
                self.list.addItem(it)

        # -- selection / preview --
        def _on_select(self, item, *_):
            if item is None:
                return
            m = item.data(QtCore.Qt.UserRole)
            cs = self.cs.currentText()
            self.delete_btn.setEnabled(bool(m.get("_library")))
            self.thumb_btn.setEnabled(bool(m.get("_library")))
            if m.get("_library"):
                self._show_preset(m)
                return
            col = pick_color(m.get("color"), cs)
            if col:
                c = _to_display(col, cs)
                self.swatch.setStyleSheet("background:%s;border:1px solid #444;" % c.name())
            _, notes = build_pxr_values(m, cs, self.sss.value(), self._roughness(), self.ext.value(),
                                       self.reduce.isChecked(), self.rough_db.isChecked())
            rows = ["<b>%s</b> — %s" % (m.get("name"), ", ".join(categories(m)))]
            if m.get("description"):
                rows.append("<i>%s</i>" % m["description"])
            if col:
                rows.append("color (%s): %s" % (cs, ", ".join("%.3f" % x for x in col)))
            for k in ("metalness", "roughness", "ior", "transmission", "diffuseRoughness"):
                if m.get(k) not in (None, ""):
                    rows.append("%s: %s" % (k, m[k]))
            rows.append("<br>" + "<br>".join("• " + n for n in notes))
            self.info.setHtml("<br>".join(rows))
            self._load_image(m)

        def _set_preview(self, pm):
            """Shows an image 1:1, only scaled down (never stretched) to fit the 256 px box."""
            box = self.preview.width()
            if pm.width() > box or pm.height() > box:
                pm = pm.scaled(box, box, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            self.preview.setPixmap(pm)

        def _show_preset(self, m):
            a, cs = m["_attrs"], self.cs.currentText()
            col = preset_swatch(a)
            if col:
                self.swatch.setStyleSheet("background:%s;border:1px solid #444;" % _to_display(col, cs).name())

            def num(k):
                return "%.4g" % a[k] if k in a else "-"

            def rgb(k):
                c = _preset_color(a, k)
                return ", ".join("%.3g" % x for x in c) if c else "-"
            rows = ["<b>%s</b> — %s preset" % (m["name"], LIBRARY_CATEGORY),
                    "<span style='color:#888'>%s</span>" % m["_path"]]
            if a.get("pbMaterial"):
                rows.append("based on: %s" % a["pbMaterial"])
            if a.get("diffuseGain", 0) > 0:
                rows.append("Diffuse: gain %s, color %s" % (num("diffuseGain"), rgb("diffuseColor")))
            if max(_preset_color(a, "specularFaceColor") or [0]) > 0:
                mode = "physical" if a.get("specularFresnelMode") == 1 else "artistic"
                rows.append("Primary Specular: %s, IOR %s, roughness %s" % (mode, rgb("specularIor"), num("specularRoughness")))
            if a.get("refractionGain", 0) > 0:
                rows.append("Glass: refraction %s, reflection %s, IOR %s, roughness %s"
                            % (num("refractionGain"), num("reflectionGain"), num("glassIor"), num("glassRoughness")))
                if max(_preset_color(a, "extinction") or [0]) > 0:
                    rows.append("Interior: extinction %s, albedo %s, g %s%s" % (
                        rgb("extinction"), rgb("ssAlbedo"), num("g"),
                        ", Multiscattering" if a.get("multiScatter") else ""))
            if a.get("subsurfaceGain", 0) > 0:
                rows.append("Subsurface: color %s, dmfp %s, directionality %s"
                            % (rgb("subsurfaceColor"), num("subsurfaceDmfp"), num("subsurfaceDirectionality")))
            if a.get("iridescenceFaceGain", 0) > 0 or a.get("iridescenceEdgeGain", 0) > 0:
                rows.append("Iridescence: thickness %s nm" % num("iridescenceThickness"))
            nd_notes = non_directional(a)[1]
            if nd_notes and self.reduce.isChecked():
                rows.append("<br>• Applied as saved (%d attributes), except:" % len(a))
                rows.extend("&nbsp;&nbsp;– " + n for n in nd_notes)
            else:
                rows.append("<br>• Applied exactly as saved (%d attributes)." % len(a))
                if nd_notes:
                    rows.append("• Directional scattering: tick Non-directional (faster SSS / Glass Single Scatter) to render it faster.")
            rows.append("• Only Non-directional (faster SSS / Glass Single Scatter) applies to presets; the other options do not."
                        "<br>• Attributes connected to textures are kept.")
            self.info.setHtml("<br>".join(rows))
            self.preview.setPixmap(QtGui.QPixmap())
            if m.get("_image"):
                pm = QtGui.QPixmap(m["_image"])
                if not pm.isNull():
                    self._set_preview(pm)
                    return
            self.preview.setText("MyLibrary\n\nNo thumbnail yet:\nclick Render Thumbnail")

        def _render_thumbnail(self, entry=None):
            if entry is None:
                item = self.list.currentItem()
                entry = item.data(QtCore.Qt.UserRole) if item else None
            if not entry or not entry.get("_library"):
                self.status.setText("Pick a MyLibrary preset first.")
                return
            self.status.setText("Rendering thumbnail: %s…" % entry["name"])
            QtWidgets.QApplication.processEvents()
            try:
                path = render_library_thumbnail(entry)
            except Exception as exc:
                self.status.setText("Thumbnail failed: %s" % exc)
                raise
            print("[PB] Thumbnail: %s" % path)
            self._rebuild(select=entry["_preset"])
            self.status.setText("Thumbnail rendered: %s" % entry["name"])

        def _delete_library(self):
            item = self.list.currentItem()
            m = item.data(QtCore.Qt.UserRole) if item else None
            if not m or not m.get("_library"):
                self.status.setText("Pick a MyLibrary preset to delete.")
                return
            extra = " and its preview image" if m.get("_image") else ""
            answer = QtWidgets.QMessageBox.question(
                self, "Delete from MyLibrary",
                "Delete '%s'%s?\n\nThe files are moved to the _trash subfolder of\n%s\n"
                "and can be restored from there." % (m["name"], extra, library_dir()))
            if answer != QtWidgets.QMessageBox.Yes:
                return
            try:
                trash = delete_from_library(m)
            except Exception as exc:
                self.status.setText("Error: %s" % exc)
                raise
            print("[PB] Deleted %s -> %s" % (m["_path"], trash))
            self._rebuild()
            self.status.setText("Deleted from MyLibrary: %s (in _trash)" % m["name"])

        def _save_library(self):
            try:
                shader = selected_pxrsurface()
            except Exception as exc:
                self.status.setText("Error: %s" % exc)
                return
            if not shader:
                self.status.setText("Select a PxrSurface (or an object using one) to save.")
                return
            short = shader.split("|")[-1]
            guess = short.replace("_PxrSurface", "").replace("PB_", "").replace("Lib_", "")
            name, ok = QtWidgets.QInputDialog.getText(self, "Save to MyLibrary",
                                                      "Preset name for %s:" % short, text=guess)
            if not ok or not name.strip():
                return
            stem = preset_file_name(name)
            target = os.path.join(library_dir(), stem + ".mel")
            if os.path.isfile(target):
                answer = QtWidgets.QMessageBox.question(self, "Save to MyLibrary",
                                                        "'%s' already exists. Overwrite it?" % stem[len(PRESET_PREFIX):])
                if answer != QtWidgets.QMessageBox.Yes:
                    return
            try:
                path = save_to_library(shader, name)
            except Exception as exc:
                self.status.setText("Error: %s" % exc)
                raise
            print("[PB] Saved %s -> %s" % (short, path))
            self._rebuild(select=stem)
            self.status.setText("Saved to MyLibrary: " + stem[len(PRESET_PREFIX):])

        def _load_image(self, m):
            name = m.get("name", "")
            url = reference_url(m)
            self.preview.setPixmap(QtGui.QPixmap())
            if not url:
                self.preview.setText("No preview")
                return
            if name in self._img_cache:
                self._on_image(name, self._img_cache[name])
                return
            self.preview.setText("…")
            fn = os.path.join(_cache_path(), "img_" + _safe_name(name) + "_300" + os.path.splitext(url)[1][:5])

            def job():
                try:
                    if os.path.isfile(fn):
                        data = open(fn, "rb").read()
                    else:
                        data = _http_get(url)
                        open(fn, "wb").write(data)
                    self.sig.image.emit(name, data)
                except Exception:
                    self.sig.image.emit(name, b"")
            threading.Thread(target=job, daemon=True).start()

        def _on_image(self, name, data):
            cur = self.list.currentItem()
            if not cur or cur.text() != name:
                return
            if not data:
                self.preview.setText("Preview unavailable")
                return
            self._img_cache[name] = data
            pm = QtGui.QPixmap()
            pm.loadFromData(data)
            self._set_preview(pm)

        def _roughness(self):
            return self.rough.value()

        # -- actions --
        def _run(self, func, preset_func=None):
            item = self.list.currentItem()
            if item is None:
                self.status.setText("Pick a material first.")
                return
            m = item.data(QtCore.Qt.UserRole)
            if m.get("_library"):
                try:
                    shaders, _ = preset_func(m, self.reduce.isChecked())
                    self.status.setText("OK -> " + ", ".join(s.split("|")[-1] for s in shaders))
                except Exception as exc:
                    self.status.setText("Error: %s" % exc)
                    raise
                return
            try:
                shaders, _ = func(m, self.cs.currentText(),
                                  self.sss.value(), self._roughness(), self.ext.value(),
                                  self.reduce.isChecked(), self.rough_db.isChecked())
                self.status.setText("OK -> " + ", ".join(s.split("|")[-1] for s in shaders))
            except Exception as exc:
                self.status.setText("Error: %s" % exc)
                raise


_window = None


def show():
    """Opens (or reopens) the window."""
    global _window
    try:
        _window.close()
        _window.deleteLater()
    except Exception:
        pass
    _window = PBWindow(_maya_main_window())
    _window.show()
    return _window


def apply_by_name(name, create=False, **kw):
    """Script usage: pb_renderman.apply_by_name('Gold', create=True, colorspace='acescg')"""
    mats = load_materials()
    m = next((x for x in mats if x.get("name", "").lower() == name.lower()), None)
    if m is None:
        raise ValueError("Material not found: %s" % name)
    return (create_on_selection if create else apply_to_selection)(m, **kw)
