bl_info = {
    "name": "Halo Wars UGX Pipeline Pro",
    "author": "CutesyThrower12",
    "version": (6, 9, 0),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > UGX Pipeline; File > Import/Export",
    "description": "Halo Wars 2 UGX/UAX pipeline for Blender 5. Uses ugx.exe for UGX model conversion, restores the proven sampled DAE -> GR2 -> UAX animation export workflow, keeps the v6.5 manual-DAE orientation patch, includes custom UAX import with scale-shear support, and restores the UAX animation helper sidebar.",
    "warning": "Requires ugx.exe for UGX/glTF conversion. Legacy UAX export can use bundled tool/DAEtoGR2.exe and tool/gr2ugx.exe.",
    "category": "Import-Export",
}

import base64
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Iterable, Optional

import bpy
import mathutils
from mathutils import Matrix, Quaternion, Vector
from bpy_extras.io_utils import ImportHelper, ExportHelper
try:
    from bpy_extras import anim_utils
except Exception:
    anim_utils = None
from bpy.props import BoolProperty, StringProperty, EnumProperty, PointerProperty, FloatProperty, FloatVectorProperty, IntProperty

ADDON_ID = __name__

HW2_IMPORT_SCALE = 0.634920635
HW2_IMPORT_GROUND_Z = 0.0

# v6.9 release metadata: author set to CutesyThrower12 and README/repo packaging added.


# -----------------------------------------------------------------------------
# Preferences / settings
# -----------------------------------------------------------------------------

class HWUGX_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = ADDON_ID

    ugx_exe_path: StringProperty(
        name="ugx.exe Path",
        description="Path to the external Halo Wars UGX converter executable",
        subtype="FILE_PATH",
        default="C:\\Users\\Admin\\Desktop\\ensemble-formats\\target\\release\\ugx.exe",
    )

    keep_temp_files: BoolProperty(
        name="Keep Temporary glTF Files",
        description="Keep the temporary glTF folder after import/export for debugging",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="Converter", icon="MODIFIER")
        box.prop(self, "ugx_exe_path")
        box.prop(self, "keep_temp_files")
        box.separator()
        box.label(text="UGX uses ugx.exe. UAX animation import is handled directly by this add-on.", icon="INFO")


def get_prefs(context=None) -> Optional[HWUGX_AddonPreferences]:
    context = context or bpy.context
    addon = context.preferences.addons.get(ADDON_ID)
    if addon:
        return addon.preferences
    # When installed as __init__.py inside a package, Blender may key preferences by package name.
    package = __package__
    if package:
        addon = context.preferences.addons.get(package)
        if addon:
            return addon.preferences
    return None


def get_ugx_exe_path(context=None) -> str:
    prefs = get_prefs(context)
    return bpy.path.abspath(prefs.ugx_exe_path) if prefs and prefs.ugx_exe_path else ""


def clean_temp_dir(path: str, context=None):
    prefs = get_prefs(context)
    if prefs and prefs.keep_temp_files:
        return
    shutil.rmtree(path, ignore_errors=True)


def report_exception(operator, message: str, exc: Exception):
    operator.report({"ERROR"}, f"{message}: {exc}")


# -----------------------------------------------------------------------------
# External converter helpers
# -----------------------------------------------------------------------------

def run_ugx_command(args: list[str], operator=None) -> bool:
    try:
        result = subprocess.run(
            args,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
        )
        output = (result.stdout or result.stderr or "ugx.exe finished").strip()
        if operator and output:
            operator.report({"INFO"}, output[:900])
        return True
    except FileNotFoundError:
        if operator:
            operator.report({"ERROR"}, "ugx.exe was not found. Set the converter path in Add-on Preferences.")
        return False
    except subprocess.CalledProcessError as exc:
        if operator:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            operator.report({"ERROR"}, f"ugx.exe failed: {detail[:900]}")
        return False


def ensure_converter(operator, context) -> Optional[str]:
    ugx_exe = get_ugx_exe_path(context)
    if not ugx_exe or not os.path.isfile(ugx_exe):
        operator.report({"ERROR"}, "Invalid ugx.exe path. Set it in Edit > Preferences > Add-ons > Halo Wars UGX Pipeline Pro.")
        return None
    return ugx_exe


def _call_gltf_export_with_filtered_kwargs(kwargs: dict):
    """Call glTF export while pruning unsupported keyword args across Blender versions."""
    pending = dict(kwargs)
    while True:
        try:
            return bpy.ops.export_scene.gltf(**pending)
        except TypeError as exc:
            text = str(exc)
            removed = False
            for key in list(pending.keys()):
                if key in text:
                    pending.pop(key, None)
                    removed = True
                    break
            if not removed:
                # Last resort: remove known optional/newer keys one by one.
                for key in ("export_animation_mode", "export_anim_single_armature", "export_nla_strips", "export_force_sampling", "export_frame_range", "export_frame_step", "export_optimize_animation_size", "export_negative_frame", "export_vertex_color", "export_attributes", "export_yup"):
                    if key in pending:
                        pending.pop(key, None)
                        removed = True
                        break
            if not removed:
                raise


def call_gltf_export(filepath: str, *, use_selection: bool, export_animations: bool, export_all_actions: bool = True, axis_up: str = "Y+", operator=None, extra_kwargs: Optional[dict] = None):
    """Call Blender's glTF exporter with Blender-version-tolerant arguments.

    Blender 5's glTF animation API is more mode-driven than the old exporter.
    UAX action export passes explicit NLA/active-action options here so each
    action is baked/exported as its own unique animation instead of repeatedly
    exporting the same empty/default clip.
    """
    kwargs = dict(
        filepath=filepath,
        export_format="GLTF_SEPARATE",
        use_selection=use_selection,
        export_apply=True,
        export_texcoords=True,
        export_normals=True,
        export_materials="EXPORT",
        export_extras=True,
        export_animations=export_animations,
        export_yup=(axis_up == "Y+"),
        export_vertex_color="ACTIVE",
    )
    if export_animations and export_all_actions:
        # Blender 5 enum. Older Blender builds will prune this if unsupported.
        kwargs["export_animation_mode"] = "ACTIONS"
        kwargs["export_anim_single_armature"] = False
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    return _call_gltf_export_with_filtered_kwargs(kwargs)


# -----------------------------------------------------------------------------
# Shared binary helpers from Halo Wars Suite animation workflow
# -----------------------------------------------------------------------------

def read_c_string(data: bytes, offset: int) -> str:
    if offset < 0 or offset >= len(data):
        return ""
    end = data.find(b"\0", offset)
    if end == -1:
        end = len(data)
    return data[offset:end].decode("utf-8", errors="replace")


def load_ecf_chunks(filepath: str) -> dict[int, list[bytes]]:
    with open(filepath, "rb") as f:
        data = f.read()

    if len(data) < 32:
        raise ValueError("File is too small to be a valid ECF/UAX container.")
    magic = struct.unpack(">I", data[:4])[0]
    if magic != 0xDABA7737:
        raise ValueError("Invalid ECF/UAX magic. Expected 0xDABA7737.")

    num_chunks = struct.unpack(">H", data[16:18])[0]
    chunks: dict[int, list[bytes]] = {}
    ecf_header_size = 32
    chunk_header_size = 24

    for i in range(num_chunks):
        header = data[ecf_header_size + chunk_header_size * i: ecf_header_size + chunk_header_size * i + 16]
        if len(header) < 16:
            continue
        chunk_id, chunk_offs, chunk_len = struct.unpack(">QII", header)
        if chunk_offs + chunk_len > len(data):
            continue
        chunks.setdefault(chunk_id, []).append(data[chunk_offs: chunk_offs + chunk_len])
    return chunks


def _safe_sqrt(value: float) -> float:
    return math.sqrt(max(0.0, value))


def get_pos_keys_from_granny(granny: bytes, key_type: str, object_offs: int) -> list[tuple[float, Vector]]:
    frames: list[tuple[float, Vector]] = []
    try:
        if key_type == "CurveDataHeader_DaK32fC32f":
            _header, knot_count, knot_offset, _null1, control_count, control_offset, _null2 = struct.unpack(
                "<IIIIIII", granny[object_offs: object_offs + 28]
            )
            count = min(knot_count, control_count)
            for k in range(count):
                knot = struct.unpack("<f", granny[knot_offset + k * 4: knot_offset + k * 4 + 4])[0]
                kx, ky, kz = struct.unpack("<fff", granny[control_offset + k * 12: control_offset + k * 12 + 12])
                frames.append((knot, Vector((kx, ky, kz))))

        elif key_type in {"CurveDataHeader_DaIdentity", "CurveDataHeader_D3Constant32f"}:
            return frames

        elif key_type == "CurveDataHeader_D3K16uC16u":
            curve_data_header, one_over_knot_scale_trunc = struct.unpack("<HH", granny[object_offs: object_offs + 4])
            control_scales = struct.unpack("<fff", granny[object_offs + 4: object_offs + 16])
            control_offsets = struct.unpack("<fff", granny[object_offs + 16: object_offs + 28])
            knot_control_count, knot_control_offset = struct.unpack("<II", granny[object_offs + 28: object_offs + 36])
            knot_count = int(knot_control_count / 4)
            shifted = struct.unpack("<f", struct.pack("<I", one_over_knot_scale_trunc << 16))[0]
            if shifted == 0:
                return frames
            for k in range(knot_count):
                knot_data = struct.unpack("<H", granny[knot_control_offset + k * 2: knot_control_offset + k * 2 + 2])[0]
                knot = knot_data / shifted
                val = struct.unpack("<HHH", granny[knot_control_offset + knot_count * 2 + k * 6: knot_control_offset + knot_count * 2 + k * 6 + 6])
                frames.append((knot, Vector(tuple(val[i] * control_scales[i] + control_offsets[i] for i in range(3)))))

        elif key_type == "CurveDataHeader_D3I1K16uC16u":
            _header, one_over_knot_scale_trunc = struct.unpack("<HH", granny[object_offs: object_offs + 4])
            control_scales = struct.unpack("<fff", granny[object_offs + 4: object_offs + 16])
            control_offsets = struct.unpack("<fff", granny[object_offs + 16: object_offs + 28])
            knot_control_count, knot_control_offset = struct.unpack("<II", granny[object_offs + 28: object_offs + 36])
            knot_count = int(knot_control_count / 2)
            shifted = struct.unpack("<f", struct.pack("<I", one_over_knot_scale_trunc << 16))[0]
            if shifted == 0:
                return frames
            for k in range(knot_count):
                knot_data = struct.unpack("<H", granny[knot_control_offset + k * 2: knot_control_offset + k * 2 + 2])[0]
                knot = knot_data / shifted
                val = struct.unpack("<H", granny[knot_control_offset + knot_count * 2 + k * 2: knot_control_offset + knot_count * 2 + k * 2 + 2])[0]
                frames.append((knot, Vector(tuple(val * control_scales[i] + control_offsets[i] for i in range(3)))))

        elif key_type == "CurveDataHeader_D3K8uC8u":
            _header, one_over_knot_scale_trunc = struct.unpack("<HH", granny[object_offs: object_offs + 4])
            control_scales = struct.unpack("<fff", granny[object_offs + 4: object_offs + 16])
            control_offsets = struct.unpack("<fff", granny[object_offs + 16: object_offs + 28])
            knot_control_count, knot_control_offset = struct.unpack("<II", granny[object_offs + 28: object_offs + 36])
            knot_count = int(knot_control_count / 4)
            shifted = struct.unpack("<f", struct.pack("<I", one_over_knot_scale_trunc << 16))[0]
            if shifted == 0:
                return frames
            for k in range(knot_count):
                knot_data = struct.unpack("<B", granny[knot_control_offset + k: knot_control_offset + k + 1])[0]
                knot = knot_data / shifted
                val = struct.unpack("<BBB", granny[knot_control_offset + knot_count + k * 3: knot_control_offset + knot_count + k * 3 + 3])
                frames.append((knot, Vector(tuple(val[i] * control_scales[i] + control_offsets[i] for i in range(3)))))

        elif key_type == "CurveDataHeader_D3I1K8uC8u":
            _header, one_over_knot_scale_trunc = struct.unpack("<HH", granny[object_offs: object_offs + 4])
            control_scales = struct.unpack("<fff", granny[object_offs + 4: object_offs + 16])
            control_offsets = struct.unpack("<fff", granny[object_offs + 16: object_offs + 28])
            knot_control_count, knot_control_offset = struct.unpack("<II", granny[object_offs + 28: object_offs + 36])
            knot_count = int(knot_control_count / 4)
            shifted = struct.unpack("<f", struct.pack("<I", one_over_knot_scale_trunc << 16))[0]
            if shifted == 0:
                return frames
            for k in range(knot_count):
                knot_data = struct.unpack("<B", granny[knot_control_offset + k: knot_control_offset + k + 1])[0]
                knot = knot_data / shifted
                val = struct.unpack("<B", granny[knot_control_offset + knot_count + k: knot_control_offset + knot_count + k + 1])[0]
                frames.append((knot, Vector(tuple(val * control_scales[i] + control_offsets[i] for i in range(3)))))
        else:
            print(f"[UGX Pipeline] Unknown position curve format: {key_type}")
    except Exception as exc:
        print(f"[UGX Pipeline] Failed to read position keys ({key_type}): {exc}")
    return frames


def get_rot_keys_from_granny(granny: bytes, key_type: str, object_offs: int) -> list[tuple[float, Quaternion]]:
    frames: list[tuple[float, Quaternion]] = []
    scale_table = (
        1.4142135, 0.70710677, 0.35355338, 0.35355338,
        0.35355338, 0.17677669, 0.17677669, 0.17677669,
        -1.4142135, -0.70710677, -0.35355338, -0.35355338,
        -0.35355338, -0.17677669, -0.17677669, -0.17677669,
    )
    offset_table = (
        -0.70710677, -0.35355338, -0.53033006, -0.17677669,
        0.17677669, -0.17677669, -0.088388346, 0.0,
        0.70710677, 0.35355338, 0.53033006, 0.17677669,
        -0.17677669, 0.17677669, 0.088388346, -0.0,
    )
    try:
        if key_type == "CurveDataHeader_DaK32fC32f":
            _header, knot_count, knot_offset, control_count, control_offset = struct.unpack(
                "<IIQIQ", granny[object_offs: object_offs + 28]
            )
            count = min(knot_count, control_count)
            for k in range(count):
                knot = struct.unpack("<f", granny[knot_offset + k * 4: knot_offset + k * 4 + 4])[0]
                kx, ky, kz, kw = struct.unpack("<ffff", granny[control_offset + k * 16: control_offset + k * 16 + 16])
                frames.append((knot, Quaternion((kw, kx, ky, kz))))

        elif key_type == "CurveDataHeader_D4Constant32f":
            _header, _padding, kx, ky, kz, kw = struct.unpack("<HHffff", granny[object_offs: object_offs + 20])
            frames.append((0.0, Quaternion((kw, kx, ky, kz))))

        elif key_type == "CurveDataHeader_DaIdentity":
            frames.append((0.0, Quaternion((1.0, 0.0, 0.0, 0.0))))

        elif key_type == "CurveDataHeader_D4nK8uC7u":
            _header, entries, one_over_knot_scale, knot_control_count, knots_control_offset = struct.unpack(
                "<HHfIQ", granny[object_offs: object_offs + 20]
            )
            knot_count = int(knot_control_count / 4)
            if one_over_knot_scale == 0:
                return frames
            scales = tuple(scale_table[(entries >> shift) & 0x0F] * 0.0078740157 for shift in (0, 4, 8, 12))
            offsets = tuple(offset_table[(entries >> shift) & 0x0F] for shift in (0, 4, 8, 12))
            for k in range(knot_count):
                knot_data = struct.unpack("<B", granny[knots_control_offset + k: knots_control_offset + k + 1])[0]
                knot = knot_data / one_over_knot_scale
                a, b, c = struct.unpack("<BBB", granny[knots_control_offset + knot_count + k * 3: knots_control_offset + knot_count + k * 3 + 3])
                sw1 = (((b & 0x80) >> 6) | ((c & 0x80) >> 7))
                sw2, sw3, sw4 = ((sw1 + 1) & 3), ((sw1 + 2) & 3), ((sw1 + 3) & 3)
                data_a = (a & 0x7F) * scales[sw2] + offsets[sw2]
                data_b = (b & 0x7F) * scales[sw3] + offsets[sw3]
                data_c = (c & 0x7F) * scales[sw4] + offsets[sw4]
                data_d = _safe_sqrt(1.0 - (data_a * data_a + data_b * data_b + data_c * data_c))
                if a & 0x80:
                    data_d = -data_d
                qv = [0.0, 0.0, 0.0, 0.0]
                qv[sw2], qv[sw3], qv[sw4], qv[sw1] = data_a, data_b, data_c, data_d
                frames.append((knot, Quaternion((qv[3], qv[0], qv[1], qv[2]))))

        elif key_type == "CurveDataHeader_D4nK16uC15u":
            _header, entries, one_over_knot_scale, knot_control_count, knots_control_offset = struct.unpack(
                "<HHfIQ", granny[object_offs: object_offs + 20]
            )
            knot_count = int(knot_control_count / 4)
            if one_over_knot_scale == 0:
                return frames
            scales = tuple(scale_table[(entries >> shift) & 0x0F] * 0.000030518509 for shift in (0, 4, 8, 12))
            offsets = tuple(offset_table[(entries >> shift) & 0x0F] for shift in (0, 4, 8, 12))
            for k in range(knot_count):
                knot_data = struct.unpack("<H", granny[knots_control_offset + k * 2: knots_control_offset + k * 2 + 2])[0]
                knot = knot_data / one_over_knot_scale
                a, b, c = struct.unpack("<HHH", granny[knots_control_offset + knot_count * 2 + k * 6: knots_control_offset + knot_count * 2 + k * 6 + 6])
                sw1 = ((b & 0x8000) >> 14) | (c >> 15)
                sw2, sw3, sw4 = ((sw1 + 1) & 3), ((sw1 + 2) & 3), ((sw1 + 3) & 3)
                data_a = (a & 0x7FFF) * scales[sw2] + offsets[sw2]
                data_b = (b & 0x7FFF) * scales[sw3] + offsets[sw3]
                data_c = (c & 0x7FFF) * scales[sw4] + offsets[sw4]
                data_d = _safe_sqrt(1.0 - (data_a * data_a + data_b * data_b + data_c * data_c))
                if a & 0x8000:
                    data_d = -data_d
                qv = [0.0, 0.0, 0.0, 0.0]
                qv[sw2], qv[sw3], qv[sw4], qv[sw1] = data_a, data_b, data_c, data_d
                frames.append((knot, Quaternion((qv[3], qv[0], qv[1], qv[2]))))
        else:
            print(f"[UGX Pipeline] Unknown rotation curve format: {key_type}")
    except Exception as exc:
        print(f"[UGX Pipeline] Failed to read rotation keys ({key_type}): {exc}")
    return frames


# -----------------------------------------------------------------------------
# UAX scale-shear import helpers (v6.8)
# -----------------------------------------------------------------------------

def _hwugx_scale_from_scale_shear_values(values):
    """Extract Blender-friendly XYZ scale from Granny's 3x3 scale-shear data.

    HW2 UAX scale curves usually store a 3x3 float matrix per key. For the
    custom structure/VFX clips generated by the legacy toolchain this is normally
    diagonal, but using column lengths keeps the import usable if a converter
    emits small off-diagonal shear/orientation terms.
    """
    vals = list(values)
    if len(vals) >= 9:
        m00, m01, m02, m10, m11, m12, m20, m21, m22 = vals[:9]
        off_diag = abs(m01) + abs(m02) + abs(m10) + abs(m12) + abs(m20) + abs(m21)
        if off_diag < 1.0e-5:
            return Vector((m00, m11, m22))
        sx = math.sqrt(max(0.0, m00*m00 + m10*m10 + m20*m20))
        sy = math.sqrt(max(0.0, m01*m01 + m11*m11 + m21*m21))
        sz = math.sqrt(max(0.0, m02*m02 + m12*m12 + m22*m22))
        return Vector((sx, sy, sz))
    if len(vals) >= 3:
        return Vector((vals[0], vals[1], vals[2]))
    if len(vals) == 1:
        return Vector((vals[0], vals[0], vals[0]))
    return Vector((1.0, 1.0, 1.0))


def _hwugx_safe_relative_scale(value: Vector, basis: Vector) -> Vector:
    def div(v, b):
        if abs(b) < 1.0e-8:
            return 1.0
        return v / b
    return Vector((div(value.x, basis.x), div(value.y, basis.y), div(value.z, basis.z)))


def get_scale_shear_keys_from_granny(granny: bytes, key_type: str, object_offs: int) -> list[tuple[float, Vector]]:
    """Read Granny scale-shear curves and return (seconds, XYZ scale) keys.

    The old Blender importer created scale FCurves but never filled them. That
    made imported custom scale-only UAX clips appear static in new Blender files.
    v6.8 implements the missing parser for the curve format produced by the
    working DAEtoGR2 -> gr2ugx legacy path: CurveDataHeader_DaK32fC32f with
    9 float controls per key.
    """
    frames: list[tuple[float, Vector]] = []
    try:
        if key_type == "CurveDataHeader_DaK32fC32f":
            _header, knot_count, knot_offset, control_count, control_offset = struct.unpack(
                "<IIQIQ", granny[object_offs: object_offs + 28]
            )
            if knot_count <= 0:
                return frames
            # Granny stores total float controls. Scale-shear is normally 9 floats/key.
            dim = 9 if control_count >= knot_count * 9 else (3 if control_count >= knot_count * 3 else 1)
            count = min(knot_count, int(control_count // max(dim, 1)))
            for k in range(count):
                knot = struct.unpack("<f", granny[knot_offset + k * 4: knot_offset + k * 4 + 4])[0]
                vals = struct.unpack("<" + "f" * dim, granny[control_offset + k * dim * 4: control_offset + (k + 1) * dim * 4])
                frames.append((knot, _hwugx_scale_from_scale_shear_values(vals)))

        elif key_type == "CurveDataHeader_DaIdentity":
            frames.append((0.0, Vector((1.0, 1.0, 1.0))))

        elif key_type in {"CurveDataHeader_D3Constant32f", "CurveDataHeader_D9Constant32f"}:
            # Rare fallback: constant 3-vector or 3x3 matrix.
            try:
                if key_type == "CurveDataHeader_D3Constant32f":
                    _header, _padding, sx, sy, sz = struct.unpack("<HHfff", granny[object_offs: object_offs + 16])
                    frames.append((0.0, Vector((sx, sy, sz))))
                else:
                    _header, _padding = struct.unpack("<HH", granny[object_offs: object_offs + 4])
                    vals = struct.unpack("<9f", granny[object_offs + 4: object_offs + 40])
                    frames.append((0.0, _hwugx_scale_from_scale_shear_values(vals)))
            except Exception:
                pass
        else:
            print(f"[UGX Pipeline] Unknown scale-shear curve format: {key_type}")
    except Exception as exc:
        print(f"[UGX Pipeline] Failed to read scale-shear keys ({key_type}): {exc}")
    return frames


# -----------------------------------------------------------------------------
# UAX animation import workflow
# -----------------------------------------------------------------------------

def find_target_armature(context) -> Optional[bpy.types.Object]:
    active = context.view_layer.objects.active
    if active and active.type == "ARMATURE":
        return active
    for obj in context.selected_objects:
        if obj.type == "ARMATURE":
            return obj
    return None


def ensure_action_channelbag_for_object(action: bpy.types.Action, obj: bpy.types.Object):
    """Return a Blender 5 channelbag for this object's action slot.

    Blender 5 removed Action.fcurves. The supported API is Action -> Slot ->
    Layer/Strip -> Channelbag -> FCurves. This helper creates/assigns the slot
    and channelbag without calling keyframe_insert(), so it avoids the legacy
    Action.fcurves path that was causing the UAX import crash.
    """
    if not obj.animation_data:
        obj.animation_data_create()

    # Create or reuse a slot. In Blender 4.4+/5, actions are not tied directly
    # to OBJECT anymore; the slot is what links the Action to the armature.
    slot = getattr(obj.animation_data, "action_slot", None)
    if slot is None and hasattr(action, "slots"):
        try:
            slot = action.slots.new(id_type=obj.id_type, name=obj.name)
        except Exception:
            try:
                slot = action.slots.new(id_type="OBJECT", name=obj.name)
            except Exception:
                try:
                    slot = action.slots.new(obj.id_type, obj.name)
                except Exception:
                    slot = None

    obj.animation_data.action = action
    if slot is not None:
        try:
            obj.animation_data.action_slot = slot
        except Exception:
            pass

    # Preferred Blender 5 helper. It creates a layer + keyframe strip +
    # channelbag for the slot when needed.
    if anim_utils is not None and hasattr(anim_utils, "action_ensure_channelbag_for_slot"):
        slot = getattr(obj.animation_data, "action_slot", None) or slot
        if slot is not None:
            return anim_utils.action_ensure_channelbag_for_slot(action, slot)

    # Manual fallback for builds where bpy_extras.anim_utils is missing.
    slots = list(getattr(action, "slots", []) or [])
    if slot is None and slots:
        slot = slots[0]
    layers = getattr(action, "layers", None)
    if layers is None:
        return None
    layer = layers[0] if len(layers) else layers.new(name="Base Layer")
    strips = getattr(layer, "strips", None)
    if strips is None:
        return None
    strip = strips[0] if len(strips) else strips.new(type="KEYFRAME")
    accessor = getattr(strip, "channelbag", None)
    if callable(accessor) and slot is not None:
        bag = accessor(slot)
        if bag is not None:
            return bag
    for bag_name in ("channelbags", "channel_bags"):
        bags = getattr(strip, bag_name, None)
        if bags is None:
            continue
        for bag in bags:
            if getattr(bag, "slot", None) == slot:
                return bag
        try:
            return bags.new(slot=slot)
        except Exception:
            pass
    return None


def iter_action_fcurves(action: bpy.types.Action):
    """Yield FCurves from Blender 5 channelbags, with legacy fallback."""
    if action is None:
        return

    # Blender 5 path.
    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            for bag_name in ("channelbags", "channel_bags"):
                bags = getattr(strip, bag_name, None)
                if bags:
                    for bag in bags:
                        for fcurve in getattr(bag, "fcurves", []) or []:
                            yield fcurve
            accessor = getattr(strip, "channelbag", None)
            if callable(accessor):
                for slot in getattr(action, "slots", []) or []:
                    try:
                        bag = accessor(slot)
                    except Exception:
                        bag = None
                    if bag:
                        for fcurve in getattr(bag, "fcurves", []) or []:
                            yield fcurve

    # Blender 4.x compatibility fallback only. Never required in Blender 5.
    legacy = getattr(action, "fcurves", None)
    if legacy:
        for fcurve in legacy:
            yield fcurve


def set_linear_interpolation(action: bpy.types.Action):
    for fcurve in iter_action_fcurves(action):
        for kp in fcurve.keyframe_points:
            kp.interpolation = "LINEAR"


def ensure_uax_fcurve(action: bpy.types.Action, obj: bpy.types.Object, data_path: str, index: int, group_name: str):
    """Create/find an FCurve without touching removed Action.fcurves."""
    # New Blender 5 convenience API. It routes to the object's assigned slot.
    ensure_method = getattr(action, "fcurve_ensure_for_datablock", None)
    if callable(ensure_method):
        try:
            return ensure_method(datablock=obj, data_path=data_path, index=index, group_name=group_name)
        except TypeError:
            try:
                return ensure_method(obj, data_path, index=index, group_name=group_name)
            except Exception:
                pass
        except Exception:
            pass

    channelbag = ensure_action_channelbag_for_object(action, obj)
    if channelbag is None:
        # Last fallback for Blender 4.x only.
        legacy = getattr(action, "fcurves", None)
        if legacy is None:
            raise RuntimeError("Could not create Blender 5 action channelbag for UAX FCurves.")
        fc = legacy.find(data_path, index=index)
        return fc or legacy.new(data_path, index=index, action_group=group_name)

    fcurves = channelbag.fcurves
    # Blender 5 has fcurves.ensure(). Use it when present, otherwise find/new.
    ensure_fc = getattr(fcurves, "ensure", None)
    if callable(ensure_fc):
        try:
            return ensure_fc(data_path, index=index, group_name=group_name)
        except TypeError:
            return ensure_fc(data_path, index=index)
    fc = fcurves.find(data_path, index=index)
    if fc is None:
        try:
            fc = fcurves.new(data_path, index=index, group_name=group_name)
        except TypeError:
            fc = fcurves.new(data_path, index=index)
    return fc


def insert_key_fast(fcurve, frame: float, value: float, interpolation="LINEAR"):
    kp = fcurve.keyframe_points.insert(float(frame), float(value), options={"FAST"})
    try:
        kp.interpolation = interpolation
    except Exception:
        pass
    return kp



def _set_or_insert_key(fcurve, frame: float, value: float, interpolation="LINEAR"):
    """Set an existing key at frame, or insert one if missing.

    Blender 5 Action/Channelbag FCurves still expose keyframe_points once the
    FCurve itself is resolved. This helper is used by the animation helper and
    ground clamp tools so they update the current timeline curve instead of
    creating duplicate keys or crashing when a key is absent.
    """
    if fcurve is None:
        return None
    frame = float(frame)
    value = float(value)
    for kp in fcurve.keyframe_points:
        if abs(float(kp.co.x) - frame) <= 0.0001:
            kp.co.y = value
            try:
                kp.handle_left.y = value
                kp.handle_right.y = value
            except Exception:
                pass
            try:
                kp.interpolation = interpolation
            except Exception:
                pass
            return kp
    return insert_key_fast(fcurve, frame, value, interpolation=interpolation)

def insert_vector_keys(action, obj, bone_name: str, prop: str, frame: float, values, group_name: str):
    path = f'pose.bones["{bone_name}"].{prop}'
    for idx, value in enumerate(values):
        fc = ensure_uax_fcurve(action, obj, path, idx, group_name)
        insert_key_fast(fc, frame, value)


def find_action_fcurve(action: bpy.types.Action, data_path: str, index: int):
    for fcurve in iter_action_fcurves(action):
        if getattr(fcurve, "data_path", None) == data_path and getattr(fcurve, "array_index", None) == index:
            return fcurve
    return None


def _mesh_users_of_armature(armature: bpy.types.Object) -> list[bpy.types.Object]:
    meshes = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        if obj.parent == armature:
            meshes.append(obj)
            continue
        for mod in obj.modifiers:
            if mod.type == "ARMATURE" and getattr(mod, "object", None) == armature:
                meshes.append(obj)
                break
    return meshes


def _find_ground_driver_bone(armature: bpy.types.Object) -> Optional[str]:
    preferred = ("b_root", "bone_root", "root")
    bone_names = set(armature.data.bones.keys())
    for name in preferred:
        if name in bone_names:
            return name
    for bone in armature.data.bones:
        if not _is_granny_root_bone_name(bone.name):
            return bone.name
    return armature.data.bones[0].name if armature.data.bones else None


def _action_frame_range(action: bpy.types.Action) -> tuple[int, int]:
    try:
        start, end = action.frame_range
    except Exception:
        start, end = (1.0, 1.0)
    start = max(1, int(math.floor(start)))
    end = max(start, int(math.ceil(end)))
    return start, end


def _evaluated_mesh_min_z(meshes: list[bpy.types.Object], depsgraph) -> Optional[float]:
    min_z = None
    for obj in meshes:
        try:
            eval_obj = obj.evaluated_get(depsgraph)
            matrix = eval_obj.matrix_world
            corners = getattr(eval_obj, "bound_box", None)
            if not corners:
                continue
            for corner in corners:
                z = (matrix @ Vector(corner)).z
                min_z = z if min_z is None else min(min_z, z)
        except Exception:
            continue
    return min_z


def apply_action_ground_contact_correction(action: bpy.types.Action, armature: bpy.types.Object, *, mode="SMART_CLAMP", ground_z=0.0, tolerance=0.002):
    """Offset the gameplay root Z so imported clips visually sit on Blender's ground.

    Halo Wars UAX clips can be authored relative to the engine's mover/pelvis basis.
    When those tracks are evaluated on the glTF-imported Blender rig, some clips
    look like the feet/crouch pose is floating. This correction samples the actual
    skinned mesh bbox and writes a compensating Z curve onto b_root/bone_root.
    Rotations are untouched, and GrannyRootBone remains locked by the existing fix.
    """
    if mode in {"OFF", "NONE", "DISABLED"} or not action or not armature or armature.type != "ARMATURE":
        return 0
    root_bone = _find_ground_driver_bone(armature)
    if not root_bone:
        return 0
    meshes = _mesh_users_of_armature(armature)
    if not meshes:
        return 0

    scene = bpy.context.scene
    previous_frame = scene.frame_current
    previous_action = armature.animation_data.action if armature.animation_data else None
    if not armature.animation_data:
        armature.animation_data_create()
    armature.animation_data.action = action

    start, end = _action_frame_range(action)
    # Grounding every frame is fine for HW2 infantry clips, but keep a soft cap for
    # unusually long actions so the UI does not hang.
    max_samples = 240
    if end - start + 1 > max_samples:
        step = max(1, int(math.ceil((end - start + 1) / max_samples)))
        frames = list(range(start, end + 1, step))
        if frames[-1] != end:
            frames.append(end)
    else:
        frames = list(range(start, end + 1))

    samples = []
    try:
        for frame in frames:
            scene.frame_set(frame)
            depsgraph = bpy.context.evaluated_depsgraph_get()
            min_z = _evaluated_mesh_min_z(meshes, depsgraph)
            if min_z is not None:
                samples.append((float(frame), float(min_z)))
    finally:
        try:
            scene.frame_set(previous_frame)
        except Exception:
            pass
        if previous_action is not None and armature.animation_data:
            armature.animation_data.action = previous_action

    if not samples:
        return 0

    path = f'pose.bones["{root_bone}"].location'
    z_curve = find_action_fcurve(action, path, 2)
    if z_curve is None:
        z_curve = ensure_uax_fcurve(action, armature, path, 2, root_bone)
        insert_key_fast(z_curve, start, 0.0)

    inserted = 0
    if mode == "CONSTANT_OFFSET":
        # Use the lowest sampled pose so existing running bob/hops are preserved.
        min_sample_z = min(z for _frame, z in samples)
        correction = float(ground_z) - min_sample_z
        if abs(correction) > tolerance:
            for frame, _z in samples:
                insert_key_fast(z_curve, frame, z_curve.evaluate(frame) + correction)
                inserted += 1
    else:
        # SMART_CLAMP / FOOT_LOCK: each sampled frame is moved to ground contact.
        # This is the strongest fix for crouch/walk clips that float above the floor.
        for frame, min_z in samples:
            correction = float(ground_z) - min_z
            if abs(correction) > tolerance:
                insert_key_fast(z_curve, frame, z_curve.evaluate(frame) + correction)
                inserted += 1

    try:
        z_curve.update()
    except Exception:
        pass
    return inserted


def _uax_axis_conversion_quat(mode: str) -> Quaternion:
    # IMPORTANT: the UGX mesh/glTF rig already arrives in the correct Blender basis.
    # Do NOT rotate every bone's animation keys by default; that destroys the pose.
    # The default therefore matches the original Blender 4 importer math exactly.
    if mode == "GLTF_YUP_TO_BLENDER_ZUP":
        return mathutils.Matrix.Rotation(math.radians(90.0), 4, "X").to_quaternion()
    if mode == "GLTF_YUP_TO_BLENDER_ZUP_NEG":
        return mathutils.Matrix.Rotation(math.radians(-90.0), 4, "X").to_quaternion()
    return Quaternion((1.0, 0.0, 0.0, 0.0))


def _convert_uax_vector(vec: Vector, correction: Quaternion) -> Vector:
    return correction @ Vector(vec)


def _convert_uax_quaternion(quat: Quaternion, correction: Quaternion) -> Quaternion:
    if abs(correction.angle) < 0.000001:
        return Quaternion(quat)
    return correction @ Quaternion(quat) @ correction.inverted()




def _is_granny_root_bone_name(bone_name: str) -> bool:
    lower = bone_name.lower()
    return lower == "grannyrootbone" or lower.startswith("grannyrootbone_")


def _is_game_root_bone_name(bone_name: str) -> bool:
    lower = bone_name.lower()
    return lower in {"b_root", "bone_root", "root"} or _is_granny_root_bone_name(bone_name)


def _is_motion_root_bone_name(bone_name: str) -> bool:
    lower = bone_name.lower()
    return lower in {"b_root", "bone_root", "root"}


def _is_pelvis_bone_name(bone_name: str) -> bool:
    lower = bone_name.lower()
    return lower in {"b_pelvis", "pelvis", "b_hips", "hips", "b_hip", "hip"}

def create_action_from_uax(name: str, armature: bpy.types.Object, granny: bytes, *, set_scene_range=True, linear_keys=True, axis_correction="LEGACY_RAW_SAFE", legacy_child_z_flip=True, child_translation_mode="ALL_BONES", granny_root_mode="LEGACY", root_stabilize_mode="NONE", b_root_xy_lock=False, ground_correction_mode="SMART_CLAMP") -> bpy.types.Action:
    if not armature or armature.type != "ARMATURE":
        raise ValueError("Select an armature before importing UAX animations.")

    if not armature.animation_data:
        armature.animation_data_create()

    action_name = name
    # Keep the Blender Action name identical to the UAX clip name.
    # Older HW Suite prefixed actions with the armature name, but that breaks 1:1
    # animation round-tripping and creates names like Armature_odst_cower_01.
    action = bpy.data.actions.new(action_name)
    ensure_action_channelbag_for_object(action, armature)
    import_scale = float(armature.get("hwugx_import_scale", 1.0))

    track_groups_len, track_groups_offs = struct.unpack("<IQ", granny[108:120])
    animations_len, animations_offs = struct.unpack("<IQ", granny[120:132])

    duration_seconds = 0.0
    if animations_len > 0 and animations_offs + 8 <= len(granny):
        animation0_real_offs = struct.unpack("<Q", granny[animations_offs: animations_offs + 8])[0]
        if animation0_real_offs + 16 <= len(granny):
            duration_seconds, _timestep = struct.unpack("<ff", granny[animation0_real_offs + 8: animation0_real_offs + 16])

    fps = bpy.context.scene.render.fps / max(bpy.context.scene.render.fps_base, 0.0001)
    max_frame = 1.0
    keyed_bones = set()
    track_group_size = 172
    transform_track_size = 60

    for i in range(track_groups_len):
        group_ptr_offs = track_groups_offs + i * track_group_size
        if group_ptr_offs + 8 > len(granny):
            continue
        cur_group = struct.unpack("<Q", granny[group_ptr_offs: group_ptr_offs + 8])[0]
        if cur_group + 32 > len(granny):
            continue
        transform_tracks_len, transform_tracks_offs = struct.unpack("<IQ", granny[cur_group + 20: cur_group + 32])
        flags = struct.unpack("<I", granny[cur_group + 124: cur_group + 128])[0] if cur_group + 128 <= len(granny) else 0
        is_vda = bool(flags & 4)

        for t in range(transform_tracks_len):
            cur = transform_tracks_offs + t * transform_track_size
            if cur + transform_track_size > len(granny):
                continue
            name_offs, _track_flags = struct.unpack("<QI", granny[cur:cur + 12])
            bone_name = read_c_string(granny, name_offs)
            if not bone_name or bone_name not in armature.data.bones:
                continue

            bone = armature.data.bones[bone_name]
            is_granny_root_bone = _is_granny_root_bone_name(bone_name)
            is_motion_root_bone = _is_motion_root_bone_name(bone_name)
            is_pelvis_bone = _is_pelvis_bone_name(bone_name)
            keyed_bones.add(bone_name)

            rot_curve_type_offs, rot_curve_object_offs = struct.unpack("<QQ", granny[cur + 12: cur + 28])
            rot_keys = []
            if rot_curve_type_offs + 12 <= len(granny):
                rot_type_name_offs = struct.unpack("<IQ", granny[rot_curve_type_offs: rot_curve_type_offs + 12])[1]
                rot_curve_type = read_c_string(granny, rot_type_name_offs)
                rot_keys = get_rot_keys_from_granny(granny, rot_curve_type, rot_curve_object_offs)

            axis_q = _uax_axis_conversion_quat(axis_correction)

            if is_granny_root_bone and granny_root_mode in {"LOCK_ROT", "LOCK_ROT_LOC"}:
                # The glTF-imported UGX rig already has the correct scene/model basis.
                # UAX files can key GrannyRootBone in the native Granny basis, which pitches
                # the whole imported Blender rig upward. Locking this shim/root bone keeps
                # the model basis stable while the real gameplay bones still animate.
                insert_vector_keys(action, armature, bone_name, "rotation_quaternion", 1.0,
                                   (1.0, 0.0, 0.0, 0.0), bone_name)
            elif is_motion_root_bone and root_stabilize_mode in {"LOCK_GAME_ROOT_ROT", "LOCK_GAME_ROOT_ROT_LOC", "LOCK_GAME_ROOT_AND_PELVIS_LOC"}:
                insert_vector_keys(action, armature, bone_name, "rotation_quaternion", 1.0,
                                   (1.0, 0.0, 0.0, 0.0), bone_name)
            elif rot_keys:
                for seconds, quat in rot_keys:
                    frame = max(1.0, seconds * fps)
                    converted_quat = _convert_uax_quaternion(quat, axis_q)
                    final_quat = bone.matrix.to_quaternion().inverted() @ converted_quat
                    final_quat.normalize()
                    insert_vector_keys(action, armature, bone_name, "rotation_quaternion", frame,
                                       (final_quat.w, final_quat.x, final_quat.y, final_quat.z), bone_name)
                    max_frame = max(max_frame, frame)
            else:
                insert_vector_keys(action, armature, bone_name, "rotation_quaternion", 1.0,
                                   (1.0, 0.0, 0.0, 0.0), bone_name)

            pos_curve_type_offs, pos_curve_object_offs = struct.unpack("<QQ", granny[cur + 28: cur + 44])
            pos_keys = []
            if pos_curve_type_offs + 12 <= len(granny):
                pos_type_name_offs = struct.unpack("<IQ", granny[pos_curve_type_offs: pos_curve_type_offs + 12])[1]
                pos_curve_type = read_c_string(granny, pos_type_name_offs)
                pos_keys = get_pos_keys_from_granny(granny, pos_curve_type, pos_curve_object_offs)

            # UAX position tracks are authored in the original Granny/HW rig basis.
            # On glTF-imported rigs, writing every child bone location track can pull
            # heads away from their parents and "explode" the model. The safe default
            # keeps child bones connected by only using root-motion translation.
            is_root_bone = bone.parent is None or _is_game_root_bone_name(bone_name)
            should_write_location = (child_translation_mode == "ALL_BONES") or (child_translation_mode == "ROOT_ONLY" and is_root_bone)

            if is_granny_root_bone and granny_root_mode == "LOCK_ROT_LOC":
                # Keep the converter shim/root at rest. This prevents imported UAX clips
                # from sending the whole glTF rig upward while still allowing b_root and
                # child gameplay bones to receive their original animation.
                insert_vector_keys(action, armature, bone_name, "location", 1.0,
                                   (0.0, 0.0, 0.0), bone_name)
            elif is_motion_root_bone and root_stabilize_mode in {"LOCK_GAME_ROOT_ROT_LOC", "LOCK_GAME_ROOT_AND_PELVIS_LOC"}:
                insert_vector_keys(action, armature, bone_name, "location", 1.0,
                                   (0.0, 0.0, 0.0), bone_name)
            elif is_pelvis_bone and root_stabilize_mode == "LOCK_GAME_ROOT_AND_PELVIS_LOC":
                insert_vector_keys(action, armature, bone_name, "location", 1.0,
                                   (0.0, 0.0, 0.0), bone_name)
            elif pos_keys and should_write_location:
                for seconds, vec in pos_keys:
                    frame = max(1.0, seconds * fps)
                    final = _convert_uax_vector(Vector(vec), axis_q) * import_scale
                    if legacy_child_z_flip and bone.parent and not is_vda:
                        final.z = -final.z
                    loc = final - bone.head
                    # Optional Halo Wars root-motion stabilizer: keep authored b_root Z
                    # translation for hops/vertical recoil, but zero X/Y location drift so
                    # clips do not pivot/orbit around the imported Blender armature. This
                    # does not affect b_root rotation or any other bone rotations.
                    if b_root_xy_lock and is_motion_root_bone:
                        loc.x = 0.0
                        loc.y = 0.0
                    insert_vector_keys(action, armature, bone_name, "location", frame,
                                       (loc.x, loc.y, loc.z), bone_name)
                    max_frame = max(max_frame, frame)
            else:
                # Lock non-root translation at rest so rotations animate the rig
                # without disconnecting/deforming the skeleton.
                insert_vector_keys(action, armature, bone_name, "location", 1.0,
                                   (0.0, 0.0, 0.0), bone_name)

            # v6.8: import native Granny scale-shear curves instead of forcing
            # every bone scale to 1.0. This restores custom scale-only UAX clips
            # made through the working legacy toolchain.
            scl_curve_type_offs, scl_curve_object_offs = struct.unpack("<QQ", granny[cur + 44: cur + 60])
            scl_keys = []
            if scl_curve_type_offs + 12 <= len(granny):
                scl_type_name_offs = struct.unpack("<IQ", granny[scl_curve_type_offs: scl_curve_type_offs + 12])[1]
                scl_curve_type = read_c_string(granny, scl_type_name_offs)
                scl_keys = get_scale_shear_keys_from_granny(granny, scl_curve_type, scl_curve_object_offs)

            if scl_keys:
                # Imported pose scale must be relative to the track's own rest
                # scale. Old/custom UAX files may use native basis values such as
                # 1.6 or 1.0 for rest; Blender pose scale should start at 1.0.
                rest_scl = Vector(scl_keys[0][1])
                for seconds, scl in scl_keys:
                    frame = max(1.0, seconds * fps)
                    rel = _hwugx_safe_relative_scale(Vector(scl), rest_scl)
                    insert_vector_keys(action, armature, bone_name, "scale", frame,
                                       (rel.x, rel.y, rel.z), bone_name)
                    max_frame = max(max_frame, frame)
            else:
                insert_vector_keys(action, armature, bone_name, "scale", 1.0,
                                   (1.0, 1.0, 1.0), bone_name)

    # Make frame ranges/evaluated curves available before the mesh-based grounding pass.
    for _pre_ground_fcurve in iter_action_fcurves(action):
        try:
            _pre_ground_fcurve.update()
        except Exception:
            pass

    # Optional mesh-based grounding pass. This happens after all UAX keys are written
    # so it can offset b_root/bone_root Z without changing any rotations.
    try:
        if ground_correction_mode and ground_correction_mode != "OFF":
            inserted_ground_keys = apply_action_ground_contact_correction(action, armature, mode=ground_correction_mode)
            if inserted_ground_keys:
                action["uax_ground_contact_keys"] = inserted_ground_keys
                action["uax_ground_contact_mode"] = ground_correction_mode
    except Exception as exc:
        print(f"[UGX Pipeline] Ground contact correction skipped for {name}: {exc}")

    # Finalize curves.
    for fcurve in iter_action_fcurves(action):
        try:
            fcurve.update()
        except Exception:
            pass
    if linear_keys:
        set_linear_interpolation(action)

    if set_scene_range:
        bpy.context.scene.frame_start = 1
        bpy.context.scene.frame_end = max(1, int(math.ceil(duration_seconds * fps or max_frame)))

    action["uax_source"] = name
    action["uax_keyed_bones"] = len(keyed_bones)
    action["uax_duration_seconds"] = duration_seconds
    return action

def import_uax(filepath: str, armature: bpy.types.Object, *, set_scene_range=True, linear_keys=True, axis_correction="LEGACY_RAW_SAFE", legacy_child_z_flip=True, child_translation_mode="ALL_BONES", granny_root_mode="LEGACY", root_stabilize_mode="NONE", b_root_xy_lock=False, ground_correction_mode="SMART_CLAMP") -> bpy.types.Action:
    with open(filepath, "rb") as f:
        raw_uax = f.read()
    chunks = load_ecf_chunks(filepath)
    granny_chunks = chunks.get(0x700)
    if not granny_chunks:
        raise ValueError("UAX did not contain a Granny animation chunk 0x700.")
    name = os.path.splitext(os.path.basename(filepath))[0]
    action = create_action_from_uax(
        name, armature, granny_chunks[0],
        set_scene_range=set_scene_range,
        linear_keys=linear_keys,
        axis_correction=axis_correction,
        legacy_child_z_flip=legacy_child_z_flip,
        child_translation_mode=child_translation_mode,
        granny_root_mode=granny_root_mode,
        root_stabilize_mode=root_stabilize_mode,
        b_root_xy_lock=b_root_xy_lock,
        ground_correction_mode=ground_correction_mode,
    )
    _store_raw_uax_on_action(action, filepath, raw_uax)
    # Store the exact import conversion settings so native UAX export can reverse
    # Blender Action values back into the original Granny curve buffers.
    action["uax_import_axis_correction"] = axis_correction
    action["uax_import_legacy_child_z_flip"] = bool(legacy_child_z_flip)
    action["uax_import_child_translation_mode"] = child_translation_mode
    action["uax_import_granny_root_mode"] = granny_root_mode
    action["uax_import_root_stabilize_mode"] = root_stabilize_mode
    action["uax_import_b_root_xy_lock"] = bool(b_root_xy_lock)
    action["uax_import_ground_correction_mode"] = ground_correction_mode
    try:
        action["uax_import_scale"] = float(armature.get("hwugx_import_scale", 1.0))
    except Exception:
        action["uax_import_scale"] = 1.0
    return action



# -----------------------------------------------------------------------------
# Post-import cleanup for ugx.exe -> glTF imports
# -----------------------------------------------------------------------------

def _root_imported_objects(new_objects: Iterable[bpy.types.Object]) -> list[bpy.types.Object]:
    new_set = set(new_objects)
    return [obj for obj in new_objects if obj.parent not in new_set]


def _apply_world_correction_to_roots(new_objects: Iterable[bpy.types.Object], *, radians_x: float = math.radians(90.0)):
    """Rotate the imported UGX bundle upright without baking mesh/animation data.

    The original Blender 4 direct importer applied +90° X to the armature. The
    ugx.exe glTF bridge arrives sideways in Blender 5, so we apply the same
    correction at the root object level. This keeps child meshes, armature
    modifiers, and imported actions together.
    """
    correction = mathutils.Matrix.Rotation(radians_x, 4, "X")
    for obj in _root_imported_objects(new_objects):
        try:
            obj.matrix_world = correction @ obj.matrix_world
        except Exception:
            pass


def _force_armature_octahedral(obj: bpy.types.Object):
    """Force a Blender armature to use normal octahedral viewport bones.

    Some glTF rigs come in looking like round envelope/icosphere controls. This
    clears custom pose shapes and sets every armature display property we can
    safely touch.
    """
    if not obj or obj.type != "ARMATURE":
        return
    arm = obj.data
    for owner in (arm, obj):
        try:
            owner.display_type = "OCTAHEDRAL"
        except Exception:
            pass
    try:
        arm.show_bone_custom_shapes = False
    except Exception:
        pass
    try:
        arm.show_names = False
    except Exception:
        pass
    try:
        obj.show_in_front = True
    except Exception:
        pass
    if obj.pose:
        for pbone in obj.pose.bones:
            try:
                pbone.custom_shape = None
            except Exception:
                pass
            try:
                pbone.custom_shape_scale_xyz = (1.0, 1.0, 1.0)
            except Exception:
                pass



def _connect_armature_bone_chains(obj: bpy.types.Object, *, max_snap_distance=10.0):
    """Deprecated safety stub.

    v3.6 attempted to physically connect imported glTF armature bones by editing
    rest-pose tails/use_connect. That can change rest bone axes and causes UAX
    rotations to evaluate incorrectly. Keep this function as a no-op for saved
    presets, but never mutate the armature here.
    """
    return 0

def _connect_armature_bone_chains_DISABLED(obj: bpy.types.Object, *, max_snap_distance=10.0):
    if not obj or obj.type != "ARMATURE":
        return 0
    prev_active = bpy.context.view_layer.objects.active
    prev_mode = prev_active.mode if prev_active else "OBJECT"
    selected = list(bpy.context.selected_objects)
    changed = 0
    try:
        if prev_active and prev_active.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        ebones = obj.data.edit_bones
        children_by_parent = {}
        for eb in ebones:
            if eb.parent:
                children_by_parent.setdefault(eb.parent.name, []).append(eb)
        for parent_name, children in children_by_parent.items():
            if len(children) != 1:
                continue
            parent = ebones.get(parent_name)
            child = children[0]
            if not parent or not child:
                continue
            # Skip disconnected accessory/socket/helper roots. Real deform chains
            # usually have the child head reasonably near the parent segment.
            try:
                dist = (parent.tail - child.head).length
                parent_len = max(parent.length, 0.0001)
            except Exception:
                continue
            if dist > max(max_snap_distance, parent_len * 8.0):
                continue
            parent.tail = child.head.copy()
            child.use_connect = True
            changed += 1
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception as exc:
        print(f"[UGX Pipeline] Bone connect cleanup skipped for {obj.name}: {exc}")
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    finally:
        try:
            bpy.ops.object.select_all(action="DESELECT")
            for old in selected:
                if old and old.name in bpy.data.objects:
                    old.select_set(True)
            if prev_active and prev_active.name in bpy.data.objects:
                bpy.context.view_layer.objects.active = prev_active
                if prev_mode != "OBJECT" and prev_active.mode != prev_mode:
                    bpy.ops.object.mode_set(mode=prev_mode)
        except Exception:
            pass
    return changed


def _connect_imported_armatures(new_objects: Iterable[bpy.types.Object]):
    # No-op by design. Do not edit rest-pose bone heads/tails/use_connect during
    # import; doing so changes the basis used by UAX rotation curves.
    return 0

def _set_armatures_octahedral(new_objects: Iterable[bpy.types.Object]):
    # First touch the imported objects, then all scene armatures. The second pass
    # handles converter builds where Blender creates/renames the armature before
    # our object-diff list catches it.
    for obj in new_objects:
        _force_armature_octahedral(obj)
    for obj in bpy.context.scene.objects:
        _force_armature_octahedral(obj)


def _make_clean_principled_material(mat: bpy.types.Material, slot_index: int = 0):
    """Replace broken/glTF debug-looking materials with stable neutral previews."""
    # Stable, non-random neutral values so the model stops showing noisy/funky
    # checker-like placeholders while still separating material slots slightly.
    shade = 0.44 + ((slot_index % 5) * 0.045)
    base_color = (shade, shade, shade, 1.0)
    mat.diffuse_color = base_color
    mat.use_nodes = True
    mat.blend_method = "OPAQUE"
    mat.use_backface_culling = False

    nodes = mat.node_tree.nodes if mat.node_tree else None
    if not nodes:
        return
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = nodes.new(type="ShaderNodeBsdfPrincipled")
    # Clear only links into the BSDF so stale/missing image nodes do not drive
    # the preview. Leave nodes in the material for future manual recovery.
    try:
        for input_socket in bsdf.inputs:
            for link in list(input_socket.links):
                mat.node_tree.links.remove(link)
    except Exception:
        pass

    def set_input(names, value):
        for name in names:
            socket = bsdf.inputs.get(name)
            if socket is not None:
                try:
                    socket.default_value = value
                    return
                except Exception:
                    pass

    set_input(("Base Color",), base_color)
    set_input(("Metallic",), 0.0)
    set_input(("Roughness",), 0.68)
    set_input(("Alpha",), 1.0)
    set_input(("Specular IOR Level", "Specular"), 0.35)


def _clean_imported_meshes_and_materials(new_objects: Iterable[bpy.types.Object], *, clean_materials: bool = True):
    material_slot_index = 0
    seen_mats = set()
    for obj in new_objects:
        if obj.type != "MESH":
            continue
        mesh = obj.data
        try:
            obj.color = (0.55, 0.55, 0.55, 1.0)
        except Exception:
            pass
        # Stop broken glTF/custom normals from causing the ugly faceted / camouflage-like
        # solid-view artifacts. Recalculate face normals and clear custom split normals.
        try:
            for poly in mesh.polygons:
                poly.use_smooth = False
        except Exception:
            pass
        try:
            mesh.validate(clean_customdata=False)
            mesh.update(calc_edges=True)
        except Exception:
            pass
        try:
            if getattr(mesh, "has_custom_normals", False):
                mesh.normals_split_custom_set(None)
                mesh.update()
        except Exception:
            pass
        try:
            # Optional Blender operator path, needed by some 4.x/5.x builds to fully
            # remove custom normal data from imported glTF meshes.
            old_active = bpy.context.view_layer.objects.active
            old_selected = list(bpy.context.selected_objects)
            bpy.ops.object.mode_set(mode="OBJECT")
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            bpy.ops.mesh.customdata_custom_splitnormals_clear()
            bpy.ops.object.shade_flat()
            bpy.ops.object.select_all(action="DESELECT")
            for old in old_selected:
                if old and old.name in bpy.data.objects:
                    old.select_set(True)
            if old_active and old_active.name in bpy.data.objects:
                bpy.context.view_layer.objects.active = old_active
        except Exception:
            pass

        if not clean_materials:
            continue
        for mat in obj.data.materials:
            if mat and mat.name not in seen_mats:
                _make_clean_principled_material(mat, material_slot_index)
                seen_mats.add(mat.name)
                material_slot_index += 1



def _scale_mesh_data_object(obj: bpy.types.Object, scale_factor: float):
    if not obj or obj.type != "MESH" or not obj.data or abs(scale_factor - 1.0) < 0.0000001:
        return
    mesh = obj.data
    try:
        for vert in mesh.vertices:
            vert.co *= scale_factor
        if mesh.shape_keys:
            for key in mesh.shape_keys.key_blocks:
                for point in key.data:
                    point.co *= scale_factor
        mesh.update()
        obj["hwugx_import_scale"] = scale_factor
    except Exception:
        pass


def _scale_armature_data_object(obj: bpy.types.Object, scale_factor: float):
    if not obj or obj.type != "ARMATURE" or not obj.data or abs(scale_factor - 1.0) < 0.0000001:
        return
    prev_active = bpy.context.view_layer.objects.active
    prev_mode = obj.mode if obj == prev_active else "OBJECT"
    selected = list(bpy.context.selected_objects)
    try:
        bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass
    try:
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        for eb in obj.data.edit_bones:
            eb.head *= scale_factor
            eb.tail *= scale_factor
            try:
                eb.head_radius *= scale_factor
                eb.tail_radius *= scale_factor
                eb.envelope_distance *= scale_factor
            except Exception:
                pass
        bpy.ops.object.mode_set(mode="OBJECT")
        obj["hwugx_import_scale"] = scale_factor
    except Exception:
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except Exception:
            pass
    finally:
        try:
            bpy.ops.object.select_all(action="DESELECT")
            for old in selected:
                if old and old.name in bpy.data.objects:
                    old.select_set(True)
            if prev_active and prev_active.name in bpy.data.objects:
                bpy.context.view_layer.objects.active = prev_active
                if prev_mode != "OBJECT" and prev_active.mode != prev_mode:
                    bpy.ops.object.mode_set(mode=prev_mode)
        except Exception:
            pass


def _apply_hw2_import_scale(new_objects: Iterable[bpy.types.Object], *, scale_factor: float = HW2_IMPORT_SCALE, ground_z: float = HW2_IMPORT_GROUND_Z):
    objs = [obj for obj in new_objects if obj and obj.name in bpy.data.objects]
    if abs(scale_factor - 1.0) < 0.0000001 and abs(ground_z) < 0.0000001:
        return
    # Match the old manual benchmark without changing object scale transforms:
    # edit/data scale down the mesh + rest bones, then move the imported root down.
    for obj in objs:
        if obj.type == "MESH":
            _scale_mesh_data_object(obj, scale_factor)
        elif obj.type == "ARMATURE":
            _scale_armature_data_object(obj, scale_factor)
    for obj in objs:
        try:
            if obj.type == "ARMATURE":
                obj["hwugx_import_scale"] = scale_factor
        except Exception:
            pass
    if abs(ground_z) > 0.0000001:
        for root in _root_imported_objects(objs):
            try:
                root.location.z += ground_z
                root["hwugx_ground_z_offset"] = ground_z
            except Exception:
                pass

def cleanup_imported_ugx_objects(new_objects: Iterable[bpy.types.Object], *, fix_upright=False, octahedral_bones=True, connect_bones=False, clean_materials=True, apply_hw2_scale=True, hw2_scale=HW2_IMPORT_SCALE, hw2_ground_z=HW2_IMPORT_GROUND_Z):
    new_objects = [obj for obj in new_objects if obj and obj.name in bpy.data.objects]
    # The ugx.exe -> glTF model orientation is already correct for the current converter.
    # This is now optional only, because rotating the imported root made the model lie sideways.
    if fix_upright:
        _apply_world_correction_to_roots(new_objects, radians_x=math.radians(90.0))
    if apply_hw2_scale:
        _apply_hw2_import_scale(new_objects, scale_factor=hw2_scale, ground_z=hw2_ground_z)
    if octahedral_bones:
        _set_armatures_octahedral(new_objects)
    if connect_bones:
        _connect_imported_armatures(new_objects)
    _clean_imported_meshes_and_materials(new_objects, clean_materials=clean_materials)
    # Match the old compact skinning path: normalize and keep strongest 4 weights.
    _repair_skin_weights_for_objects(new_objects, limit_to_four=True)

# -----------------------------------------------------------------------------
# Operators: UGX glTF bridge
# -----------------------------------------------------------------------------

class HWUGX_OT_import_ugx(bpy.types.Operator, ImportHelper):
    bl_idname = "import_scene.hw_ugx"
    bl_label = "Import Halo Wars UGX"
    bl_description = "Convert UGX to glTF with ugx.exe, then import it into Blender"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".ugx"

    filter_glob: StringProperty(default="*.ugx", options={"HIDDEN"})
    fix_upright: BoolProperty(
        name="Fix Upright Orientation",
        description="Apply the Halo Wars +90° X import correction so the model/animations stand upright in Blender",
        default=False,
    )
    octahedral_bones: BoolProperty(
        name="Octahedral Bones",
        description="Force imported armatures to display as Blender octahedral bones instead of sphere/envelope-looking bones",
        default=True,
    )
    connect_bones: BoolProperty(
        name="Connect Bone Chains (Disabled)",
        description="Disabled for animation safety; physically connecting imported glTF bones changes rest axes and breaks UAX rotations",
        default=False,
    )
    clean_materials: BoolProperty(
        name="Clean Preview Materials",
        description="Replace broken or noisy glTF placeholder materials with stable neutral Halo-style preview materials",
        default=True,
    )
    apply_hw2_scale: BoolProperty(
        name="Apply HW2 Scale",
        description="Automatically data-scale meshes and rest bones by 0.634920635. No Z offset is applied by default",
        default=True,
    )
    hw2_scale: FloatProperty(
        name="HW2 Scale",
        description="Scale applied directly to imported mesh vertices and armature edit bones",
        default=HW2_IMPORT_SCALE,
        precision=9,
    )
    hw2_ground_z: FloatProperty(
        name="Ground Z Offset",
        description="Optional Z translation applied to imported root object(s) after data scaling. Default is 0 because scale-only is the correct HW2 benchmark",
        default=HW2_IMPORT_GROUND_Z,
        precision=9,
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        box = layout.box()
        box.label(text="Import Cleanup", icon="BRUSH_DATA")
        box.prop(self, "fix_upright")
        box.prop(self, "octahedral_bones")
        # Bone chain connection is intentionally hidden/disabled: editing rest-pose
        # bone tails/use_connect breaks 1:1 UAX rotation evaluation.
        box.prop(self, "clean_materials")
        layout.separator()
        scale_box = layout.box()
        scale_box.label(text="Halo Wars 2 Scale", icon="EMPTY_ARROWS")
        scale_box.prop(self, "apply_hw2_scale")
        sub = scale_box.column()
        sub.enabled = self.apply_hw2_scale
        sub.prop(self, "hw2_scale")
        # Ground Z offset intentionally hidden; default is 0.0 after v3.2.0

    def execute(self, context):
        ugx_exe = ensure_converter(self, context)
        if not ugx_exe:
            return {"CANCELLED"}
        if not os.path.isfile(self.filepath):
            self.report({"ERROR"}, "UGX file not found.")
            return {"CANCELLED"}

        temp_dir = tempfile.mkdtemp(prefix="hw_ugx_import_")
        base_name = os.path.splitext(os.path.basename(self.filepath))[0]
        gltf_path = os.path.join(temp_dir, base_name + ".gltf")
        old_active = context.view_layer.objects.active
        old_selected = list(context.selected_objects)
        before_objects = set(bpy.data.objects)
        try:
            cmd = [ugx_exe, "to-gltf", "-i", self.filepath, "-o", gltf_path]
            if not run_ugx_command(cmd, self):
                return {"CANCELLED"}
            if not os.path.isfile(gltf_path):
                self.report({"ERROR"}, "ugx.exe did not produce a glTF file.")
                return {"CANCELLED"}
            bpy.ops.import_scene.gltf(filepath=gltf_path)
            imported_objects = [obj for obj in bpy.data.objects if obj not in before_objects]
            try:
                _attach_ugx_native_skeleton_to_armatures(imported_objects, self.filepath, self)
            except Exception as native_exc:
                self.report({"WARNING"}, f"UGX native skeleton cache failed; UAX export will use glTF rest basis: {native_exc}")
            cleanup_imported_ugx_objects(
                imported_objects,
                fix_upright=self.fix_upright,
                octahedral_bones=self.octahedral_bones,
                connect_bones=False,
                clean_materials=self.clean_materials,
                apply_hw2_scale=self.apply_hw2_scale,
                hw2_scale=self.hw2_scale,
                hw2_ground_z=self.hw2_ground_z,
            )
            try:
                bpy.ops.object.select_all(action="DESELECT")
                for obj in imported_objects:
                    obj.select_set(True)
                armatures = [obj for obj in imported_objects if obj.type == "ARMATURE"]
                if armatures:
                    context.view_layer.objects.active = armatures[0]
            except Exception:
                pass
            self.report({"INFO"}, f"Imported UGX with HW2 scale, octahedral bones, cleaned preview, and repaired weights: {os.path.basename(self.filepath)}")
            return {"FINISHED"}
        except Exception as exc:
            report_exception(self, "UGX import failed", exc)
            return {"CANCELLED"}
        finally:
            try:
                bpy.ops.object.select_all(action="DESELECT")
                for obj in old_selected:
                    if obj and obj.name in bpy.data.objects:
                        obj.select_set(True)
                if old_active and old_active.name in bpy.data.objects:
                    context.view_layer.objects.active = old_active
            except Exception:
                pass
            clean_temp_dir(temp_dir, context)


def enum_scene_armatures(self, context):
    items = [("", "Active / Viewport Selection", "Use the selected armature in the viewport")]
    for obj in context.scene.objects:
        if obj.type == "ARMATURE":
            items.append((obj.name, obj.name, "Export using this armature as the animation root"))
    return items


def enum_actions_for_export(self, context):
    items = [("", "Active Action", "Export the selected armature's current action")]
    try:
        armature = _resolve_export_armature(context)
    except Exception:
        armature = find_target_armature(context)
    actions = []
    if armature:
        try:
            actions = _iter_candidate_actions_for_armature(armature, all_actions=True)
        except Exception:
            actions = list(bpy.data.actions)
    else:
        actions = list(bpy.data.actions)
    seen = set()
    for action in actions:
        if action and action.name not in seen:
            label = str(action.get("uax_source", "")) or action.name
            items.append((action.name, label, f"Export {label} as one .uax file"))
            seen.add(action.name)
    return items




def enum_helper_bones(self, context):
    """Bone list for the UAX Animation Helper. Uses the active/selected armature."""
    armature = find_target_armature(context)
    if not armature or armature.type != "ARMATURE":
        return [("", "No armature selected", "Select the target armature first")]
    items = []
    for bone in armature.data.bones:
        items.append((bone.name, bone.name, ""))
    return items or [("", "No bones", "Selected armature has no bones")]


def enum_helper_actions(self, context):
    items = []
    armature = find_target_armature(context)
    active_name = None
    if armature and armature.animation_data and armature.animation_data.action:
        active_name = armature.animation_data.action.name
    if active_name:
        items.append(("__ACTIVE__", f"Active: {active_name}", "Use the selected armature's active action"))
    else:
        items.append(("__ACTIVE__", "Active Action", "Use the selected armature's active action"))
    for action in bpy.data.actions:
        items.append((action.name, action.name, ""))
    return items

class HWUGX_ExportSettings(bpy.types.PropertyGroup):
    """Sidebar/export dialog settings matching the requested UGX export layout."""

    axis_back: EnumProperty(
        name="Back",
        description="Back-facing axis label used by the Halo Wars export workflow",
        items=(("Z-", "Z-", ""), ("Z+", "Z+", ""), ("Y-", "Y-", ""), ("Y+", "Y+", ""), ("X-", "X-", ""), ("X+", "X+", "")),
        default="Z-",
    )
    axis_right: EnumProperty(
        name="Right",
        description="Right-facing axis label used by the Halo Wars export workflow",
        items=(("X+", "X+", ""), ("X-", "X-", ""), ("Y+", "Y+", ""), ("Y-", "Y-", ""), ("Z+", "Z+", ""), ("Z-", "Z-", "")),
        default="X-",
    )
    axis_up: EnumProperty(
        name="Up",
        description="Up axis for the intermediate glTF export",
        items=(("Y+", "Y+", ""), ("Z+", "Z+", "")),
        default="Y+",
    )
    export_meshes: BoolProperty(
        name="Export Meshes",
        description="Include mesh children/modifier users when exporting the selected armature",
        default=False,
    )
    export_selection_only: BoolProperty(
        name="Selected Only",
        description="Export only the current selection or the chosen armature bundle",
        default=True,
    )
    selected_armature: EnumProperty(
        name="Selected Armature",
        description="Armature root used for animation-oriented exports",
        items=enum_scene_armatures,
    )
    export_animations: BoolProperty(
        name="Export Animations",
        description="Include animation data in the intermediate glTF",
        default=True,
    )
    export_all_actions: BoolProperty(
        name="All Actions",
        description="Include all Blender actions when supported by Blender's glTF exporter, and export all actions as individual .uax files when exporting UAX",
        default=True,
    )
    uax_export_action: EnumProperty(
        name="Individual Action",
        description="Action to export when All Actions is disabled",
        items=enum_actions_for_export,
    )
    animation_export_path: StringProperty(
        name="Animation Export Path",
        description="Optional folder where exported UAX animation file(s) should be written",
        subtype="DIR_PATH",
        default="",
    )
    uax_template_path: StringProperty(
        name="Optional UAX Template Fallback",
        description="Optional fallback. Safest custom-export path when no imported UAX cache exists.",
        subtype="FILE_PATH",
        default="",
    )
    uax_custom_export_mode: EnumProperty(
        name="Custom UAX Mode",
        description="How to handle Actions that were not imported from a real UAX",
        items=(
            ("SCRATCH_NATIVE", "Template-Free Legacy/Native", "Use bundled legacy DAE->GR2->UAX tools first; fall back to native scratch writer only if legacy tools are unavailable"),
            ("SAFE_NATIVE", "Imported/Template Only", "Only export imported UAX actions or an optional template fallback"),
        ),
        default="SCRATCH_NATIVE",
    )
    uax_write_debug_report: BoolProperty(
        name="Write Debug Report",
        description="Write a .txt report beside skipped/experimental UAX exports explaining what data was used and why",
        default=True,
    )
    uax_custom_timing_mode: EnumProperty(
        name="Custom Action Timing",
        description="For optional template fallback this controls template retiming. Experimental scratch uses the Action range",
        items=(
            ("ACTION_RANGE", "Use Action Range", "Retarget template key times across the Blender Action frame range and write the action duration to the UAX"),
            ("TEMPLATE", "Keep Template Timing", "Use the template UAX native knot times and duration exactly"),
        ),
        default="ACTION_RANGE",
    )
    uax_export_rewrite_names: BoolProperty(
        name="Write Clip Name When Possible",
        description="Patch the native Granny animation/track-group name string when the output action name fits in the template's original string space. File name is always written regardless",
        default=True,
    )

    helper_action: EnumProperty(
        name="Action",
        description="Action to edit with the UAX Animation Helper",
        items=enum_helper_actions,
    )
    helper_bone: EnumProperty(
        name="Bone",
        description="Bone to offset uniformly across the selected action timeline",
        items=enum_helper_bones,
    )
    helper_location_offset: FloatVectorProperty(
        name="Location Offset",
        description="Add this XYZ offset to the selected bone's location curves across the whole action",
        subtype="TRANSLATION",
        size=3,
        default=(0.0, 0.0, 0.0),
        precision=5,
    )
    helper_rotation_offset: FloatVectorProperty(
        name="Rotation Offset",
        description="Add this XYZ Euler rotation offset to the selected bone's rotation across the whole action",
        subtype="EULER",
        size=3,
        default=(0.0, 0.0, 0.0),
        precision=5,
    )
    helper_apply_location: BoolProperty(
        name="Apply Location",
        description="Offset the selected bone's location curves",
        default=True,
    )
    helper_apply_rotation: BoolProperty(
        name="Apply Rotation",
        description="Offset the selected bone's quaternion rotation curves uniformly",
        default=False,
    )
    helper_create_missing_keys: BoolProperty(
        name="Create Missing Curves",
        description="If the bone has no matching curves, create start/end keys for the offset",
        default=True,
    )
    helper_auto_capture_on_apply: BoolProperty(
        name="Auto Capture Pose On Apply",
        description="Before applying, read the current pose-bone difference from the active action at the current frame and use it as the timeline-wide offset",
        default=True,
    )
    helper_ground_root_bone: StringProperty(
        name="Ground Root Bone",
        description="Bone whose Z location curve should be adjusted by Clamp Feet To Ground. Usually b_root or bone_root",
        default="b_root",
    )
    helper_ground_floor_object: PointerProperty(
        name="Floor Plane",
        description="Optional plane/object to use as the floor. The clamp projects the selected contact bones to this object's local Z=0 plane. Leave empty to use Blender world Z=0",
        type=bpy.types.Object,
    )
    helper_ground_use_mesh_contact: BoolProperty(
        name="Use Weighted Mesh Contact",
        description="Use the lowest weighted mesh vertices assigned to the chosen foot/contact bones instead of only the bone head/tail. This better matches where the visible foot actually touches the floor",
        default=True,
    )
    helper_ground_world_z_only: BoolProperty(
        name="Pure World-Z Compensation",
        description="Convert the correction into root local channels so the visible rig moves vertically in world space instead of drifting sideways when root axes are rotated",
        default=True,
    )
    helper_ground_contact_stickiness: FloatProperty(
        name="Contact Stickiness",
        description="How much the solver prefers the previous foot before switching to the other contact. Higher values reduce left/right flicker",
        default=0.025,
        min=0.0,
        max=1.0,
        precision=4,
    )
    helper_ground_use_selected_bones: BoolProperty(
        name="Use Selected Pose Bones",
        description="When enabled, selected pose bones become the contact bones. Manual fields still override when filled",
        default=True,
    )
    helper_ground_sample_every_frame: BoolProperty(
        name="Sample Every Frame",
        description="Clamp contact for every whole frame in the action range instead of only existing keyframes",
        default=True,
    )
    helper_ground_contact_mode: EnumProperty(
        name="Contact Mode",
        description="How multiple contact bones are used when clamping",
        items=(
            ("LOWEST", "Lowest Contact", "Move the root so the lowest selected foot/toe/contact point touches the floor"),
            ("AVERAGE", "Average Contact", "Move the root so the average of selected contact points touches the floor"),
        ),
        default="LOWEST",
    )
    helper_walkrun_smoothing: IntProperty(
        name="Smooth",
        description="Optional smoothing radius in frames for the unified ground solver. 0 keeps exact foot contact. Higher values reduce jitter but can soften contact",
        default=0,
        min=0,
        max=12,
    )
    helper_walkrun_max_step: FloatProperty(
        name="Max Frame Step",
        description="Maximum vertical correction change allowed per frame. 0 disables the limiter and keeps exact contact",
        default=0.0,
        min=0.0,
        max=100.0,
        precision=4,
    )
    helper_left_foot_bone: StringProperty(
        name="Left/Contact Bone A",
        description="Optional contact bone for ground clamping. Leave blank to use selected pose bones or auto-detect",
        default="",
    )
    helper_right_foot_bone: StringProperty(
        name="Right/Contact Bone B",
        description="Optional contact bone for ground clamping. Leave blank to use selected pose bones or auto-detect",
        default="",
    )
    hw2_version: BoolProperty(
        name="Halo Wars 2 Format",
        description="Pass --version hw2 to ugx.exe from-gltf",
        default=True,
    )


def get_scene_export_settings(context):
    return getattr(context.scene, "hwugx_export_settings", None)


def draw_export_settings_ui(layout, settings, *, include_hw2=True, include_header=True):
    if include_header:
        header = layout.row(align=True)
        header.label(text="Export Settings", icon="SETTINGS")

    general = layout.box()
    row = general.row(align=True)
    row.label(text="▣ General", icon="OPTIONS")
    row = general.row(align=True)
    row.prop(settings, "axis_back", text="Back")
    row.prop(settings, "axis_right", text="Right")
    general.prop(settings, "axis_up", text="Up")
    axis_hint = general.column(align=True)
    axis_hint.scale_y = 0.75
    axis_hint.label(text="Up affects glTF/UGX export. Back/Right/Up also feed template-free UAX basis when changed from the HW Suite default.", icon="ORIENTATION_GLOBAL")
    general.separator(factor=0.5)
    general.prop(settings, "export_meshes", icon="MESH_DATA")
    general.prop(settings, "export_selection_only", icon="RESTRICT_SELECT_OFF")
    general.prop(settings, "selected_armature", icon="ARMATURE_DATA")
    if include_hw2 and hasattr(settings, "hw2_version"):
        general.prop(settings, "hw2_version", icon="CHECKMARK")

    anim = layout.box()
    row = anim.row(align=True)
    row.label(text="◈ Animation (UAX)", icon="ANIM_DATA")
    anim.prop(settings, "export_animations", icon="ACTION")
    sub = anim.column(align=True)
    sub.enabled = settings.export_animations
    sub.prop(settings, "export_all_actions", icon="ACTION_TWEAK")
    if not settings.export_all_actions:
        sub.prop(settings, "uax_export_action", icon="ACTION")
    sub.prop(settings, "animation_export_path", icon="FILE_FOLDER")
    template = anim.column(align=True)
    template.enabled = settings.export_animations
    template.label(text="UAX export: imported cache first, template second, template-free writer last.", icon="FILE_TICK")
    template.prop(settings, "uax_template_path", icon="FILE")
    template.prop(settings, "uax_custom_timing_mode", icon="TIME")
    template.prop(settings, "uax_export_rewrite_names", icon="SORTALPHA")
    template.prop(settings, "uax_custom_export_mode", icon="EXPERIMENTAL")
    template.prop(settings, "uax_write_debug_report", icon="TEXT")
    hint = anim.column(align=True)
    hint.scale_y = 0.8
    if getattr(settings, "uax_custom_export_mode", "SCRATCH_NATIVE") == "EXPERIMENTAL_SCRATCH":
        hint.alert = True
        hint.label(text="Experimental Scratch can make game-unsafe UAX files. Use only for testing.", icon="ERROR")
    else:
        hint.label(text="Template-free custom Actions write a debug report instead of unsafe .uax output.", icon="INFO")


def _copy_intermediate_export_files(temp_dir: str, destination_dir: str, operator=None) -> int:
    if not destination_dir:
        return 0
    dest = bpy.path.abspath(destination_dir)
    os.makedirs(dest, exist_ok=True)
    copied = 0
    for filename in os.listdir(temp_dir):
        src = os.path.join(temp_dir, filename)
        if not os.path.isfile(src):
            continue
        # glTF separate exports .gltf, .bin, and sometimes image files. Copy the full set
        # so the animation sidecar opens cleanly outside the temporary converter folder.
        shutil.copy2(src, os.path.join(dest, filename))
        copied += 1
    if operator and copied:
        operator.report({"INFO"}, f"Copied {copied} glTF animation sidecar file(s) to {dest}")
    return copied




# -----------------------------------------------------------------------------
# Native UAX passthrough cache
# -----------------------------------------------------------------------------

def _uax_cache_text_name(action_name: str) -> str:
    return "HWUGX_UAX_CACHE_" + _sanitize_filename(action_name, "action")


def _store_raw_uax_on_action(action: bpy.types.Action, filepath: str, raw_data: bytes):
    """Preserve the exact imported UAX container for native round-trip export.

    ugx.exe currently does not support animation-specific UAX writing. The old
    HW Suite path handled UAX directly by reading the ECF/Granny animation data,
    so this add-on keeps the original UAX bytes attached to imported actions.
    Exporting an imported action can then write a real .uax file with the same
    per-animation structure and size instead of a generic 37 KB converter stub.
    """
    if not action or not raw_data:
        return
    text_name = _uax_cache_text_name(action.name)
    text = bpy.data.texts.get(text_name)
    if text is None:
        text = bpy.data.texts.new(text_name)
    else:
        text.clear()
    encoded = base64.b64encode(raw_data).decode("ascii")
    # Split lines so Blender's Text datablock remains responsive with larger UAX files.
    lines = [encoded[i:i + 120] for i in range(0, len(encoded), 120)]
    text.write("\n".join(lines))
    text.use_fake_user = True
    action["uax_raw_cache_text"] = text.name
    action["uax_original_path"] = filepath
    action["uax_original_size"] = len(raw_data)
    action["uax_original_sha1"] = hashlib.sha1(raw_data).hexdigest()
    action["uax_export_mode"] = "native_patch"


def _read_raw_uax_from_action(action: bpy.types.Action) -> bytes | None:
    if not action:
        return None
    text_name = str(action.get("uax_raw_cache_text", ""))
    if text_name:
        text = bpy.data.texts.get(text_name)
        if text:
            try:
                encoded = "".join(line.body.strip() for line in text.lines)
                raw = base64.b64decode(encoded.encode("ascii"), validate=False)
                if raw:
                    return raw
            except Exception:
                pass
    # Fallback for actions imported before v3.5 if their original file still exists.
    original_path = bpy.path.abspath(str(action.get("uax_original_path", "")))
    if original_path and os.path.isfile(original_path):
        try:
            with open(original_path, "rb") as f:
                return f.read()
        except Exception:
            return None
    return None



# -----------------------------------------------------------------------------
# Native editable UAX patch export
# -----------------------------------------------------------------------------

def _iter_ecf_chunk_infos(raw: bytes):
    """Yield (chunk_index, id, data_offset, data_length, header_offset)."""
    if not raw or len(raw) < 32:
        return
    try:
        if struct.unpack(">I", raw[:4])[0] != 0xDABA7737:
            return
        num_chunks = struct.unpack(">H", raw[16:18])[0]
        for i in range(num_chunks):
            header_off = 32 + (24 * i)
            if header_off + 16 > len(raw):
                break
            chunk_id, chunk_offs, chunk_len = struct.unpack(">QII", raw[header_off:header_off + 16])
            if chunk_offs + chunk_len <= len(raw):
                yield i, chunk_id, chunk_offs, chunk_len, header_off
    except Exception:
        return


def _find_uax_granny_chunk(raw: bytes):
    for info in _iter_ecf_chunk_infos(raw) or []:
        _idx, chunk_id, chunk_offs, chunk_len, _header = info
        if chunk_id == 0x700:
            return chunk_offs, chunk_len
    return None


def _get_granny_first_animation_ptr(granny: bytes) -> int | None:
    try:
        if len(granny) < 132:
            return None
        animations_len, animations_offs = struct.unpack("<IQ", granny[120:132])
        if animations_len < 1 or animations_offs + 8 > len(granny):
            return None
        ptr = struct.unpack("<Q", granny[animations_offs:animations_offs + 8])[0]
        if ptr <= 0 or ptr + 16 > len(granny):
            return None
        return ptr
    except Exception:
        return None


def _get_granny_animation_duration_seconds(granny: bytes) -> float:
    ptr = _get_granny_first_animation_ptr(granny)
    if ptr is None:
        return 0.0
    try:
        duration, _timestep = struct.unpack("<ff", granny[ptr + 8:ptr + 16])
        return max(0.0, float(duration))
    except Exception:
        return 0.0


def _action_frame_range(action: bpy.types.Action) -> tuple[float, float]:
    try:
        start, end = action.frame_range
        start, end = float(start), float(end)
    except Exception:
        start, end = 1.0, float(bpy.context.scene.frame_end)
    if end < start:
        start, end = end, start
    if abs(end - start) < 0.001:
        end = start + 1.0
    return start, end


def _export_frame_from_uax_seconds(seconds: float, fps: float, action: bpy.types.Action | None = None, *, source_duration: float = 0.0, retime_to_action: bool = False) -> float:
    if retime_to_action and action is not None and source_duration > 0.00001:
        start, end = _action_frame_range(action)
        ratio = max(0.0, min(1.0, float(seconds) / float(source_duration)))
        return start + ratio * (end - start)
    return _frame_from_uax_seconds(seconds, fps)


def _float_to_truncated_granny_scale(value: float) -> int:
    try:
        bits = struct.unpack("<I", struct.pack("<f", float(value)))[0]
        return (bits >> 16) & 0xFFFF
    except Exception:
        return 0


def _scale_granny_curve_knot_times(granny: bytearray, key_type: str, object_offs: int, scale_factor: float, visited: set[int], *, is_position: bool = False) -> int:
    if object_offs in visited or abs(scale_factor - 1.0) < 0.000001 or scale_factor <= 0.0:
        return 0
    visited.add(object_offs)
    scaled = 0
    try:
        if key_type == "CurveDataHeader_DaK32fC32f":
            if is_position:
                _header, knot_count, knot_offset, _null1, _control_count, _control_offset, _null2 = struct.unpack("<IIIIIII", granny[object_offs:object_offs + 28])
            else:
                _header, knot_count, knot_offset, _control_count, _control_offset = struct.unpack("<IIQIQ", granny[object_offs:object_offs + 28])
            for k in range(knot_count):
                off = knot_offset + k * 4
                if off + 4 <= len(granny):
                    old = struct.unpack("<f", granny[off:off + 4])[0]
                    struct.pack_into("<f", granny, off, float(old) * float(scale_factor))
                    scaled += 1
        elif key_type in {"CurveDataHeader_D4nK8uC7u", "CurveDataHeader_D4nK16uC15u"}:
            if object_offs + 8 <= len(granny):
                old_scale = struct.unpack("<f", granny[object_offs + 4:object_offs + 8])[0]
                if old_scale != 0.0:
                    struct.pack_into("<f", granny, object_offs + 4, float(old_scale) / float(scale_factor))
                    scaled += 1
        elif key_type in {"CurveDataHeader_D3K16uC16u", "CurveDataHeader_D3I1K16uC16u", "CurveDataHeader_D3K8uC8u", "CurveDataHeader_D3I1K8uC8u"}:
            if object_offs + 4 <= len(granny):
                header, trunc = struct.unpack("<HH", granny[object_offs:object_offs + 4])
                shifted = struct.unpack("<f", struct.pack("<I", int(trunc) << 16))[0]
                if shifted != 0.0:
                    new_trunc = _float_to_truncated_granny_scale(float(shifted) / float(scale_factor))
                    if new_trunc:
                        struct.pack_into("<HH", granny, object_offs, header, new_trunc)
                        scaled += 1
    except Exception as exc:
        print(f"[UGX Pipeline] Failed scaling knot times {key_type}: {exc}")
    return scaled


def _patch_string_in_place(granny: bytearray, offset: int, new_text: str) -> bool:
    try:
        old = read_c_string(granny, offset)
        if not old:
            return False
        encoded = str(new_text).encode("utf-8", errors="ignore")
        if len(encoded) > len(old.encode("utf-8", errors="ignore")):
            return False
        granny[offset:offset + len(encoded)] = encoded
        # Keep the old allocation and clear leftover characters so the game/editor does not see stale suffixes.
        clear_start = offset + len(encoded)
        clear_end = offset + len(old.encode("utf-8", errors="ignore"))
        if clear_end > clear_start:
            granny[clear_start:clear_end] = b"\x00" * (clear_end - clear_start)
        return True
    except Exception:
        return False


def _patch_granny_clip_names(granny: bytearray, clip_name: str) -> int:
    patched = 0
    try:
        ptr = _get_granny_first_animation_ptr(granny)
        if ptr is not None and ptr + 8 <= len(granny):
            name_offs = struct.unpack("<Q", granny[ptr:ptr + 8])[0]
            if name_offs and name_offs < len(granny):
                patched += 1 if _patch_string_in_place(granny, name_offs, clip_name) else 0
        track_groups_len, track_groups_offs = struct.unpack("<IQ", granny[108:120])
        for i in range(track_groups_len):
            group_ptr_offs = track_groups_offs + i * 172
            if group_ptr_offs + 8 > len(granny):
                continue
            group_ptr = struct.unpack("<Q", granny[group_ptr_offs:group_ptr_offs + 8])[0]
            if group_ptr and group_ptr + 8 <= len(granny):
                group_name_offs = struct.unpack("<Q", granny[group_ptr:group_ptr + 8])[0]
                if group_name_offs and group_name_offs < len(granny):
                    patched += 1 if _patch_string_in_place(granny, group_name_offs, clip_name) else 0
    except Exception:
        pass
    return patched


def _retime_granny_animation_to_action(granny: bytearray, action: bpy.types.Action, fps: float, source_duration: float) -> dict:
    stats = {"duration": 0, "knots": 0}
    if source_duration <= 0.00001:
        return stats
    start, end = _action_frame_range(action)
    target_duration = max(0.001, (end - start) / max(float(fps), 0.0001))
    scale_factor = target_duration / source_duration
    if scale_factor <= 0.0:
        return stats
    try:
        ptr = _get_granny_first_animation_ptr(granny)
        if ptr is not None and ptr + 16 <= len(granny):
            old_duration, old_step = struct.unpack("<ff", granny[ptr + 8:ptr + 16])
            struct.pack_into("<ff", granny, ptr + 8, float(target_duration), float(old_step))
            stats["duration"] = 1
    except Exception as exc:
        print(f"[UGX Pipeline] Failed patching animation duration: {exc}")

    visited: set[int] = set()
    try:
        track_groups_len, track_groups_offs = struct.unpack("<IQ", granny[108:120])
        for i in range(track_groups_len):
            group_ptr_offs = track_groups_offs + i * 172
            if group_ptr_offs + 8 > len(granny):
                continue
            group_ptr = struct.unpack("<Q", granny[group_ptr_offs:group_ptr_offs + 8])[0]
            if group_ptr + 128 > len(granny):
                continue
            transform_tracks_len, transform_tracks_offs = struct.unpack("<IQ", granny[group_ptr + 20:group_ptr + 32])
            for t in range(transform_tracks_len):
                cur = transform_tracks_offs + t * 60
                if cur + 60 > len(granny):
                    continue
                try:
                    rot_type_ptr, rot_obj = struct.unpack("<QQ", granny[cur + 12:cur + 28])
                    rot_type_name = read_c_string(granny, struct.unpack("<IQ", granny[rot_type_ptr:rot_type_ptr + 12])[1])
                    stats["knots"] += _scale_granny_curve_knot_times(granny, rot_type_name, rot_obj, scale_factor, visited, is_position=False)
                except Exception:
                    pass
                try:
                    pos_type_ptr, pos_obj = struct.unpack("<QQ", granny[cur + 28:cur + 44])
                    pos_type_name = read_c_string(granny, struct.unpack("<IQ", granny[pos_type_ptr:pos_type_ptr + 12])[1])
                    stats["knots"] += _scale_granny_curve_knot_times(granny, pos_type_name, pos_obj, scale_factor, visited, is_position=True)
                except Exception:
                    pass
    except Exception as exc:
        print(f"[UGX Pipeline] Failed retiming curve knots: {exc}")
    return stats


def _read_template_uax_from_settings(settings) -> tuple[bytes | None, str]:
    path = ""
    try:
        path = bpy.path.abspath(getattr(settings, "uax_template_path", "") or "")
    except Exception:
        path = ""
    if not path or not os.path.isfile(path):
        return None, path
    try:
        with open(path, "rb") as f:
            return f.read(), path
    except Exception:
        return None, path


def _fcurve_value(action: bpy.types.Action, bone_name: str, prop: str, index: int, frame: float, default: float) -> float:
    fc = find_action_fcurve(action, f'pose.bones["{bone_name}"].{prop}', index)
    if fc is None:
        return float(default)
    try:
        return float(fc.evaluate(float(frame)))
    except Exception:
        return float(default)


def _action_location_at(action: bpy.types.Action, bone_name: str, frame: float) -> Vector | None:
    base_path = f'pose.bones["{bone_name}"].location'
    if not any(getattr(fc, "data_path", "") == base_path for fc in iter_action_fcurves(action)):
        return None
    return Vector((
        _fcurve_value(action, bone_name, "location", 0, frame, 0.0),
        _fcurve_value(action, bone_name, "location", 1, frame, 0.0),
        _fcurve_value(action, bone_name, "location", 2, frame, 0.0),
    ))


def _action_scale_at(action: bpy.types.Action, bone_name: str, frame: float) -> Vector | None:
    """Evaluate pose bone scale curves from a Blender Action.

    Native UAX transform tracks contain rotation, position, and scale-shear. Earlier
    scratch writers only wrote a guessed constant scale-shear value; that produced
    files that loaded but could appear static in HW2 when the visible motion was
    actually authored as bone scale. This mirrors the old custom-UAX behavior by
    using the Blender Action scale keys directly when they exist.
    """
    base_path = f'pose.bones["{bone_name}"].scale'
    if not any(getattr(fc, "data_path", "") == base_path for fc in iter_action_fcurves(action)):
        return None
    return Vector((
        _fcurve_value(action, bone_name, "scale", 0, frame, 1.0),
        _fcurve_value(action, bone_name, "scale", 1, frame, 1.0),
        _fcurve_value(action, bone_name, "scale", 2, frame, 1.0),
    ))


def _action_has_scale_curves(action: bpy.types.Action, bone_name: str) -> bool:
    base_path = f'pose.bones["{bone_name}"].scale'
    return any(getattr(fc, "data_path", "") == base_path for fc in iter_action_fcurves(action))


def _action_quaternion_at(action: bpy.types.Action, bone_name: str, frame: float) -> Quaternion | None:
    base_path = f'pose.bones["{bone_name}"].rotation_quaternion'
    if not any(getattr(fc, "data_path", "") == base_path for fc in iter_action_fcurves(action)):
        return None
    q = Quaternion((
        _fcurve_value(action, bone_name, "rotation_quaternion", 0, frame, 1.0),
        _fcurve_value(action, bone_name, "rotation_quaternion", 1, frame, 0.0),
        _fcurve_value(action, bone_name, "rotation_quaternion", 2, frame, 0.0),
        _fcurve_value(action, bone_name, "rotation_quaternion", 3, frame, 0.0),
    ))
    try:
        q.normalize()
    except Exception:
        q = Quaternion((1.0, 0.0, 0.0, 0.0))
    return q


def _frame_from_uax_seconds(seconds: float, fps: float) -> float:
    # Matches import: frame 0 seconds was clamped to Blender frame 1.
    return max(1.0, float(seconds) * float(fps))


def _uax_vec_from_action_location(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, *, is_vda: bool, axis_q: Quaternion, import_scale: float, legacy_child_z_flip: bool) -> Vector | None:
    loc = _action_location_at(action, bone_name, frame)
    if loc is None or bone_name not in armature.data.bones:
        return None
    bone = armature.data.bones[bone_name]
    final = Vector(loc) + bone.head
    if legacy_child_z_flip and bone.parent and not is_vda:
        final.z = -final.z
    scale = float(import_scale) if abs(float(import_scale)) > 0.000001 else 1.0
    final = final / scale
    try:
        if abs(axis_q.angle) >= 0.000001:
            final = axis_q.inverted() @ final
    except Exception:
        pass
    return final


def _uax_quat_from_action_rotation(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, *, axis_q: Quaternion) -> Quaternion | None:
    q_blender = _action_quaternion_at(action, bone_name, frame)
    if q_blender is None or bone_name not in armature.data.bones:
        return None
    bone = armature.data.bones[bone_name]
    q_uax = bone.matrix.to_quaternion() @ q_blender
    try:
        if abs(axis_q.angle) >= 0.000001:
            q_uax = axis_q.inverted() @ q_uax @ axis_q
    except Exception:
        pass
    try:
        q_uax.normalize()
    except Exception:
        q_uax = Quaternion((1.0, 0.0, 0.0, 0.0))
    return q_uax


def _clamp_int(value, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(round(float(value)))))
    except Exception:
        return minimum


def _encode_scalar_quantized(value: float, scale: float, offset: float, maximum: int) -> int:
    if abs(scale) < 0.000000001:
        return 0
    return _clamp_int((float(value) - float(offset)) / float(scale), 0, maximum)


def _encode_vec_quantized(vec: Vector, scales, offsets, maximum: int) -> tuple[int, int, int]:
    return tuple(_encode_scalar_quantized(vec[i], scales[i], offsets[i], maximum) for i in range(3))


def _encode_vec_quantized_i1(vec: Vector, scales, offsets, maximum: int) -> int:
    qs = []
    for i in range(3):
        if abs(scales[i]) >= 0.000000001:
            qs.append((float(vec[i]) - float(offsets[i])) / float(scales[i]))
    if not qs:
        return 0
    return _clamp_int(sum(qs) / len(qs), 0, maximum)


def _patch_position_curve_from_action(granny: bytearray, key_type: str, object_offs: int, action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, *, is_vda: bool, fps: float, axis_q: Quaternion, import_scale: float, legacy_child_z_flip: bool, source_duration: float = 0.0, retime_to_action: bool = False) -> tuple[int, int]:
    """Patch an existing Granny position curve in-place from the Blender Action.

    Returns (patched_controls, locked_controls). Locked controls are curve types
    that cannot be changed without rebuilding the Granny object graph, such as
    DaIdentity.
    """
    patched = 0
    locked = 0
    try:
        if key_type == "CurveDataHeader_DaK32fC32f":
            _header, knot_count, knot_offset, _null1, control_count, control_offset, _null2 = struct.unpack("<IIIIIII", granny[object_offs: object_offs + 28])
            count = min(knot_count, control_count)
            for k in range(count):
                knot = struct.unpack("<f", granny[knot_offset + k * 4:knot_offset + k * 4 + 4])[0]
                frame = _export_frame_from_uax_seconds(knot, fps, action, source_duration=source_duration, retime_to_action=retime_to_action)
                vec = _uax_vec_from_action_location(action, armature, bone_name, frame, is_vda=is_vda, axis_q=axis_q, import_scale=import_scale, legacy_child_z_flip=legacy_child_z_flip)
                if vec is None:
                    continue
                struct.pack_into("<fff", granny, control_offset + k * 12, float(vec.x), float(vec.y), float(vec.z))
                patched += 1
        elif key_type == "CurveDataHeader_D3Constant32f":
            vec = _uax_vec_from_action_location(action, armature, bone_name, (_action_frame_range(action)[0] if retime_to_action else 1.0), is_vda=is_vda, axis_q=axis_q, import_scale=import_scale, legacy_child_z_flip=legacy_child_z_flip)
            if vec is not None and object_offs + 16 <= len(granny):
                struct.pack_into("<HHfff", granny, object_offs, *struct.unpack("<HH", granny[object_offs:object_offs + 4]), float(vec.x), float(vec.y), float(vec.z))
                patched += 1
        elif key_type == "CurveDataHeader_D3K16uC16u":
            _header, one_over_knot_scale_trunc = struct.unpack("<HH", granny[object_offs: object_offs + 4])
            scales = struct.unpack("<fff", granny[object_offs + 4: object_offs + 16])
            offsets = struct.unpack("<fff", granny[object_offs + 16: object_offs + 28])
            knot_control_count, knot_control_offset = struct.unpack("<II", granny[object_offs + 28: object_offs + 36])
            knot_count = int(knot_control_count / 4)
            shifted = struct.unpack("<f", struct.pack("<I", one_over_knot_scale_trunc << 16))[0]
            if shifted == 0:
                return patched, locked
            for k in range(knot_count):
                knot_data = struct.unpack("<H", granny[knot_control_offset + k * 2:knot_control_offset + k * 2 + 2])[0]
                frame = _export_frame_from_uax_seconds(knot_data / shifted, fps, action, source_duration=source_duration, retime_to_action=retime_to_action)
                vec = _uax_vec_from_action_location(action, armature, bone_name, frame, is_vda=is_vda, axis_q=axis_q, import_scale=import_scale, legacy_child_z_flip=legacy_child_z_flip)
                if vec is None:
                    continue
                packed = _encode_vec_quantized(vec, scales, offsets, 0xFFFF)
                struct.pack_into("<HHH", granny, knot_control_offset + knot_count * 2 + k * 6, *packed)
                patched += 1
        elif key_type == "CurveDataHeader_D3I1K16uC16u":
            _header, one_over_knot_scale_trunc = struct.unpack("<HH", granny[object_offs: object_offs + 4])
            scales = struct.unpack("<fff", granny[object_offs + 4: object_offs + 16])
            offsets = struct.unpack("<fff", granny[object_offs + 16: object_offs + 28])
            knot_control_count, knot_control_offset = struct.unpack("<II", granny[object_offs + 28: object_offs + 36])
            knot_count = int(knot_control_count / 2)
            shifted = struct.unpack("<f", struct.pack("<I", one_over_knot_scale_trunc << 16))[0]
            if shifted == 0:
                return patched, locked
            for k in range(knot_count):
                knot_data = struct.unpack("<H", granny[knot_control_offset + k * 2:knot_control_offset + k * 2 + 2])[0]
                frame = _export_frame_from_uax_seconds(knot_data / shifted, fps, action, source_duration=source_duration, retime_to_action=retime_to_action)
                vec = _uax_vec_from_action_location(action, armature, bone_name, frame, is_vda=is_vda, axis_q=axis_q, import_scale=import_scale, legacy_child_z_flip=legacy_child_z_flip)
                if vec is None:
                    continue
                packed = _encode_vec_quantized_i1(vec, scales, offsets, 0xFFFF)
                struct.pack_into("<H", granny, knot_control_offset + knot_count * 2 + k * 2, packed)
                patched += 1
        elif key_type == "CurveDataHeader_D3K8uC8u":
            _header, one_over_knot_scale_trunc = struct.unpack("<HH", granny[object_offs: object_offs + 4])
            scales = struct.unpack("<fff", granny[object_offs + 4: object_offs + 16])
            offsets = struct.unpack("<fff", granny[object_offs + 16: object_offs + 28])
            knot_control_count, knot_control_offset = struct.unpack("<II", granny[object_offs + 28: object_offs + 36])
            knot_count = int(knot_control_count / 4)
            shifted = struct.unpack("<f", struct.pack("<I", one_over_knot_scale_trunc << 16))[0]
            if shifted == 0:
                return patched, locked
            for k in range(knot_count):
                knot_data = struct.unpack("<B", granny[knot_control_offset + k:knot_control_offset + k + 1])[0]
                frame = _export_frame_from_uax_seconds(knot_data / shifted, fps, action, source_duration=source_duration, retime_to_action=retime_to_action)
                vec = _uax_vec_from_action_location(action, armature, bone_name, frame, is_vda=is_vda, axis_q=axis_q, import_scale=import_scale, legacy_child_z_flip=legacy_child_z_flip)
                if vec is None:
                    continue
                packed = _encode_vec_quantized(vec, scales, offsets, 0xFF)
                struct.pack_into("<BBB", granny, knot_control_offset + knot_count + k * 3, *packed)
                patched += 1
        elif key_type == "CurveDataHeader_D3I1K8uC8u":
            _header, one_over_knot_scale_trunc = struct.unpack("<HH", granny[object_offs: object_offs + 4])
            scales = struct.unpack("<fff", granny[object_offs + 4: object_offs + 16])
            offsets = struct.unpack("<fff", granny[object_offs + 16: object_offs + 28])
            knot_control_count, knot_control_offset = struct.unpack("<II", granny[object_offs + 28: object_offs + 36])
            knot_count = int(knot_control_count / 4)
            shifted = struct.unpack("<f", struct.pack("<I", one_over_knot_scale_trunc << 16))[0]
            if shifted == 0:
                return patched, locked
            for k in range(knot_count):
                knot_data = struct.unpack("<B", granny[knot_control_offset + k:knot_control_offset + k + 1])[0]
                frame = _export_frame_from_uax_seconds(knot_data / shifted, fps, action, source_duration=source_duration, retime_to_action=retime_to_action)
                vec = _uax_vec_from_action_location(action, armature, bone_name, frame, is_vda=is_vda, axis_q=axis_q, import_scale=import_scale, legacy_child_z_flip=legacy_child_z_flip)
                if vec is None:
                    continue
                packed = _encode_vec_quantized_i1(vec, scales, offsets, 0xFF)
                struct.pack_into("<B", granny, knot_control_offset + knot_count + k, packed)
                patched += 1
        elif key_type == "CurveDataHeader_DaIdentity":
            locked += 1
        else:
            locked += 1
    except Exception as exc:
        print(f"[UGX Pipeline] Failed to patch position curve {bone_name} ({key_type}): {exc}")
    return patched, locked


def _quat_scale_offset_tables(entries: int, bits: int):
    scale_table = (
        1.4142135, 0.70710677, 0.35355338, 0.35355338,
        0.35355338, 0.17677669, 0.17677669, 0.17677669,
        -1.4142135, -0.70710677, -0.35355338, -0.35355338,
        -0.35355338, -0.17677669, -0.17677669, -0.17677669,
    )
    offset_table = (
        -0.70710677, -0.35355338, -0.53033006, -0.17677669,
        0.17677669, -0.17677669, -0.088388346, 0.0,
        0.70710677, 0.35355338, 0.53033006, 0.17677669,
        -0.17677669, 0.17677669, 0.088388346, -0.0,
    )
    multiplier = 0.0078740157 if bits == 8 else 0.000030518509
    scales = tuple(scale_table[(entries >> shift) & 0x0F] * multiplier for shift in (0, 4, 8, 12))
    offsets = tuple(offset_table[(entries >> shift) & 0x0F] for shift in (0, 4, 8, 12))
    return scales, offsets


def _pack_compressed_quat_components(q: Quaternion, entries: int, bits: int):
    try:
        q.normalize()
    except Exception:
        q = Quaternion((1.0, 0.0, 0.0, 0.0))
    qv = [float(q.x), float(q.y), float(q.z), float(q.w)]
    sw1 = max(range(4), key=lambda i: abs(qv[i]))
    sw2, sw3, sw4 = ((sw1 + 1) & 3), ((sw1 + 2) & 3), ((sw1 + 3) & 3)
    scales, offsets = _quat_scale_offset_tables(entries, bits)
    max_val = 0x7F if bits == 8 else 0x7FFF
    va = _encode_scalar_quantized(qv[sw2], scales[sw2], offsets[sw2], max_val)
    vb = _encode_scalar_quantized(qv[sw3], scales[sw3], offsets[sw3], max_val)
    vc = _encode_scalar_quantized(qv[sw4], scales[sw4], offsets[sw4], max_val)
    sign = qv[sw1] < 0.0
    if bits == 8:
        a = va | (0x80 if sign else 0)
        b = vb | (0x80 if (sw1 & 0x2) else 0)
        c = vc | (0x80 if (sw1 & 0x1) else 0)
    else:
        a = va | (0x8000 if sign else 0)
        b = vb | (0x8000 if (sw1 & 0x2) else 0)
        c = vc | (0x8000 if (sw1 & 0x1) else 0)
    return a, b, c


def _patch_rotation_curve_from_action(granny: bytearray, key_type: str, object_offs: int, action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, *, fps: float, axis_q: Quaternion, source_duration: float = 0.0, retime_to_action: bool = False) -> tuple[int, int]:
    patched = 0
    locked = 0
    try:
        if key_type == "CurveDataHeader_DaK32fC32f":
            _header, knot_count, knot_offset, control_count, control_offset = struct.unpack("<IIQIQ", granny[object_offs: object_offs + 28])
            count = min(knot_count, control_count)
            for k in range(count):
                knot = struct.unpack("<f", granny[knot_offset + k * 4:knot_offset + k * 4 + 4])[0]
                frame = _export_frame_from_uax_seconds(knot, fps, action, source_duration=source_duration, retime_to_action=retime_to_action)
                q = _uax_quat_from_action_rotation(action, armature, bone_name, frame, axis_q=axis_q)
                if q is None:
                    continue
                struct.pack_into("<ffff", granny, control_offset + k * 16, float(q.x), float(q.y), float(q.z), float(q.w))
                patched += 1
        elif key_type == "CurveDataHeader_D4Constant32f":
            q = _uax_quat_from_action_rotation(action, armature, bone_name, (_action_frame_range(action)[0] if retime_to_action else 1.0), axis_q=axis_q)
            if q is not None and object_offs + 20 <= len(granny):
                h0, h1 = struct.unpack("<HH", granny[object_offs:object_offs + 4])
                struct.pack_into("<HHffff", granny, object_offs, h0, h1, float(q.x), float(q.y), float(q.z), float(q.w))
                patched += 1
        elif key_type == "CurveDataHeader_D4nK8uC7u":
            _header, entries, one_over_knot_scale, knot_control_count, knots_control_offset = struct.unpack("<HHfIQ", granny[object_offs: object_offs + 20])
            knot_count = int(knot_control_count / 4)
            if one_over_knot_scale == 0:
                return patched, locked
            for k in range(knot_count):
                knot_data = struct.unpack("<B", granny[knots_control_offset + k:knots_control_offset + k + 1])[0]
                frame = _export_frame_from_uax_seconds(knot_data / one_over_knot_scale, fps, action, source_duration=source_duration, retime_to_action=retime_to_action)
                q = _uax_quat_from_action_rotation(action, armature, bone_name, frame, axis_q=axis_q)
                if q is None:
                    continue
                a, b, c = _pack_compressed_quat_components(q, entries, 8)
                struct.pack_into("<BBB", granny, knots_control_offset + knot_count + k * 3, a, b, c)
                patched += 1
        elif key_type == "CurveDataHeader_D4nK16uC15u":
            _header, entries, one_over_knot_scale, knot_control_count, knots_control_offset = struct.unpack("<HHfIQ", granny[object_offs: object_offs + 20])
            knot_count = int(knot_control_count / 4)
            if one_over_knot_scale == 0:
                return patched, locked
            for k in range(knot_count):
                knot_data = struct.unpack("<H", granny[knots_control_offset + k * 2:knots_control_offset + k * 2 + 2])[0]
                frame = _export_frame_from_uax_seconds(knot_data / one_over_knot_scale, fps, action, source_duration=source_duration, retime_to_action=retime_to_action)
                q = _uax_quat_from_action_rotation(action, armature, bone_name, frame, axis_q=axis_q)
                if q is None:
                    continue
                a, b, c = _pack_compressed_quat_components(q, entries, 16)
                struct.pack_into("<HHH", granny, knots_control_offset + knot_count * 2 + k * 6, a, b, c)
                patched += 1
        elif key_type == "CurveDataHeader_DaIdentity":
            locked += 1
        else:
            locked += 1
    except Exception as exc:
        print(f"[UGX Pipeline] Failed to patch rotation curve {bone_name} ({key_type}): {exc}")
    return patched, locked



def _patch_scale_shear_curve_from_action(granny: bytearray, key_type: str, object_offs: int, action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, *, fps: float, source_duration: float = 0.0, retime_to_action: bool = False) -> tuple[int, int]:
    """Patch a native Granny scale-shear curve from Blender Action scale keys.

    This is essential for old-plugin style custom UAX animations where the visible
    in-game motion is authored as root/bone scale. If the Action has no scale
    curves for the bone, preserve the existing native scale curve instead of
    forcing identity.
    """
    patched = 0
    locked = 0
    if not _action_has_scale_curves(action, bone_name):
        return patched, locked
    try:
        if key_type == "CurveDataHeader_DaK32fC32f":
            _header, knot_count, knot_offset, _null1, control_count, control_offset, _null2 = struct.unpack("<IIIIIII", granny[object_offs: object_offs + 28])
            # Scale-shear has 9 controls per key in the known-good generated files.
            key_count = min(int(knot_count), int(control_count // 9) if control_count >= 9 else int(knot_count))
            for k in range(key_count):
                knot = struct.unpack("<f", granny[knot_offset + k * 4:knot_offset + k * 4 + 4])[0]
                frame = _export_frame_from_uax_seconds(knot, fps, action, source_duration=source_duration, retime_to_action=retime_to_action)
                ss = _scratch_pose_scale_shear(action, armature, bone_name, frame)
                struct.pack_into("<fffffffff", granny, control_offset + k * 36, *[float(v) for v in ss])
                patched += 1
        elif key_type == "CurveDataHeader_DaIdentity":
            locked += 1
        else:
            # Other scale-shear encodings are left untouched for safety.
            locked += 1
    except Exception as exc:
        print(f"[UGX Pipeline] Failed to patch scale-shear curve {bone_name} ({key_type}): {exc}")
    return patched, locked

def _patch_granny_animation_chunk_from_action(granny: bytearray, action: bpy.types.Action, armature: bpy.types.Object, *, retime_to_action: bool = False, rewrite_clip_name: bool = False) -> dict:
    """Rewrite existing Granny curve controls from Blender Action values.

    This is not a generic Granny authoring library. It intentionally preserves the
    original UAX/Granny object graph, curve types, key counts, offsets, chunk sizes,
    and ECF container layout, then only replaces control values at the original
    curve sample times. That is the game-safe path for edited imported UAX clips.
    """
    stats = {"rot": 0, "pos": 0, "locked": 0, "preserved": 0, "tracks": 0, "missing": 0, "retime_duration": 0, "retime_knots": 0, "names": 0}
    source_duration = _get_granny_animation_duration_seconds(granny)
    if not action or not armature or armature.type != "ARMATURE":
        return stats
    fps = bpy.context.scene.render.fps / max(bpy.context.scene.render.fps_base, 0.0001)
    axis_q = _uax_axis_conversion_quat(str(action.get("uax_import_axis_correction", "LEGACY_RAW_SAFE")))
    legacy_child_z_flip = bool(action.get("uax_import_legacy_child_z_flip", True))
    child_translation_mode = str(action.get("uax_import_child_translation_mode", "ALL_BONES"))
    granny_root_mode = str(action.get("uax_import_granny_root_mode", "LOCK_ROT_LOC"))
    root_stabilize_mode = str(action.get("uax_import_root_stabilize_mode", "NONE"))
    try:
        import_scale = float(action.get("uax_import_scale", armature.get("hwugx_import_scale", 1.0)))
    except Exception:
        import_scale = 1.0

    if len(granny) < 132:
        return stats
    try:
        track_groups_len, track_groups_offs = struct.unpack("<IQ", granny[108:120])
    except Exception:
        return stats

    track_group_size = 172
    transform_track_size = 60
    for i in range(track_groups_len):
        group_ptr_offs = track_groups_offs + i * track_group_size
        if group_ptr_offs + 8 > len(granny):
            continue
        cur_group = struct.unpack("<Q", granny[group_ptr_offs:group_ptr_offs + 8])[0]
        if cur_group + 128 > len(granny):
            continue
        try:
            transform_tracks_len, transform_tracks_offs = struct.unpack("<IQ", granny[cur_group + 20:cur_group + 32])
            flags = struct.unpack("<I", granny[cur_group + 124:cur_group + 128])[0]
        except Exception:
            continue
        is_vda = bool(flags & 4)
        for t in range(transform_tracks_len):
            cur = transform_tracks_offs + t * transform_track_size
            if cur + transform_track_size > len(granny):
                continue
            name_offs, _track_flags = struct.unpack("<QI", granny[cur:cur + 12])
            bone_name = read_c_string(granny, name_offs)
            if not bone_name or bone_name not in armature.data.bones:
                stats["missing"] += 1
                continue
            stats["tracks"] += 1
            bone = armature.data.bones[bone_name]
            is_granny_root = _is_granny_root_bone_name(bone_name)
            is_motion_root = _is_motion_root_bone_name(bone_name)
            is_pelvis = _is_pelvis_bone_name(bone_name)
            is_root_bone = bone.parent is None or _is_game_root_bone_name(bone_name)

            # Import-only stabilizers are for making the clip readable on the
            # Blender/glTF rig. They must NOT overwrite the original engine
            # control/root curves during export, or the UAX can fail in-game.
            skip_rot = False
            skip_pos = False
            if is_granny_root and granny_root_mode in {"LOCK_ROT", "LOCK_ROT_LOC"}:
                skip_rot = True
            if is_granny_root and granny_root_mode == "LOCK_ROT_LOC":
                skip_pos = True
            if is_motion_root and root_stabilize_mode in {"LOCK_GAME_ROOT_ROT", "LOCK_GAME_ROOT_ROT_LOC", "LOCK_GAME_ROOT_AND_PELVIS_LOC"}:
                skip_rot = True
            if is_motion_root and root_stabilize_mode in {"LOCK_GAME_ROOT_ROT_LOC", "LOCK_GAME_ROOT_AND_PELVIS_LOC"}:
                skip_pos = True
            if is_pelvis and root_stabilize_mode == "LOCK_GAME_ROOT_AND_PELVIS_LOC":
                skip_pos = True
            if child_translation_mode == "NONE":
                skip_pos = True
            elif child_translation_mode == "ROOT_ONLY" and not is_root_bone:
                skip_pos = True

            # Rotation curve type/object.
            try:
                rot_curve_type_offs, rot_curve_object_offs = struct.unpack("<QQ", granny[cur + 12:cur + 28])
                rot_type_name_offs = struct.unpack("<IQ", granny[rot_curve_type_offs:rot_curve_type_offs + 12])[1]
                rot_curve_type = read_c_string(granny, rot_type_name_offs)
                if skip_rot:
                    stats["preserved"] += 1
                else:
                    patched, locked = _patch_rotation_curve_from_action(granny, rot_curve_type, rot_curve_object_offs, action, armature, bone_name, fps=fps, axis_q=axis_q, source_duration=source_duration, retime_to_action=retime_to_action)
                    stats["rot"] += patched
                    stats["locked"] += locked
            except Exception as exc:
                print(f"[UGX Pipeline] Could not patch rotation track {bone_name}: {exc}")

            # Position curve type/object.
            try:
                pos_curve_type_offs, pos_curve_object_offs = struct.unpack("<QQ", granny[cur + 28:cur + 44])
                pos_type_name_offs = struct.unpack("<IQ", granny[pos_curve_type_offs:pos_curve_type_offs + 12])[1]
                pos_curve_type = read_c_string(granny, pos_type_name_offs)
                if skip_pos:
                    stats["preserved"] += 1
                else:
                    patched, locked = _patch_position_curve_from_action(granny, pos_curve_type, pos_curve_object_offs, action, armature, bone_name, is_vda=is_vda, fps=fps, axis_q=axis_q, import_scale=import_scale, legacy_child_z_flip=legacy_child_z_flip, source_duration=source_duration, retime_to_action=retime_to_action)
                    stats["pos"] += patched
                    stats["locked"] += locked
            except Exception as exc:
                print(f"[UGX Pipeline] Could not patch position track {bone_name}: {exc}")

            # Scale-shear curve type/object. Old custom UAX exports used this for
            # visible scale animations, so keep it in sync with Blender Action scale keys.
            try:
                scl_curve_type_offs, scl_curve_object_offs = struct.unpack("<QQ", granny[cur + 44:cur + 60])
                scl_type_name_offs = struct.unpack("<IQ", granny[scl_curve_type_offs:scl_curve_type_offs + 12])[1]
                scl_curve_type = read_c_string(granny, scl_type_name_offs)
                patched, locked = _patch_scale_shear_curve_from_action(granny, scl_curve_type, scl_curve_object_offs, action, armature, bone_name, fps=fps, source_duration=source_duration, retime_to_action=retime_to_action)
                stats["scl"] += patched
                stats["locked"] += locked
            except Exception as exc:
                print(f"[UGX Pipeline] Could not patch scale-shear track {bone_name}: {exc}")
    if retime_to_action and source_duration > 0.00001:
        timing_stats = _retime_granny_animation_to_action(granny, action, fps, source_duration)
        stats["retime_duration"] = timing_stats.get("duration", 0)
        stats["retime_knots"] = timing_stats.get("knots", 0)
    if rewrite_clip_name:
        stats["names"] = _patch_granny_clip_names(granny, _sanitize_filename(str(action.get("uax_source", "")) or action.name or "animation"))
    return stats


def _build_patched_native_uax_from_action(action: bpy.types.Action, armature: bpy.types.Object, operator=None, *, raw_override: bytes | None = None, retime_to_action: bool = False, rewrite_clip_name: bool = False) -> tuple[bytes | None, dict]:
    raw = raw_override if raw_override is not None else _read_raw_uax_from_action(action)
    if not raw:
        return None, {"error": "no_native_cache"}
    chunk = _find_uax_granny_chunk(raw)
    if not chunk:
        return None, {"error": "no_granny_chunk"}
    chunk_offs, chunk_len = chunk
    granny = bytearray(raw[chunk_offs:chunk_offs + chunk_len])
    stats = _patch_granny_animation_chunk_from_action(granny, action, armature, retime_to_action=retime_to_action, rewrite_clip_name=rewrite_clip_name)
    if stats.get("rot", 0) + stats.get("pos", 0) + stats.get("scl", 0) <= 0:
        stats["error"] = "no_curve_controls_patched"
        return None, stats
    patched = bytearray(raw)
    patched[chunk_offs:chunk_offs + chunk_len] = granny
    return bytes(patched), stats

def _write_native_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object | None = None, operator=None, *, raw_override: bytes | None = None, template_path: str = "", retime_to_action: bool = False, rewrite_clip_name: bool = False) -> bool:
    if not action:
        return False
    if armature is None:
        armature = _resolve_export_armature(bpy.context, get_scene_export_settings(bpy.context))
    if not armature or armature.type != "ARMATURE":
        if operator:
            operator.report({"ERROR"}, f"Cannot export {action.name}: no armature was selected/resolved for native UAX patching.")
        return False

    patched_raw, stats = _build_patched_native_uax_from_action(action, armature, operator, raw_override=raw_override, retime_to_action=retime_to_action, rewrite_clip_name=rewrite_clip_name)
    if not patched_raw:
        if operator:
            reason = stats.get("error", "unknown") if isinstance(stats, dict) else "unknown"
            operator.report({"WARNING"}, f"Skipped native UAX patch for {action.name}: {reason}. Re-import the source UAX in this build if needed.")
        return False

    os.makedirs(os.path.dirname(uax_path), exist_ok=True)
    with open(uax_path, "wb") as f:
        f.write(patched_raw)
    if operator:
        source_label = "template" if raw_override is not None else "native"
        operator.report({"INFO"}, f"Exported {source_label} UAX: {os.path.basename(uax_path)} ({len(patched_raw):,} bytes, rot keys {stats.get('rot', 0)}, loc keys {stats.get('pos', 0)}, scale keys {stats.get('scl', 0)}, retimed knots {stats.get('retime_knots', 0)}, names {stats.get('names', 0)}, preserved root/import-fix curves {stats.get('preserved', 0)})")
        if stats.get("locked", 0):
            operator.report({"WARNING"}, f"{action.name}: {stats.get('locked', 0)} identity/unsupported native curves could not be changed without rebuilding the Granny object graph.")
    return True

# -----------------------------------------------------------------------------
# UAX animation export helpers
# -----------------------------------------------------------------------------

def _sanitize_filename(name: str, fallback: str = "animation") -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in (name or fallback)).strip("._ ")
    return safe or fallback


def _resolve_export_armature(context, settings=None):
    settings = settings or get_scene_export_settings(context)
    if settings and getattr(settings, "selected_armature", ""):
        arm = bpy.data.objects.get(settings.selected_armature)
        if arm and arm.type == "ARMATURE":
            return arm
    return find_target_armature(context)


def _iter_candidate_actions_for_armature(armature: bpy.types.Object, *, all_actions=True, specific_action_name: str = ""):
    if not armature or armature.type != "ARMATURE":
        return []
    active = armature.animation_data.action if armature.animation_data else None
    if specific_action_name:
        action = bpy.data.actions.get(specific_action_name)
        return [action] if action else []
    if not all_actions:
        return [active] if active else []

    actions = []
    seen = set()
    if active:
        actions.append(active)
        seen.add(active.name)

    # Imported UAX actions now keep their real clip names, so do NOT depend on an
    # armature-name prefix. Prefer actions tagged by this add-on, then include
    # any remaining actions so batch export can produce one .uax per action.
    for action in bpy.data.actions:
        if action.name in seen:
            continue
        if str(action.get("uax_source", "")):
            actions.append(action)
            seen.add(action.name)
    for action in bpy.data.actions:
        if action.name not in seen:
            actions.append(action)
            seen.add(action.name)
    return actions


def _select_export_bundle_for_armature(context, armature: bpy.types.Object, include_meshes: bool):
    bpy.ops.object.select_all(action="DESELECT")
    armature.select_set(True)
    context.view_layer.objects.active = armature
    if include_meshes:
        for obj in context.scene.objects:
            if obj.type == "MESH" and (obj.parent == armature or any(mod.type == "ARMATURE" and mod.object == armature for mod in obj.modifiers)):
                obj.select_set(True)


def _run_gltf_to_uax_converter(ugx_exe: str, gltf_path: str, uax_path: str, operator=None) -> bool:
    """Convert an intermediate animation glTF to UAX.

    Prefer animation-specific converter commands first. In the previous build,
    `from-gltf` could succeed while writing a generic/static container, which
    made every exported action land at the same small file size.
    """
    command_attempts = [
        [ugx_exe, "to-uax", "-i", gltf_path, "-o", uax_path, "--version", "hw2"],
        [ugx_exe, "to-uax", "-i", gltf_path, "-o", uax_path],
        [ugx_exe, "from-gltf-animation", "-i", gltf_path, "-o", uax_path, "--version", "hw2"],
        [ugx_exe, "from-gltf-animation", "-i", gltf_path, "-o", uax_path],
        [ugx_exe, "from-gltf", "-i", gltf_path, "-o", uax_path, "--version", "hw2"],
        [ugx_exe, "from-gltf", "-i", gltf_path, "-o", uax_path],
    ]
    last_detail = ""
    for cmd in command_attempts:
        try:
            result = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, shell=False)
            if os.path.isfile(uax_path) and os.path.getsize(uax_path) > 0:
                if operator:
                    msg = (result.stdout or result.stderr or f"Exported UAX: {os.path.basename(uax_path)}").strip()
                    operator.report({"INFO"}, msg[:900])
                return True
            last_detail = (result.stderr or result.stdout or "converter returned success but no UAX was written").strip()
        except FileNotFoundError:
            last_detail = "ugx.exe not found"
            break
        except subprocess.CalledProcessError as exc:
            last_detail = (exc.stderr or exc.stdout or str(exc)).strip()
            continue
    if operator:
        operator.report({"ERROR"}, "Could not convert glTF animation to UAX. Last converter response: " + last_detail[:700])
    return False



def _gltf_animation_stats(gltf_path: str) -> tuple[int, int]:
    """Return (animation_count, channel_count) for a separate .gltf file."""
    try:
        with open(gltf_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        animations = data.get("animations") or []
        channels = sum(len(anim.get("channels") or []) for anim in animations if isinstance(anim, dict))
        return len(animations), channels
    except Exception:
        return 0, 0


def _mute_all_nla_tracks(anim_data):
    states = []
    if not anim_data:
        return states
    for track in anim_data.nla_tracks:
        states.append((track, getattr(track, "mute", False), getattr(track, "is_solo", False)))
        try:
            track.mute = True
            track.is_solo = False
        except Exception:
            pass
    return states


def _restore_nla_track_states(states):
    for track, muted, solo in states:
        try:
            track.mute = muted
            track.is_solo = solo
        except Exception:
            pass


def _remove_temp_nla_track(anim_data, track):
    if not anim_data or not track:
        return
    try:
        anim_data.nla_tracks.remove(track)
    except Exception:
        pass


def _action_frame_bounds(action: bpy.types.Action) -> tuple[int, int]:
    try:
        start, end = action.frame_range
    except Exception:
        start, end = 1.0, 1.0
    start_i = max(0, int(math.floor(start)))
    end_i = max(start_i + 1, int(math.ceil(end)))
    return start_i, end_i


def _export_single_action_gltf_for_uax(context, armature: bpy.types.Object, action: bpy.types.Action, gltf_path: str, settings, operator=None) -> bool:
    """Export one Blender action as one glTF animation.

    This isolates the action by temporarily creating a solo NLA track with only
    that action. It avoids Blender exporting the same active/default animation
    for every loop iteration, which was producing identical ~37 KB UAX files.
    """
    if not armature.animation_data:
        armature.animation_data_create()
    anim_data = armature.animation_data

    old_action = anim_data.action
    old_scene_start = context.scene.frame_start
    old_scene_end = context.scene.frame_end
    old_frame = context.scene.frame_current
    muted_states = _mute_all_nla_tracks(anim_data)
    temp_track = None
    try:
        start_i, end_i = _action_frame_bounds(action)
        context.scene.frame_start = start_i
        context.scene.frame_end = end_i
        context.scene.frame_set(start_i)

        anim_data.action = action
        temp_track = anim_data.nla_tracks.new()
        temp_track.name = _sanitize_filename(str(action.get("uax_source", "")) or action.name, "uax_action")
        try:
            temp_track.mute = False
            temp_track.is_solo = True
        except Exception:
            pass
        strip = temp_track.strips.new(temp_track.name, start_i, action)
        strip.frame_start = start_i
        strip.frame_end = end_i
        try:
            strip.action_frame_start = start_i
            strip.action_frame_end = end_i
        except Exception:
            pass

        _select_export_bundle_for_armature(context, armature, include_meshes=(settings.export_meshes if settings else False))
        extra = {
            "export_animation_mode": "NLA_TRACKS",
            "export_nla_strips": True,
            "export_force_sampling": True,
            "export_frame_range": True,
            "export_frame_step": 1,
            "export_optimize_animation_size": False,
        }
        call_gltf_export(
            gltf_path,
            use_selection=True,
            export_animations=True,
            export_all_actions=False,
            axis_up=(settings.axis_up if settings else "Y+"),
            operator=operator,
            extra_kwargs=extra,
        )
        anim_count, channel_count = _gltf_animation_stats(gltf_path)
        if anim_count <= 0 or channel_count <= 0:
            # Fallback: some Blender builds name the active-action mode differently.
            extra["export_animation_mode"] = "ACTIVE_ACTIONS"
            call_gltf_export(
                gltf_path,
                use_selection=True,
                export_animations=True,
                export_all_actions=False,
                axis_up=(settings.axis_up if settings else "Y+"),
                operator=operator,
                extra_kwargs=extra,
            )
            anim_count, channel_count = _gltf_animation_stats(gltf_path)
        if anim_count <= 0 or channel_count <= 0:
            if operator:
                operator.report({"WARNING"}, f"Skipped {action.name}: intermediate glTF had no animation channels.")
            return False
        return True
    finally:
        _remove_temp_nla_track(anim_data, temp_track)
        _restore_nla_track_states(muted_states)
        try:
            anim_data.action = old_action
            context.scene.frame_start = old_scene_start
            context.scene.frame_end = old_scene_end
            context.scene.frame_set(old_frame)
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Template-free native UAX scratch writer for brand-new custom actions
# -----------------------------------------------------------------------------

class _BinaryArena:
    """Small offset-addressed byte arena used to build the minimal Granny chunk.

    The old Blender 4 workflow read UAX directly as an ECF file containing a
    Granny animation chunk 0x700. This writer creates that same high-level layout
    without using ugx.exe, then stores uncompressed Float32 position/rotation
    curves so custom actions are real UAX files instead of glTF stubs.
    """
    def __init__(self, initial_size: int = 160):
        self.data = bytearray(b"\x00" * initial_size)

    def align(self, amount: int = 8):
        pad = (-len(self.data)) % int(amount)
        if pad:
            self.data.extend(b"\x00" * pad)

    def add(self, raw: bytes, align: int = 8) -> int:
        self.align(align)
        off = len(self.data)
        self.data.extend(raw)
        return off

    def add_zeros(self, count: int, align: int = 8) -> int:
        return self.add(b"\x00" * int(count), align=align)

    def add_cstr(self, text: str) -> int:
        return self.add(str(text or "").encode("utf-8", errors="ignore") + b"\x00", align=1)

    def add_cstring(self, text: str, align: int = 1) -> int:
        """Compatibility alias used by the repo-audited scratch writer.

        Older parts of this add-on called the method add_cstr(), while the
        newer UAX writer path calls add_cstring(text, align=4). Keep both names
        so export cannot fail before writing the native string table.
        """
        return self.add(str(text or "").encode("utf-8", errors="ignore") + b"\x00", align=int(align or 1))

    def patch(self, off: int, raw: bytes):
        """Overwrite already-reserved bytes at an absolute arena offset.

        The repo-audited UAX writer reserves old-style structs first, then
        fills them after all strings/curves have known offsets.  v5.3.1 had
        those calls but not this compatibility method.
        """
        raw = bytes(raw or b"")
        end = int(off) + len(raw)
        if end > len(self.data):
            self.data.extend(b"\x00" * (end - len(self.data)))
        self.data[int(off):end] = raw

    def bytes(self) -> bytes:
        """Return the arena as immutable bytes."""
        return bytes(self.data)

    def pack_into(self, fmt: str, off: int, *values):
        size = struct.calcsize(fmt)
        end = int(off) + size
        if end > len(self.data):
            self.data.extend(b"\x00" * (end - len(self.data)))
        struct.pack_into(fmt, self.data, off, *values)


_SCRATCH_ROT_TYPE = "CurveDataHeader_DaK32fC32f"
_SCRATCH_POS_TYPE = "CurveDataHeader_DaK32fC32f"
_SCRATCH_SCL_TYPE = "CurveDataHeader_DaK32fC32f"


def _axis_label_vector(label: str) -> Vector:
    label = str(label or "").strip().upper()
    if len(label) < 2:
        return Vector((0.0, 0.0, 0.0))
    sign = -1.0 if label.endswith("-") else 1.0
    axis = label[0]
    if axis == "X":
        return Vector((sign, 0.0, 0.0))
    if axis == "Y":
        return Vector((0.0, sign, 0.0))
    if axis == "Z":
        return Vector((0.0, 0.0, sign))
    return Vector((0.0, 0.0, 0.0))


def _settings_axis_matrix(settings) -> Matrix:
    """Return a Blender->HaloWars component matrix from Back/Right/Up settings.

    The UI labels come from the original exporter convention: Right, Up, and Back
    describe which Blender axes correspond to the engine's local basis. For UAX
    vector export we write components in (Right, Up, Back) order. This makes the
    fields functional instead of cosmetic while preserving the default
    Back=Z-, Right=X+, Up=Y+ convention.
    """
    right = _axis_label_vector(getattr(settings, "axis_right", "X+"))
    up = _axis_label_vector(getattr(settings, "axis_up", "Y+"))
    back = _axis_label_vector(getattr(settings, "axis_back", "Z-"))
    # Rows perform dot-products against the Blender vector.
    return Matrix(((right.x, right.y, right.z), (up.x, up.y, up.z), (back.x, back.y, back.z)))


def _settings_axis_quaternion(settings) -> Quaternion:
    """Best-effort rotational basis for axis settings.

    Some Back/Right/Up combinations are left-handed, so a perfect quaternion basis
    does not always exist. In that case we keep rotation in the legacy basis and
    still apply the axis matrix to translations, which is safer for in-game clips.
    """
    try:
        m3 = _settings_axis_matrix(settings).to_3x3()
        if m3.determinant() < 0.0:
            return Quaternion((1.0, 0.0, 0.0, 0.0))
        q = m3.to_quaternion()
        q.normalize()
        return q
    except Exception:
        return Quaternion((1.0, 0.0, 0.0, 0.0))


def _action_keyed_bone_names(action: bpy.types.Action) -> set[str]:
    names = set()
    if not action:
        return names
    for fc in iter_action_fcurves(action):
        path = getattr(fc, "data_path", "")
        if path.startswith('pose.bones["'):
            try:
                names.add(path.split('pose.bones["', 1)[1].split('"]', 1)[0])
            except Exception:
                pass
    return names


def _scratch_action_sample_frames(action: bpy.types.Action, *, every_frame: bool = True) -> list[float]:
    start, end = _action_frame_range(action)
    if every_frame:
        return [float(f) for f in range(int(math.floor(start)), int(math.ceil(end)) + 1)]
    frames = {float(start), float(end)}
    for fc in iter_action_fcurves(action):
        for kp in getattr(fc, "keyframe_points", []):
            try:
                x = float(kp.co.x)
                if start - 0.001 <= x <= end + 0.001:
                    frames.add(x)
            except Exception:
                pass
    return sorted(frames)


def _scratch_export_axes_are_default(settings) -> bool:
    return (
        str(getattr(settings, "axis_right", "X+")) == "X+" and
        str(getattr(settings, "axis_up", "Y+")) == "Y+" and
        str(getattr(settings, "axis_back", "Z-")) == "Z-"
    )


def _scratch_pose_quat(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, axis_q: Quaternion) -> Quaternion:
    # Same inverse basis as the working UAX importer:
    # import: blender_quat = bone.matrix^-1 @ native_quat
    # export: native_quat = bone.matrix @ blender_quat
    q = _uax_quat_from_action_rotation(action, armature, bone_name, frame, axis_q=axis_q)
    if q is None:
        q = Quaternion((1.0, 0.0, 0.0, 0.0))
    try:
        q.normalize()
    except Exception:
        q = Quaternion((1.0, 0.0, 0.0, 0.0))
    return q


def _scratch_pose_pos(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, settings, axis_q: Quaternion, axis_m: Matrix) -> Vector:
    # Same inverse basis as the working UAX importer. Missing location curves mean
    # rest/local zero, not a missing native position track.
    if bone_name not in armature.data.bones:
        return Vector((0.0, 0.0, 0.0))
    loc = _action_location_at(action, bone_name, frame)
    if loc is None:
        loc = Vector((0.0, 0.0, 0.0))
    bone = armature.data.bones[bone_name]
    try:
        import_scale = float(armature.get("hwugx_import_scale", 1.0))
    except Exception:
        import_scale = 1.0
    final = Vector(loc) + bone.head
    # Legacy HW Suite UAX import flips child Z on import; export must reverse it.
    if bone.parent:
        final.z = -final.z
    if abs(import_scale) > 0.000001:
        final = final / import_scale
    try:
        if abs(axis_q.angle) >= 0.000001:
            final = axis_q.inverted() @ final
    except Exception:
        pass
    # Back/Right/Up are intentionally basis controls. The default matches the old
    # HW Suite path, so it is treated as legacy/no extra remap. If the user changes
    # those fields, apply the explicit matrix so the controls actually affect custom UAX.
    try:
        if not _scratch_export_axes_are_default(settings):
            final = axis_m @ final
    except Exception:
        pass
    return final


def _scratch_pose_scale_shear(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float) -> tuple[float, float, float, float, float, float, float, float, float]:
    """Return a native Granny 3x3 scale-shear matrix for one sampled pose.

    The old working custom UAX files did not leave scale as DaIdentity; they wrote
    animated DaK32fC32f scale-shear controls. For template-free custom UAX export,
    direct Blender pose-bone scale is the safest match. Missing scale curves export
    as identity, not a guessed 1.575/1.6 constant.
    """
    scl = _action_scale_at(action, bone_name, frame)
    if scl is None:
        sx = sy = sz = 1.0
    else:
        sx, sy, sz = float(scl.x), float(scl.y), float(scl.z)
    # Granny scale-shear is row-major 3x3. We export diagonal scale only; shear is
    # kept zero, matching the old generated UAX files for normal Blender scale keys.
    return (sx, 0.0, 0.0, 0.0, sy, 0.0, 0.0, 0.0, sz)


def _build_curve_type_objects(arena: _BinaryArena) -> dict[str, int]:
    out = {}
    for name in (_SCRATCH_ROT_TYPE, _SCRATCH_POS_TYPE, _SCRATCH_SCL_TYPE):
        name_off = arena.add_cstr(name)
        out[name] = arena.add(struct.pack("<IQ", 0, name_off), align=8)
    return out


_GRANNY_DAK32FC32F_HEADER = 0x00000201


def _build_float_rot_curve(arena: _BinaryArena, times: list[float], quats: list[Quaternion]) -> int:
    count = len(times)
    knot_off = arena.add(b"".join(struct.pack("<f", float(t)) for t in times), align=4)
    ctrl = bytearray()
    for q in quats:
        try:
            q.normalize()
        except Exception:
            q = Quaternion((1.0, 0.0, 0.0, 0.0))
        ctrl.extend(struct.pack("<ffff", float(q.x), float(q.y), float(q.z), float(q.w)))
    ctrl_off = arena.add(bytes(ctrl), align=4)
    # The working Blender 4 generated UAX files use 0x201 as the DaK32fC32f
    # curve header. Writing 0 here made files parseable by our importer but not
    # safe for HW2's runtime.
    return arena.add(struct.pack("<IIQIQ", _GRANNY_DAK32FC32F_HEADER, count, knot_off, count, ctrl_off), align=8)


def _build_float_pos_curve(arena: _BinaryArena, times: list[float], vecs: list[Vector]) -> int:
    count = len(times)
    knot_off = arena.add(b"".join(struct.pack("<f", float(t)) for t in times), align=4)
    ctrl = bytearray()
    for v in vecs:
        ctrl.extend(struct.pack("<fff", float(v.x), float(v.y), float(v.z)))
    ctrl_off = arena.add(bytes(ctrl), align=4)
    # Position DaK32fC32f uses the older 32-bit-offset variant seen in working UAX.
    return arena.add(struct.pack("<IIIIIII", _GRANNY_DAK32FC32F_HEADER, count, knot_off, 0, count * 3, ctrl_off, 0), align=8)


def _scratch_native_scale_shear_value(armature: bpy.types.Object) -> float:
    # Working custom UAX exports from the old Blender 4 path wrote a 3x3
    # scale-shear curve with about 1.6 on the diagonal. If the model was imported
    # with the HW2 scale correction, use its inverse; otherwise keep the observed
    # HW2-safe fallback.
    try:
        import_scale = float(armature.get("hwugx_import_scale", 0.0))
        if abs(import_scale) > 0.000001:
            return float(1.0 / import_scale)
    except Exception:
        pass
    return 1.6


def _build_float_scale_shear_curve(arena: _BinaryArena, times: list[float], armature: bpy.types.Object) -> int:
    count = len(times)
    knot_off = arena.add(b"".join(struct.pack("<f", float(t)) for t in times), align=4)
    s = _scratch_native_scale_shear_value(armature)
    ctrl = bytearray()
    for _ in times:
        # Granny transform scale-shear is a 3x3 matrix, not a simple Vector scale.
        # The old working file stores nine controls per key.
        ctrl.extend(struct.pack("<fffffffff", s, 0.0, 0.0, 0.0, s, 0.0, 0.0, 0.0, s))
    ctrl_off = arena.add(bytes(ctrl), align=4)
    return arena.add(struct.pack("<IIIIIII", _GRANNY_DAK32FC32F_HEADER, count, knot_off, 0, count * 9, ctrl_off, 0), align=8)


def _build_identity_curve_object(arena: _BinaryArena) -> int:
    return arena.add(struct.pack("<I", 0), align=4)


def _build_scratch_granny_chunk_from_action(action: bpy.types.Action, armature: bpy.types.Object, settings, *, operator=None) -> tuple[bytes | None, dict]:
    stats = {"tracks": 0, "rot_keys": 0, "pos_keys": 0, "duration": 0.0, "error": ""}
    if not action or not armature or armature.type != "ARMATURE":
        stats["error"] = "missing_action_or_armature"
        return None, stats
    fps = bpy.context.scene.render.fps / max(bpy.context.scene.render.fps_base, 0.0001)
    frames = _scratch_action_sample_frames(action, every_frame=True)
    if not frames:
        stats["error"] = "no_action_frames"
        return None, stats
    start = min(frames)
    duration = max(0.001, (max(frames) - start) / max(fps, 0.0001))
    times = [max(0.0, (f - start) / max(fps, 0.0001)) for f in frames]
    stats["duration"] = duration

    keyed = _action_keyed_bone_names(action)
    # Export all deform/regular bones by default. Missing tracks can be interpreted
    # differently by the engine, so a full track group is safer for custom clips.
    bone_names = [b.name for b in armature.data.bones]
    if keyed:
        # Keep full skeleton but ensure keyed bones remain ordered with the armature.
        bone_names = [n for n in bone_names if n in armature.data.bones]
    if not bone_names:
        stats["error"] = "no_bones"
        return None, stats

    axis_m = _settings_axis_matrix(settings)
    # Legacy-safe default: Back=Z-, Right=X+, Up=Y+ matches the original UAX importer/export basis.
    # Only custom non-default axes get a quaternion basis where possible.
    axis_q = _settings_axis_quaternion(settings) if not _scratch_export_axes_are_default(settings) else Quaternion((1.0, 0.0, 0.0, 0.0))
    clip_name = _sanitize_filename(str(action.get("uax_source", "")) or action.name or "custom_action")

    # Reserve the same initial file_info footprint used by the working Blender 4 UAX
    # files, then add an art_tool_info block right after it. Earlier scratch builds
    # left these fields zeroed, which produced files our parser could read but HW2
    # could hang on.
    arena = _BinaryArena(0x94)
    art_tool_info_off = arena.add_zeros(0x58, align=4)
    type_ptrs = _build_curve_type_objects(arena)

    track_records = []
    for bone_name in bone_names:
        name_off = arena.add_cstr(bone_name)
        quats = [_scratch_pose_quat(action, armature, bone_name, f, axis_q) for f in frames]
        poss = [_scratch_pose_pos(action, armature, bone_name, f, settings, axis_q, axis_m) for f in frames]
        rot_obj = _build_float_rot_curve(arena, times, quats)
        pos_obj = _build_float_pos_curve(arena, times, poss)
        scale_obj = _build_float_scale_shear_curve(arena, times, armature)
        track_records.append((name_off, rot_obj, pos_obj, scale_obj))
        stats["tracks"] += 1
        stats["rot_keys"] += len(quats)
        stats["pos_keys"] += len(poss)

    tracks_off = arena.add_zeros(len(track_records) * 60, align=8)
    for i, (name_off, rot_obj, pos_obj, scale_obj) in enumerate(track_records):
        off = tracks_off + i * 60
        arena.pack_into("<QI", off + 0, name_off, 0)
        arena.pack_into("<QQ", off + 12, type_ptrs[_SCRATCH_ROT_TYPE], rot_obj)
        arena.pack_into("<QQ", off + 28, type_ptrs[_SCRATCH_POS_TYPE], pos_obj)
        arena.pack_into("<QQ", off + 44, type_ptrs[_SCRATCH_SCL_TYPE], scale_obj)

    group_name_off = arena.add_cstr(clip_name)
    group_off = arena.add_zeros(172, align=8)
    arena.pack_into("<Q", group_off + 0, group_name_off)
    arena.pack_into("<IQ", group_off + 20, len(track_records), tracks_off)
    arena.pack_into("<I", group_off + 124, 0)  # flags; bit 4 VDA is off.

    group_ptrs_off = arena.add(struct.pack("<Q", group_off), align=8)

    anim_name_off = arena.add_cstr(clip_name)
    anim_off = arena.add_zeros(80, align=8)
    arena.pack_into("<Q", anim_off + 0, anim_name_off)
    arena.pack_into("<ff", anim_off + 8, float(duration), float(1.0 / max(fps, 0.0001)))
    # Granny animation records include their own track-group reference list. The old
    # importer only reads file_info.TrackGroups, but HW2's runtime expects the
    # animation to point at its track group too. Missing this was a likely cause of
    # template-free clips freezing or doing nothing in game.
    arena.pack_into("<fI", anim_off + 16, 1.0, 1)  # oversampling, trackGroupCount
    arena.pack_into("<Q", anim_off + 24, group_ptrs_off)
    anim_ptrs_off = arena.add(struct.pack("<Q", anim_off), align=8)

    art_tool_text = "Blender 4.0.0 commit date:2023-11-13, commit time:17:26, hash:878f71061b8e"
    art_tool_text_off = arena.add_cstr(art_tool_text)
    exporter_name_off = arena.add_cstr("gr2ugx")

    # granny_file_info fields mirrored from known-good UAX exports:
    # +0 ArtToolInfo pointer, +16 exporter name, +108 TrackGroups, +120 Animations.
    arena.pack_into("<Q", 0, art_tool_info_off)
    arena.pack_into("<Q", 16, exporter_name_off)
    arena.pack_into("<IQ", 108, 1, group_ptrs_off)
    arena.pack_into("<IQ", 120, 1, anim_ptrs_off)

    # Basic art_tool_info block. The exact text is less important than the object
    # existing and using the same sane coordinate/matrix defaults as working files.
    arena.pack_into("<Q", art_tool_info_off + 0, art_tool_text_off)
    arena.pack_into("<I", art_tool_info_off + 8, 1)
    arena.pack_into("<I", art_tool_info_off + 16, 0x20)
    arena.pack_into("<ffffffffffff", art_tool_info_off + 20,
                    1.0, 0.0, 0.0,
                    0.0, 1.0, 0.0,
                    0.0, 0.0, 1.0,
                    0.0, 0.0, -1.0)
    return bytes(arena.data), stats


# -----------------------------------------------------------------------------
# v5.0.0 scratch UAX writer override
# -----------------------------------------------------------------------------
# The v4.9 writer fixed the ECF shell, but it still differed from known-good
# old-plugin custom UAX files in the Granny animation chunk layout. Most importantly:
# - minimal curve-type objects were too small for the game runtime;
# - rotation curve controlCount was written as key_count, not key_count * 4;
# - track group flags/internal animation fields/internal names did not match old output.
# This override intentionally mirrors the old working UAX structure:
# file_info -> art_tool_info -> track_group pointer -> track_group -> tracks ->
# full DaK32fC32f type metadata -> curve arrays -> animation -> strings -> curve headers.

_HWUGX_OLD_ART_TOOL_INFO_TEMPLATE = bytes.fromhex(
        "3c0a0000000000000100000000000000200000000000803f0000000000000000000000000000803f0000000000000000"
        "000000000000803f000000000000000000000000000080bf00000000000000000000000000000000"
    )
_HWUGX_OLD_TRACK_GROUP_TEMPLATE = bytes.fromhex(
        "900a00000000000000000000000000000000000001000000980100000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000000000000000000803f0000803f00000000"
        "00000000000000000000803f0000000000000000000000000000803f0200000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000"
    )
_HWUGX_OLD_DAK32FC32F_TYPE_TEMPLATE = bytes.fromhex(
        "01000000ac0a000000000000b0020000000000000000000000000000000000000000000000000000000000000f000000"
        "d80a000000000000000000000000000000000000000000000000000000000000000000000000000003000000e00a0000"
        "00000000340300000000000000000000000000000000000000000000000000000000000003000000f00a000000000000"
        "340300000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000000c000000c80a0000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000c000000d00a000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000a000000e80a0000000000000000000000000000000000000000000000000000"
        "000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000"
    )

_OLD_TYPE_OFF = 468
_OLD_TYPE_STRINGS = {
    2732: "CurveDataHeader_DaK32fC32f",
    2760: "Format",
    2768: "Degree",
    2776: "Padding",
    2784: "Knots",
    2792: "Real32",
    2800: "Controls",
}


def _patch_old_art_tool_info(template: bytes, art_tool_string_off: int) -> bytes:
    """Relocate the old-plugin art_tool_info template for the current chunk.

    The template is copied from known-good Blender 4 generated UAX data.  Its
    first field is the ArtToolName string pointer; leaving that pointer at the
    old absolute value makes the file parse in loose tools but invalidates the
    runtime metadata.  Keep the rest of the matrix/axis metadata byte-for-byte.
    """
    block = bytearray(template or b"")
    if len(block) >= 8:
        struct.pack_into("<Q", block, 0, int(art_tool_string_off))
    return bytes(block)


def _patch_old_dak32fc32f_type(template: bytes, string_offsets: dict[str, int], type_off: int | None = None) -> bytes:
    """Relocate the old full Granny DaK32fC32f type metadata block.

    v5.3 started using the full old-style type metadata, but this helper was
    accidentally omitted.  It patches embedded self-pointers and known string
    references while preserving the rest of the working metadata blob.
    """
    block = bytearray(template or b"")
    if type_off is None:
        # Most call sites reserve the type object at the same offset as the
        # known-good old layout.  Passing type_off is preferred, but this keeps
        # compatibility with the existing v5.3 call signature.
        type_off = _OLD_TYPE_OFF
    patch = {
        688: int(type_off) + (688 - _OLD_TYPE_OFF),
        820: int(type_off) + (820 - _OLD_TYPE_OFF),
    }
    for old_off, label in _OLD_TYPE_STRINGS.items():
        if label in string_offsets:
            patch[int(old_off)] = int(string_offsets[label])
    for i in range(0, len(block) - 3, 4):
        val = struct.unpack_from("<I", block, i)[0]
        if val in patch:
            struct.pack_into("<I", block, i, int(patch[val]))
    return bytes(block)


def _scratch_add_full_dak32fc32f_type_object(arena: _BinaryArena, string_offsets: dict[str, int]) -> int:
    """Add a relocated copy of the full old-plugin DaK32fC32f type metadata.

    The old importer only needed the first <I,Q> of this object to read a curve
    name, but HW2's runtime appears to need the full Granny type metadata block.
    """
    type_off = arena.add_zeros(len(_HWUGX_OLD_DAK32FC32F_TYPE_TEMPLATE), align=4)
    block = bytearray(_HWUGX_OLD_DAK32FC32F_TYPE_TEMPLATE)
    patch = {
        688: type_off + (688 - _OLD_TYPE_OFF),
        820: type_off + (820 - _OLD_TYPE_OFF),
    }
    for old_off, label in _OLD_TYPE_STRINGS.items():
        if label in string_offsets:
            patch[old_off] = int(string_offsets[label])
    for i in range(0, len(block) - 3, 4):
        val = struct.unpack_from("<I", block, i)[0]
        if val in patch:
            struct.pack_into("<I", block, i, patch[val])
    arena.data[type_off:type_off + len(block)] = block
    return type_off


def _scratch_root_bone_name_for_group(armature: bpy.types.Object, bone_names: list[str]) -> str:
    for b in armature.data.bones:
        if b.name.startswith("GrannyRootBone"):
            return b.name
    if bone_names:
        return bone_names[0]
    return armature.data.bones[0].name if armature and armature.type == "ARMATURE" and armature.data.bones else "GrannyRootBone"


def _scratch_export_bone_names(action: bpy.types.Action, armature: bpy.types.Object) -> list[str]:
    keyed = _action_keyed_bone_names(action)
    all_bones = [b.name for b in armature.data.bones]
    if not all_bones:
        return []
    root_name = _scratch_root_bone_name_for_group(armature, all_bones)
    if keyed:
        # The known-good old custom UAX for the attached lekgolo test only wrote
        # the keyed GrannyRootBone track. Exporting every unkeyed helper/VFX bone
        # can produce a clip that loads but does not visibly bind in-game.
        ordered = []
        if root_name in all_bones:
            ordered.append(root_name)
        for name in all_bones:
            if name in keyed and name not in ordered:
                ordered.append(name)
        return ordered
    return all_bones


def _scratch_add_float_array(arena: _BinaryArena, values, width: int) -> int:
    raw = bytearray()
    for v in values:
        if width == 1:
            raw.extend(struct.pack("<f", float(v)))
        else:
            raw.extend(struct.pack("<" + "f" * width, *[float(x) for x in v]))
    return arena.add(bytes(raw), align=4)


def _build_scratch_granny_chunk_from_action(action: bpy.types.Action, armature: bpy.types.Object, settings, *, operator=None) -> tuple[bytes | None, dict]:
    """Build a template-free UAX Granny chunk using the old-plugin compatible layout.

    This is intentionally more rigid than the v4.x scratch writer because HW2 is
    picky: files can parse in our importer and still be ignored in-game if the
    Granny type block, animation record fields, or internal names differ too much
    from old working custom exports.
    """
    stats = {"tracks": 0, "rot_keys": 0, "pos_keys": 0, "duration": 0.0, "error": ""}
    if not action or not armature or armature.type != "ARMATURE":
        stats["error"] = "missing_action_or_armature"
        return None, stats

    fps = bpy.context.scene.render.fps / max(bpy.context.scene.render.fps_base, 0.0001)
    frames = _scratch_action_sample_frames(action, every_frame=True)
    if not frames:
        stats["error"] = "no_action_frames"
        return None, stats

    start = min(frames)
    duration = max(0.001, (max(frames) - start) / max(fps, 0.0001))
    # Match old working exports: knots are at Blender-frame seconds, but the
    # native animation timestep field is fixed to 0.04.
    times = [max(0.0, (float(f) - start) / max(fps, 0.0001)) for f in frames]
    count = len(times)
    stats["duration"] = duration

    bone_names = _scratch_export_bone_names(action, armature)
    if not bone_names:
        stats["error"] = "no_bones"
        return None, stats

    root_name = _scratch_root_bone_name_for_group(armature, bone_names)
    axis_m = _settings_axis_matrix(settings)
    axis_q = _settings_axis_quaternion(settings) if not _scratch_export_axes_are_default(settings) else Quaternion((1.0, 0.0, 0.0, 0.0))

    arena = _BinaryArena(0x94)

    # 0x94 old art_tool_info block.
    art_tool_info_off = arena.add_zeros(len(_HWUGX_OLD_ART_TOOL_INFO_TEMPLATE), align=4)

    # Known-good layout has the file_info TrackGroups list at 0xEC, the group at
    # 0xF4, and tracks at 0x198. Keep those offsets exactly where possible.
    track_group_ptr_list_off = arena.add_zeros(8, align=4)
    group_off = arena.add_zeros(len(_HWUGX_OLD_TRACK_GROUP_TEMPLATE), align=4)
    tracks_off = arena.add_zeros(len(bone_names) * 60, align=4)

    type_off = arena.add_zeros(len(_HWUGX_OLD_DAK32FC32F_TYPE_TEMPLATE), align=4)

    # Curve sample arrays come before the small curve header objects in old output.
    curve_array_info = []
    stats["scale_keys"] = 0
    stats["scale_min"] = None
    stats["scale_max"] = None

    for bone_name in bone_names:
        quats = [_scratch_pose_quat(action, armature, bone_name, f, axis_q) for f in frames]
        poss = [_scratch_pose_pos(action, armature, bone_name, f, settings, axis_q, axis_m) for f in frames]
        scales = [_scratch_pose_scale_shear(action, armature, bone_name, f) for f in frames]
        rot_knot_off = _scratch_add_float_array(arena, times, 1)
        rot_ctrl_off = _scratch_add_float_array(arena, [(q.x, q.y, q.z, q.w) for q in quats], 4)
        pos_knot_off = _scratch_add_float_array(arena, times, 1)
        pos_ctrl_off = _scratch_add_float_array(arena, [(v.x, v.y, v.z) for v in poss], 3)
        scl_knot_off = _scratch_add_float_array(arena, times, 1)
        scl_ctrl_off = _scratch_add_float_array(arena, scales, 9)
        curve_array_info.append((rot_knot_off, rot_ctrl_off, pos_knot_off, pos_ctrl_off, scl_knot_off, scl_ctrl_off))
        stats["tracks"] += 1
        stats["rot_keys"] += len(quats)
        stats["pos_keys"] += len(poss)
        if _action_has_scale_curves(action, bone_name):
            stats["scale_keys"] += len(scales)
        for ss in scales:
            for idx in (0, 4, 8):
                val = float(ss[idx])
                stats["scale_min"] = val if stats["scale_min"] is None else min(float(stats["scale_min"]), val)
                stats["scale_max"] = val if stats["scale_max"] is None else max(float(stats["scale_max"]), val)

    # Animation pointer + record. Old custom UAX files use a 108-byte animation
    # record. The track-group reference list is embedded at +56, and +24 points to it.
    anim_ptrs_off = arena.add_zeros(8, align=8)
    anim_off = arena.add_zeros(108, align=8)

    # Strings/metadata after the animation record, matching old output ordering.
    art_tool_text = "Blender 4.0.0 commit date:2023-11-13, commit time:17:26, hash:878f71061b8e"
    art_tool_text_off = arena.add_cstr(art_tool_text)
    exporter_name_off = arena.add_cstr("gr2ugx")

    bone_name_offsets = {name: arena.add_cstr(name) for name in bone_names}
    if root_name not in bone_name_offsets:
        bone_name_offsets[root_name] = arena.add_cstr(root_name)

    string_offsets = {}
    for label in ("CurveDataHeader_DaK32fC32f", "Format", "Degree", "Padding", "Knots", "Real32", "Controls"):
        string_offsets[label] = arena.add_cstr(label)

    # Curve header objects come after the strings in known-good custom UAX.
    curve_object_info = []
    for (rot_knot_off, rot_ctrl_off, pos_knot_off, pos_ctrl_off, scl_knot_off, scl_ctrl_off) in curve_array_info:
        rot_obj = arena.add(struct.pack("<IIQIQ", _GRANNY_DAK32FC32F_HEADER, count, rot_knot_off, count * 4, rot_ctrl_off), align=4)
        pos_obj = arena.add(struct.pack("<IIIIIII", _GRANNY_DAK32FC32F_HEADER, count, pos_knot_off, 0, count * 3, pos_ctrl_off, 0), align=4)
        scl_obj = arena.add(struct.pack("<IIIIIII", _GRANNY_DAK32FC32F_HEADER, count, scl_knot_off, 0, count * 9, scl_ctrl_off, 0), align=4)
        curve_object_info.append((rot_obj, pos_obj, scl_obj))

    # Internal native animation name is always Default in the old-plugin custom
    # files. The output .uax filename still uses the Blender Action name.
    anim_name_off = arena.add_cstr("Default")

    # Patch full type metadata.
    type_block = bytearray(_HWUGX_OLD_DAK32FC32F_TYPE_TEMPLATE)
    patch = {
        688: type_off + (688 - _OLD_TYPE_OFF),
        820: type_off + (820 - _OLD_TYPE_OFF),
    }
    for old_off, label in _OLD_TYPE_STRINGS.items():
        if label in string_offsets:
            patch[old_off] = int(string_offsets[label])
    for i in range(0, len(type_block) - 3, 4):
        val = struct.unpack_from("<I", type_block, i)[0]
        if val in patch:
            struct.pack_into("<I", type_block, i, patch[val])
    arena.data[type_off:type_off + len(type_block)] = type_block

    # Patch art tool info from old template with the new string pointer.
    art_block = bytearray(_HWUGX_OLD_ART_TOOL_INFO_TEMPLATE)
    struct.pack_into("<Q", art_block, 0, art_tool_text_off)
    arena.data[art_tool_info_off:art_tool_info_off + len(art_block)] = art_block

    # Patch track group list and group record.
    arena.pack_into("<Q", track_group_ptr_list_off, group_off)
    group_block = bytearray(_HWUGX_OLD_TRACK_GROUP_TEMPLATE)
    struct.pack_into("<Q", group_block, 0, bone_name_offsets[root_name])
    struct.pack_into("<IQ", group_block, 20, len(bone_names), tracks_off)
    struct.pack_into("<I", group_block, 124, 2)
    arena.data[group_off:group_off + len(group_block)] = group_block

    # Patch track records.
    for i, bone_name in enumerate(bone_names):
        rec_off = tracks_off + i * 60
        rot_obj, pos_obj, scl_obj = curve_object_info[i]
        arena.pack_into("<QI", rec_off + 0, bone_name_offsets[bone_name], 0)
        arena.pack_into("<QQ", rec_off + 12, type_off, rot_obj)
        arena.pack_into("<QQ", rec_off + 28, type_off, pos_obj)
        arena.pack_into("<QQ", rec_off + 44, type_off, scl_obj)

    # Patch animation pointer and record. The extra fields at +32/+36/+56 are
    # required to match old working custom UAX files; without them HW2 can load the
    # file but ignore the visible animation.
    arena.pack_into("<Q", anim_ptrs_off, anim_off)
    arena.pack_into("<Q", anim_off + 0, anim_name_off)
    arena.pack_into("<ff", anim_off + 8, float(duration), 0.03999999910593033)
    arena.pack_into("<fI", anim_off + 16, 1.0, 1)
    arena.pack_into("<Q", anim_off + 24, anim_off + 56)
    arena.pack_into("<II", anim_off + 32, 1, 1)
    arena.pack_into("<Q", anim_off + 56, group_off)

    # Patch file_info.
    arena.pack_into("<Q", 0, art_tool_info_off)
    arena.pack_into("<Q", 16, exporter_name_off)
    arena.pack_into("<IQ", 108, 1, track_group_ptr_list_off)
    arena.pack_into("<IQ", 120, 1, anim_ptrs_off)

    return bytes(arena.data), stats

def _build_ecf_uax_from_granny_chunk(granny: bytes) -> bytes:
    # Match the ECF header shape used by known-good Halo Wars 2 UAX files.
    # Earlier versions wrote the UAX file ID into the wrong header words and used
    # a 0x38 data offset. The old/working files use file ID at +0x14 and a
    # 0x40-aligned first chunk.
    data_off = 0x40
    raw = bytearray(b"\x00" * data_off)
    total_size = data_off + len(granny)
    struct.pack_into(">I", raw, 0, 0xDABA7737)   # ECF magic
    struct.pack_into(">I", raw, 4, 0x00000020)   # ECF header size/version field seen in valid UAX
    struct.pack_into(">I", raw, 8, 0x20410296)   # compatibility/hash field; preserved from valid writer pattern
    struct.pack_into(">I", raw, 12, total_size)
    struct.pack_into(">H", raw, 16, 1)
    struct.pack_into(">I", raw, 20, 0xAAC93747)  # UAX file ID
    struct.pack_into(">QII", raw, 32, 0x700, data_off, len(granny))
    struct.pack_into(">I", raw, 48, 0xDA1599BF)  # chunk hash/marker seen in working old-plugin UAX
    struct.pack_into(">I", raw, 52, 0x00040000)  # chunk flags seen in working UAX
    raw.extend(granny)
    return bytes(raw)


def _write_scratch_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    granny, stats = _build_scratch_granny_chunk_from_action(action, armature, settings, operator=operator)
    if not granny:
        if operator:
            operator.report({"ERROR"}, f"Could not build scratch UAX for {action.name}: {stats.get('error', 'unknown')}")
        return False
    raw = _build_ecf_uax_from_granny_chunk(granny)
    os.makedirs(os.path.dirname(uax_path), exist_ok=True)
    with open(uax_path, "wb") as f:
        f.write(raw)
    if operator:
        operator.report({"INFO"}, f"Exported EXPERIMENTAL scratch UAX: {os.path.basename(uax_path)} ({len(raw):,} bytes, tracks {stats.get('tracks', 0)}, rot keys {stats.get('rot_keys', 0)}, loc keys {stats.get('pos_keys', 0)}, scale keys {stats.get('scale_keys', 0)}, scale range {stats.get('scale_min', 0)}..{stats.get('scale_max', 0)}, duration {stats.get('duration', 0.0):.3f}s)")
    return True


def _write_uax_export_debug_report(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, reason: str, settings, operator=None):
    """Write a sidecar report for UAX exports that cannot be made game-safe.

    This intentionally does not pretend a template-free scratch file is valid.
    The original Blender importer only proves we can READ the Granny animation
    curves; it does not provide enough metadata to build a full engine-safe Granny
    file from nothing.
    """
    try:
        report_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
        frames = _scratch_action_sample_frames(action, every_frame=False) if action else []
        keyed = sorted(_action_keyed_bone_names(action)) if action else []
        data = {
            "action": getattr(action, "name", ""),
            "requested_output": uax_path,
            "armature": getattr(armature, "name", ""),
            "reason": reason,
            "frame_range": list(_action_frame_range(action)) if action else [],
            "keyed_bones_count": len(keyed),
            "keyed_bones": keyed,
            "sample_key_frames": frames[:256],
            "has_imported_uax_cache": bool(action and action.get("hwugx_uax_cache_chunks")),
            "template_path": getattr(settings, "uax_template_path", "") if settings else "",
            "custom_export_mode": getattr(settings, "uax_custom_export_mode", "SCRATCH_NATIVE") if settings else "SAFE_NATIVE",
            "axis_back": getattr(settings, "axis_back", "Z-") if settings else "Z-",
            "axis_right": getattr(settings, "axis_right", "X+") if settings else "X+",
            "axis_up": getattr(settings, "axis_up", "Y+") if settings else "Y+",
            "note": "Template-free export writes a native ECF/UAX with a Granny animation chunk from this Blender Action. Imported/template actions remain safer because they preserve more original Granny metadata.",
        }
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if operator:
            operator.report({"WARNING"}, f"Wrote UAX debug report: {os.path.basename(report_path)}")
        return report_path
    except Exception as exc:
        if operator:
            operator.report({"WARNING"}, f"Could not write UAX debug report: {exc}")
        return None


def _export_uax_sidecars_from_current_settings(context, temp_dir: str, destination_dir: str, base_name: str, operator=None) -> int:
    """Export Blender actions to .uax files.

    Priority:
    1) Imported UAX actions: patch their original native UAX cache.
    2) Optional template fallback: clone/patch a native UAX if supplied.
    3) Template-free native writer: build an ECF/UAX with a Granny animation chunk
       directly from the Blender Action. This is required for brand-new custom rigs.
    """
    settings = get_scene_export_settings(context)
    if not destination_dir:
        return 0
    dest = bpy.path.abspath(destination_dir)
    os.makedirs(dest, exist_ok=True)

    armature = _resolve_export_armature(context, settings)
    if not armature:
        if operator:
            operator.report({"ERROR"}, "Cannot export UAX animations: select or choose an armature.")
        return 0
    if not armature.animation_data:
        armature.animation_data_create()

    actions = _iter_candidate_actions_for_armature(
        armature,
        all_actions=(settings.export_all_actions if settings else True),
        specific_action_name=(getattr(settings, "uax_export_action", "") if settings and not settings.export_all_actions else ""),
    )
    if not actions:
        if operator:
            operator.report({"ERROR"}, "No actions found to export as UAX.")
        return 0

    old_action = armature.animation_data.action
    old_active = context.view_layer.objects.active
    old_selected = list(context.selected_objects)
    exported = 0
    try:
        for action in actions:
            action_base = _sanitize_filename(str(action.get("uax_source", "")) or action.name or base_name)
            uax_path = os.path.join(dest, action_base + ".uax")

            # Edited vanilla/imported path: safest and most exact.
            if _write_native_uax_from_action(
                action,
                uax_path,
                armature,
                operator,
                rewrite_clip_name=bool(getattr(settings, "uax_export_rewrite_names", True)),
            ):
                exported += 1
                continue

            # Optional native template fallback, but no longer mandatory.
            template_raw, template_path = _read_template_uax_from_settings(settings)
            if template_raw:
                retime = str(getattr(settings, "uax_custom_timing_mode", "ACTION_RANGE")) == "ACTION_RANGE"
                if _write_native_uax_from_action(
                    action,
                    uax_path,
                    armature,
                    operator,
                    raw_override=template_raw,
                    template_path=template_path,
                    retime_to_action=retime,
                    rewrite_clip_name=bool(getattr(settings, "uax_export_rewrite_names", True)),
                ):
                    exported += 1
                    continue

            custom_mode = str(getattr(settings, "uax_custom_export_mode", "SCRATCH_NATIVE"))
            if custom_mode == "SCRATCH_NATIVE":
                if getattr(settings, "uax_write_debug_report", True):
                    _write_uax_export_debug_report(action, uax_path, armature, "template_free_native_writer", settings, operator)
                if _write_scratch_uax_from_action(action, uax_path, armature, settings, operator):
                    exported += 1
                    continue
                if operator:
                    operator.report({"ERROR"}, f"Skipped {action.name}: template-free UAX export failed. Legacy toolchain and native fallback debug report written if enabled.")
                continue

            reason = "No imported UAX cache and no optional native template. Custom UAX Mode is Imported/Template Only, so template-free writer is disabled."
            if getattr(settings, "uax_write_debug_report", True):
                _write_uax_export_debug_report(action, uax_path, armature, reason, settings, operator)
            if operator:
                operator.report({"ERROR"}, f"Skipped {action.name}: enable Template-Free Native mode or provide a UAX template.")
            continue

        if operator and exported:
            operator.report({"INFO"}, f"Exported {exported} UAX animation file(s) to {dest}")
        return exported
    finally:
        try:
            armature.animation_data.action = old_action
            bpy.ops.object.select_all(action="DESELECT")
            for obj in old_selected:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if old_active and old_active.name in bpy.data.objects:
                context.view_layer.objects.active = old_active
        except Exception:
            pass



class HWUGX_OT_export_ugx(bpy.types.Operator, ExportHelper):
    bl_idname = "export_scene.hw_ugx"
    bl_label = "Export Halo Wars UGX"
    bl_description = "Export Blender scene/selection to glTF, then convert it to UGX with ugx.exe"
    bl_options = {"REGISTER"}
    filename_ext = ".ugx"

    filter_glob: StringProperty(default="*.ugx", options={"HIDDEN"})
    axis_back: EnumProperty(
        name="Back",
        description="Displayed export orientation helper. Default matches Halo Wars glTF bridge",
        items=(("Z-", "Z-", ""), ("Z+", "Z+", ""), ("Y-", "Y-", ""), ("Y+", "Y+", ""), ("X-", "X-", ""), ("X+", "X+", "")),
        default="Z-",
    )
    axis_right: EnumProperty(
        name="Right",
        description="Displayed export orientation helper. Default matches Halo Wars glTF bridge",
        items=(("X+", "X+", ""), ("X-", "X-", ""), ("Y+", "Y+", ""), ("Y-", "Y-", ""), ("Z+", "Z+", ""), ("Z-", "Z-", "")),
        default="X-",
    )
    axis_up: EnumProperty(
        name="Up",
        description="Up axis for the Blender glTF exporter where supported",
        items=(("Y+", "Y+", "Blender glTF Y-up export"), ("Z+", "Z+", "Native Blender Z-up when supported")),
        default="Y+",
    )
    export_meshes: BoolProperty(
        name="Export Meshes",
        description="When an armature is chosen, include mesh children with the armature",
        default=True,
    )
    export_selection_only: BoolProperty(
        name="Selected Only",
        description="Export only selected objects, or the chosen armature bundle when Selected Armature is set",
        default=True,
    )
    selected_armature: EnumProperty(
        name="Selected Armature",
        description="Armature root to export animations from",
        items=enum_scene_armatures,
    )
    export_animations: BoolProperty(
        name="Export Animations",
        description="Include Blender actions/NLA animation data in the intermediate glTF when supported by the converter",
        default=True,
    )
    export_all_actions: BoolProperty(
        name="All Actions",
        description="Export all candidate actions as individual .uax files when UAX sidecar export is enabled",
        default=True,
    )
    uax_export_action: EnumProperty(
        name="Individual Action",
        description="Action to export when All Actions is disabled",
        items=enum_actions_for_export,
    )
    animation_export_path: StringProperty(
        name="Animation Export Path",
        description="Optional folder where exported UAX animation file(s) should be written",
        subtype="DIR_PATH",
        default="",
    )
    uax_template_path: StringProperty(
        name="Optional UAX Template Fallback",
        description="Optional fallback. Safest custom-export path when no imported UAX cache exists.",
        subtype="FILE_PATH",
        default="",
    )
    uax_custom_export_mode: EnumProperty(
        name="Custom UAX Mode",
        description="How to handle Actions that were not imported from a real UAX",
        items=(
            ("SCRATCH_NATIVE", "Template-Free Legacy/Native", "Use bundled legacy DAE->GR2->UAX tools first; fall back to native scratch writer only if legacy tools are unavailable"),
            ("SAFE_NATIVE", "Imported/Template Only", "Only export imported UAX actions or an optional template fallback"),
        ),
        default="SCRATCH_NATIVE",
    )
    uax_write_debug_report: BoolProperty(
        name="Write Debug Report",
        description="Write a .txt report beside skipped/experimental UAX exports explaining what data was used and why",
        default=True,
    )
    uax_custom_timing_mode: EnumProperty(
        name="Custom Action Timing",
        description="For optional template fallback this controls template retiming. Experimental scratch uses the Action range",
        items=(
            ("ACTION_RANGE", "Use Action Range", "Retarget template key times across the Blender Action frame range and write the action duration to the UAX"),
            ("TEMPLATE", "Keep Template Timing", "Use the template UAX native knot times and duration exactly"),
        ),
        default="ACTION_RANGE",
    )
    uax_export_rewrite_names: BoolProperty(
        name="Write Clip Name When Possible",
        description="Patch the native Granny animation/track-group name string when the output action name fits in the template's original string space. File name is always written regardless",
        default=True,
    )

    helper_action: EnumProperty(
        name="Action",
        description="Action to edit with the UAX Animation Helper",
        items=enum_helper_actions,
    )
    helper_bone: EnumProperty(
        name="Bone",
        description="Bone to offset uniformly across the selected action timeline",
        items=enum_helper_bones,
    )
    helper_location_offset: FloatVectorProperty(
        name="Location Offset",
        description="Add this XYZ offset to the selected bone's location curves across the whole action",
        subtype="TRANSLATION",
        size=3,
        default=(0.0, 0.0, 0.0),
        precision=5,
    )
    helper_rotation_offset: FloatVectorProperty(
        name="Rotation Offset",
        description="Add this XYZ Euler rotation offset to the selected bone's rotation across the whole action",
        subtype="EULER",
        size=3,
        default=(0.0, 0.0, 0.0),
        precision=5,
    )
    helper_apply_location: BoolProperty(
        name="Apply Location",
        description="Offset the selected bone's location curves",
        default=True,
    )
    helper_apply_rotation: BoolProperty(
        name="Apply Rotation",
        description="Offset the selected bone's quaternion rotation curves uniformly",
        default=False,
    )
    helper_create_missing_keys: BoolProperty(
        name="Create Missing Curves",
        description="If the bone has no matching curves, create start/end keys for the offset",
        default=True,
    )
    helper_auto_capture_on_apply: BoolProperty(
        name="Auto Capture Pose On Apply",
        description="Before applying, read the current pose-bone difference from the active action at the current frame and use it as the timeline-wide offset",
        default=True,
    )
    helper_ground_root_bone: StringProperty(
        name="Ground Root Bone",
        description="Bone whose Z location curve should be adjusted by Clamp Feet To Ground. Usually b_root or bone_root",
        default="b_root",
    )
    helper_left_foot_bone: StringProperty(
        name="Left Foot Bone",
        description="Optional left foot/toe bone for ground clamping. Leave blank to auto-detect",
        default="",
    )
    helper_right_foot_bone: StringProperty(
        name="Right Foot Bone",
        description="Optional right foot/toe bone for ground clamping. Leave blank to auto-detect",
        default="",
    )
    hw2_version: BoolProperty(
        name="Halo Wars 2 Format",
        description="Pass --version hw2 to ugx.exe from-gltf",
        default=True,
    )

    def draw(self, context):
        draw_export_settings_ui(self.layout, self, include_hw2=True)

    def invoke(self, context, event):
        settings = get_scene_export_settings(context)
        if settings is not None:
            self.axis_back = settings.axis_back
            self.axis_right = settings.axis_right
            self.axis_up = settings.axis_up
            self.export_meshes = settings.export_meshes
            self.export_selection_only = settings.export_selection_only
            self.selected_armature = settings.selected_armature
            self.export_animations = settings.export_animations
            self.export_all_actions = settings.export_all_actions
            self.uax_export_action = settings.uax_export_action
            self.animation_export_path = settings.animation_export_path
            if hasattr(settings, "uax_template_path"):
                self.uax_template_path = settings.uax_template_path
            if hasattr(settings, "uax_custom_export_mode") and hasattr(self, "uax_custom_export_mode"):
                self.uax_custom_export_mode = settings.uax_custom_export_mode
            if hasattr(settings, "uax_write_debug_report") and hasattr(self, "uax_write_debug_report"):
                self.uax_write_debug_report = settings.uax_write_debug_report
            if hasattr(settings, "uax_custom_timing_mode"):
                self.uax_custom_timing_mode = settings.uax_custom_timing_mode
            if hasattr(settings, "uax_export_rewrite_names"):
                self.uax_export_rewrite_names = settings.uax_export_rewrite_names
            self.hw2_version = settings.hw2_version
        return super().invoke(context, event)

    def execute(self, context):
        ugx_exe = ensure_converter(self, context)
        if not ugx_exe:
            return {"CANCELLED"}

        output_path = self.filepath
        if not output_path.lower().endswith(".ugx"):
            output_path += ".ugx"

        temp_dir = tempfile.mkdtemp(prefix="hw_ugx_export_")
        base_name = os.path.splitext(os.path.basename(output_path))[0]
        gltf_path = os.path.join(temp_dir, base_name + ".gltf")

        old_active = context.view_layer.objects.active
        old_selected = list(context.selected_objects)
        forced_selection = False
        try:
            arm = None
            if self.selected_armature:
                arm = bpy.data.objects.get(self.selected_armature)
            elif self.export_selection_only:
                # Auto-pick the armature selected in the viewport. This makes the
                # sidebar Selected Only workflow work without manually choosing the
                # same armature again from the dropdown.
                arm = find_target_armature(context)
            if arm and arm.type == "ARMATURE":
                bpy.ops.object.select_all(action="DESELECT")
                arm.select_set(True)
                context.view_layer.objects.active = arm
                if self.export_meshes:
                    for obj in context.scene.objects:
                        if obj.type == "MESH" and (obj.parent == arm or any(mod.type == "ARMATURE" and mod.object == arm for mod in obj.modifiers)):
                            obj.select_set(True)
                forced_selection = True

            call_gltf_export(
                gltf_path,
                use_selection=(self.export_selection_only or forced_selection),
                export_animations=self.export_animations,
                export_all_actions=self.export_all_actions,
                axis_up=self.axis_up,
                operator=self,
            )
            if not os.path.isfile(gltf_path):
                self.report({"ERROR"}, "Blender did not produce the intermediate glTF file.")
                return {"CANCELLED"}
            if self.export_animations and self.animation_export_path:
                scene_settings = get_scene_export_settings(context)
                if scene_settings is not None:
                    for _name in (
                        "axis_back", "axis_right", "axis_up", "export_meshes", "export_selection_only",
                        "selected_armature", "export_animations", "export_all_actions", "uax_export_action",
                        "animation_export_path", "uax_template_path", "uax_custom_export_mode", "uax_write_debug_report",
                        "uax_custom_timing_mode", "uax_export_rewrite_names", "hw2_version",
                    ):
                        if hasattr(self, _name) and hasattr(scene_settings, _name):
                            try:
                                setattr(scene_settings, _name, getattr(self, _name))
                            except Exception:
                                pass
                _export_uax_sidecars_from_current_settings(context, temp_dir, self.animation_export_path, base_name, self)

            cmd = [ugx_exe, "from-gltf", "-i", gltf_path, "-o", output_path]
            if self.hw2_version:
                cmd.extend(["--version", "hw2"])
            if not run_ugx_command(cmd, self):
                return {"CANCELLED"}
            self.report({"INFO"}, f"Exported UGX: {os.path.basename(output_path)}")
            return {"FINISHED"}
        except Exception as exc:
            report_exception(self, "UGX export failed", exc)
            return {"CANCELLED"}
        finally:
            try:
                bpy.ops.object.select_all(action="DESELECT")
                for obj in old_selected:
                    if obj and obj.name in bpy.data.objects:
                        obj.select_set(True)
                if old_active and old_active.name in bpy.data.objects:
                    context.view_layer.objects.active = old_active
            except Exception:
                pass
            clean_temp_dir(temp_dir, context)


# -----------------------------------------------------------------------------
# Operators: UAX animation import
# -----------------------------------------------------------------------------

class HWUGX_OT_import_uax(bpy.types.Operator, ImportHelper):
    bl_idname = "import_anim.hw_uax"
    bl_label = "Import Halo Wars UAX Animation"
    bl_description = "Import a Halo Wars UAX animation onto the selected armature as a Blender Action"
    bl_options = {"REGISTER", "UNDO"}
    filename_ext = ".uax"

    filter_glob: StringProperty(default="*.uax", options={"HIDDEN"})
    import_all_in_folder: BoolProperty(
        name="Import All UAX in Folder",
        description="Import every .uax file in the selected file's folder onto the selected armature",
        default=False,
    )
    set_scene_range: BoolProperty(
        name="Set Timeline Range",
        description="Set the scene frame range to match the imported animation duration",
        default=True,
    )
    linear_keys: BoolProperty(
        name="Linear Keyframes",
        description="Set imported keyframes to linear interpolation, matching game-style authored curves better",
        default=True,
    )
    axis_correction: EnumProperty(
        name="Animation Axis Fix",
        description="How to convert UAX animation keys before writing Blender pose curves",
        items=(
            ("LEGACY_RAW_SAFE", "Legacy / Safe", "Matches the original working Blender 4 UAX importer; does not rotate every bone"),
            ("NONE", "No Axis Rotation", "Same as legacy for rotation, but mainly useful with Child Z Flip disabled"),
            ("GLTF_YUP_TO_BLENDER_ZUP", "Experimental +90 X", "Not recommended: can disfigure rigs because it rotates every bone key"),
            ("GLTF_YUP_TO_BLENDER_ZUP_NEG", "Experimental -90 X", "Not recommended: inverse full-bone axis conversion"),
        ),
        default="LEGACY_RAW_SAFE",
    )
    legacy_child_z_flip: BoolProperty(
        name="Legacy Child Z Flip",
        description="Matches the original Blender 4 importer when writing child translation tracks",
        default=True,
    )
    child_translation_mode: EnumProperty(
        name="Bone Translation",
        description="Controls UAX position tracks. All Bones matches the old working importer; root shim handling below keeps the glTF rig from launching upward",
        items=(
            ("ALL_BONES", "All Bones / Legacy", "Original HW Suite behavior for authored bone positions"),
            ("ROOT_ONLY", "Root Only / Connected", "Use position keys only on root bones; safer for badly matched rigs"),
            ("NONE", "No Translation", "Ignore all UAX position tracks; rotations only"),
        ),
        default="ALL_BONES",
    )
    b_root_xy_lock: BoolProperty(
        name="b_root Z Only",
        description="For b_root/bone_root location keys, zero X/Y translation while keeping Z translation and all rotation. Optional and off by default; only filters b_root/bone_root location, never rotation",
        default=False,
    )
    ground_correction_mode: EnumProperty(
        name="Ground Contact Fix",
        description="Keeps imported walk/run/crouch clips visually grounded by offsetting b_root Z after the UAX keys are written; rotations stay 1:1",
        items=(
            ("SMART_CLAMP", "Feet To Ground", "Sample the skinned mesh and keep the lowest point on Blender ground each frame"),
            ("CONSTANT_OFFSET", "Constant Offset", "Apply one vertical offset to the whole clip; preserves authored bobbing more than Feet To Ground"),
            ("OFF", "Off / 1:1 Raw", "Do not add any ground-contact correction"),
        ),
        default="OFF",
    )
    show_advanced: BoolProperty(
        name="Show Advanced Import Options",
        description="Show legacy axis/translation controls. Leave hidden for the normal 1:1 HW2 workflow",
        default=False,
    )
    granny_root_mode: EnumProperty(
        name="GrannyRootBone Fix",
        description="Prevents the glTF converter shim/root bone from rotating the whole animation upward",
        items=(
            ("LEGACY", "Preserve / 1:1 Custom", "Recommended for custom UAX files made by this exporter; preserve GrannyRootBone rotation, location, and scale"),
            ("LOCK_ROT_LOC", "Lock Rotation + Location", "Use for vanilla clips that tilt the whole glTF rig upward; keeps GrannyRootBone at rest"),
            ("LOCK_ROT", "Lock Rotation Only", "Keep root orientation stable but allow root translation"),
        ),
        default="LEGACY",
    )
    # Internal compatibility only. Kept for old saved operator presets, but no longer shown.
    # Default NONE means b_root/bone_root/pelvis keys are imported 1:1 instead of being filtered.
    root_stabilize_mode: EnumProperty(
        name="Game Root Filter",
        description="Internal compatibility setting; default keeps b_root/bone_root/pelvis animation raw/1:1",
        items=(("NONE", "None / 1:1", "Do not alter b_root, bone_root, or pelvis tracks"),),
        default="NONE",
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True

        main = layout.box()
        main.label(text="Animation Import", icon="ACTION")
        main.prop(self, "import_all_in_folder")
        main.prop(self, "granny_root_mode")

        advanced = layout.box()
        advanced.prop(self, "show_advanced", icon="PREFERENCES")
        if self.show_advanced:
            advanced.prop(self, "set_scene_range")
            advanced.prop(self, "linear_keys")
            advanced.prop(self, "axis_correction")
            advanced.prop(self, "child_translation_mode")
            advanced.prop(self, "legacy_child_z_flip")
            advanced.prop(self, "b_root_xy_lock")
            advanced.prop(self, "ground_correction_mode")
            if self.child_translation_mode == "ROOT_ONLY":
                advanced.label(text="Root-only translation is safer but less faithful.", icon="INFO")
            if self.axis_correction in {"GLTF_YUP_TO_BLENDER_ZUP", "GLTF_YUP_TO_BLENDER_ZUP_NEG"}:
                warn = advanced.box()
                warn.label(text="Experimental full-bone axis rotation can break rigs.", icon="ERROR")

    def execute(self, context):
        armature = find_target_armature(context)
        if not armature:
            self.report({"ERROR"}, "Select the target armature before importing UAX animation.")
            return {"CANCELLED"}

        paths = [self.filepath]
        if self.import_all_in_folder:
            folder = os.path.dirname(self.filepath)
            paths = [os.path.join(folder, f) for f in sorted(os.listdir(folder)) if f.lower().endswith(".uax")]

        imported = 0
        failed = 0
        last_action = None
        for path in paths:
            try:
                last_action = import_uax(
                    path, armature,
                    set_scene_range=self.set_scene_range,
                    linear_keys=self.linear_keys,
                    axis_correction=self.axis_correction,
                    legacy_child_z_flip=self.legacy_child_z_flip,
                    child_translation_mode=self.child_translation_mode,
                    granny_root_mode=self.granny_root_mode,
                    root_stabilize_mode=self.root_stabilize_mode,
                    b_root_xy_lock=self.b_root_xy_lock,
                    ground_correction_mode=self.ground_correction_mode,
                )
                imported += 1
            except Exception as exc:
                failed += 1
                print(f"[UGX Pipeline] Failed importing {path}: {exc}")

        if last_action and armature.animation_data:
            armature.animation_data.action = last_action

        if imported:
            message = f"Imported {imported} UAX animation(s) onto {armature.name}"
            if failed:
                message += f"; {failed} failed. See console."
            self.report({"INFO"}, message)
            return {"FINISHED"}
        self.report({"ERROR"}, "No UAX animations were imported. See console for details.")
        return {"CANCELLED"}



def _find_armature_for_mesh(obj: bpy.types.Object):
    if obj.type != "MESH":
        return None
    for mod in obj.modifiers:
        if mod.type == "ARMATURE" and mod.object and mod.object.type == "ARMATURE":
            return mod.object
    if obj.parent and obj.parent.type == "ARMATURE":
        return obj.parent
    return None


def _repair_mesh_skin_weights(obj: bpy.types.Object, *, limit_to_four=True, remove_empty=True):
    """Normalize/limit vertex group weights after glTF import.

    The external glTF bridge should already create vertex groups, but some files
    arrive with unnormalized or excessive influences. Halo Wars/UGX skinning is
    effectively a compact 4-influence workflow, matching the older importer path,
    so this pass keeps the strongest four weights and renormalizes each vertex.
    """
    if obj.type != "MESH" or not obj.vertex_groups:
        return 0
    arm = _find_armature_for_mesh(obj)
    valid_group_indices = set(range(len(obj.vertex_groups)))
    if arm:
        bone_names = {b.name for b in arm.data.bones}
        valid_group_indices = {vg.index for vg in obj.vertex_groups if vg.name in bone_names}
    fixed = 0
    for v in obj.data.vertices:
        groups = [(g.group, g.weight) for g in v.groups if g.group in valid_group_indices and g.weight > 0.000001]
        if not groups:
            continue
        groups.sort(key=lambda item: item[1], reverse=True)
        keep = groups[:4] if limit_to_four else groups
        total = sum(w for _, w in keep)
        if total <= 0.0:
            continue
        keep_ids = {idx for idx, _ in keep}
        # Remove weak/excess influences first.
        if limit_to_four or remove_empty:
            for idx, _weight in groups:
                if idx not in keep_ids:
                    try:
                        obj.vertex_groups[idx].remove((v.index,))
                    except Exception:
                        pass
        # Reapply normalized weights.
        for idx, weight in keep:
            try:
                obj.vertex_groups[idx].add((v.index,), weight / total, 'REPLACE')
            except Exception:
                pass
        fixed += 1
    try:
        obj.data.update()
    except Exception:
        pass
    return fixed


def _repair_skin_weights_for_objects(objects: Iterable[bpy.types.Object], *, limit_to_four=True):
    targets = set()
    selected = list(objects)
    for obj in selected:
        if obj.type == "MESH":
            targets.add(obj)
        elif obj.type == "ARMATURE":
            for mesh in bpy.context.scene.objects:
                if mesh.type == "MESH" and _find_armature_for_mesh(mesh) == obj:
                    targets.add(mesh)
    total = 0
    for obj in targets:
        total += _repair_mesh_skin_weights(obj, limit_to_four=limit_to_four)
    return total, len(targets)


class HWUGX_OT_repair_skin_weights(bpy.types.Operator):
    bl_idname = "hwugx.repair_skin_weights"
    bl_label = "Repair Skin Weights"
    bl_description = "Normalize Halo Wars/glTF skin weights and limit influences to the strongest four, matching the old exporter/importer workflow"
    bl_options = {"REGISTER", "UNDO"}

    limit_to_four: BoolProperty(
        name="Limit to 4 Influences",
        description="Keep the strongest four weights per vertex, matching the compact UGX skinning workflow",
        default=True,
    )

    def execute(self, context):
        objs = list(context.selected_objects)
        if not objs:
            self.report({"ERROR"}, "Select the imported armature or skinned mesh first.")
            return {"CANCELLED"}
        fixed_vertices, mesh_count = _repair_skin_weights_for_objects(objs, limit_to_four=self.limit_to_four)
        self.report({"INFO"}, f"Repaired skin weights on {mesh_count} mesh(es), {fixed_vertices} weighted vertices normalized.")
        return {"FINISHED"}


class HWUGX_OT_fix_selected_cleanup(bpy.types.Operator):
    bl_idname = "hwugx.fix_selected_cleanup"
    bl_label = "Fix Selected Rig / Mesh Display"
    bl_description = "Force selected armature to octahedral bones and clear broken imported mesh normals/material previews"
    bl_options = {"REGISTER", "UNDO"}

    clear_normals: BoolProperty(name="Clear Broken Normals", default=True)
    octahedral_bones: BoolProperty(name="Octahedral Bones", default=True)
    clean_materials: BoolProperty(name="Clean Preview Materials", default=True)

    def execute(self, context):
        objs = list(context.selected_objects)
        if not objs:
            self.report({"ERROR"}, "Select the imported mesh/armature objects first.")
            return {"CANCELLED"}
        if self.octahedral_bones:
            _set_armatures_octahedral(objs)
        if self.clear_normals or self.clean_materials:
            _clean_imported_meshes_and_materials(objs, clean_materials=self.clean_materials)
        self.report({"INFO"}, "Cleaned selected imported UGX display data.")
        return {"FINISHED"}


class HWUGX_OT_set_converter_path(bpy.types.Operator, ImportHelper):
    bl_idname = "hwugx.set_converter_path"
    bl_label = "Set ugx.exe Path"
    bl_description = "Browse to ugx.exe and save it in this add-on's preferences"
    filename_ext = ".exe"
    filter_glob: StringProperty(default="*.exe", options={"HIDDEN"})

    def execute(self, context):
        prefs = get_prefs(context)
        if not prefs:
            self.report({"ERROR"}, "Could not access add-on preferences.")
            return {"CANCELLED"}
        prefs.ugx_exe_path = self.filepath
        self.report({"INFO"}, "Saved ugx.exe path.")
        return {"FINISHED"}



class HWUGX_OT_export_animation_gltf(bpy.types.Operator):
    bl_idname = "export_scene.hw_ugx_animation_sidecar"
    bl_label = "Export UAX Actions To Path"
    bl_description = "Export the selected armature action(s) as Halo Wars .uax animation files into the Animation Export Path"
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = get_scene_export_settings(context)
        if not settings or not settings.animation_export_path:
            self.report({"ERROR"}, "Set Animation Export Path first.")
            return {"CANCELLED"}
        temp_dir = tempfile.mkdtemp(prefix="hw_uax_export_")
        try:
            count = _export_uax_sidecars_from_current_settings(context, temp_dir, settings.animation_export_path, "animation", self)
            if count <= 0:
                return {"CANCELLED"}
            return {"FINISHED"}
        except Exception as exc:
            report_exception(self, "UAX animation export failed", exc)
            return {"CANCELLED"}
        finally:
            clean_temp_dir(temp_dir, context)




def _resolve_helper_action(settings, armature):
    action_name = getattr(settings, "helper_action", "__ACTIVE__") or "__ACTIVE__"
    if action_name == "__ACTIVE__":
        if armature and armature.animation_data:
            return armature.animation_data.action
        return None
    return bpy.data.actions.get(action_name)


def _frames_for_bone_curves(action, bone_name: str) -> list[float]:
    frames = set()
    prefix = f'pose.bones["{bone_name}"].'
    for fc in iter_action_fcurves(action):
        if getattr(fc, "data_path", "").startswith(prefix):
            for kp in fc.keyframe_points:
                frames.add(float(kp.co.x))
    if not frames:
        start, end = _action_frame_range(action)
        frames.update([float(start), float(end)])
    return sorted(frames)


def _offset_location_curves(action, armature, bone_name: str, offset, create_missing=True) -> int:
    if not action or not armature or not bone_name:
        return 0
    path = f'pose.bones["{bone_name}"].location'
    frames = _frames_for_bone_curves(action, bone_name)
    changed = 0
    for i in range(3):
        delta = float(offset[i])
        if abs(delta) < 0.0000001:
            continue
        fc = find_action_fcurve(action, path, i)
        if fc is None:
            if not create_missing:
                continue
            fc = ensure_uax_fcurve(action, armature, path, i, bone_name)
            for frame in frames:
                _set_or_insert_key(fc, frame, delta)
                changed += 1
        else:
            for kp in fc.keyframe_points:
                kp.co.y += delta
                try:
                    kp.handle_left.y += delta
                    kp.handle_right.y += delta
                except Exception:
                    pass
                changed += 1
            try:
                fc.update()
            except Exception:
                pass
    return changed


def _offset_quaternion_curves(action, armature, bone_name: str, euler_offset, create_missing=True) -> int:
    if not action or not armature or not bone_name:
        return 0
    if max(abs(float(v)) for v in euler_offset) < 0.0000001:
        return 0
    path = f'pose.bones["{bone_name}"].rotation_quaternion'
    frames = _frames_for_bone_curves(action, bone_name)
    curves = [find_action_fcurve(action, path, i) for i in range(4)]
    if any(fc is None for fc in curves):
        if not create_missing:
            return 0
        curves = [ensure_uax_fcurve(action, armature, path, i, bone_name) for i in range(4)]
    # Offset in the bone's local pose space. This keeps a constant correction across
    # the whole clip instead of touching only the current frame/key.
    offset_q = mathutils.Euler((float(euler_offset[0]), float(euler_offset[1]), float(euler_offset[2])), 'XYZ').to_quaternion()
    changed = 0
    for frame in frames:
        current = Quaternion((
            curves[0].evaluate(frame) if curves[0] else 1.0,
            curves[1].evaluate(frame) if curves[1] else 0.0,
            curves[2].evaluate(frame) if curves[2] else 0.0,
            curves[3].evaluate(frame) if curves[3] else 0.0,
        ))
        try:
            current.normalize()
        except Exception:
            pass
        final = current @ offset_q
        try:
            final.normalize()
        except Exception:
            pass
        vals = (final.w, final.x, final.y, final.z)
        for i, value in enumerate(vals):
            _set_or_insert_key(curves[i], frame, value)
            changed += 1
    for fc in curves:
        try:
            fc.update()
        except Exception:
            pass
    return changed



def _eval_action_location(action, bone_name: str, frame: float) -> Vector:
    path = f'pose.bones["{bone_name}"].location'
    return Vector(tuple((find_action_fcurve(action, path, i).evaluate(frame) if find_action_fcurve(action, path, i) else 0.0) for i in range(3)))


def _eval_action_quaternion(action, bone_name: str, frame: float) -> Quaternion:
    path = f'pose.bones["{bone_name}"].rotation_quaternion'
    vals = [1.0, 0.0, 0.0, 0.0]
    for i in range(4):
        fc = find_action_fcurve(action, path, i)
        if fc:
            vals[i] = fc.evaluate(frame)
    q = Quaternion((vals[0], vals[1], vals[2], vals[3]))
    try:
        q.normalize()
    except Exception:
        pass
    return q


def _pose_bone_quaternion(pose_bone) -> Quaternion:
    try:
        if pose_bone.rotation_mode == 'QUATERNION':
            q = pose_bone.rotation_quaternion.copy()
        elif pose_bone.rotation_mode == 'AXIS_ANGLE':
            q = Quaternion((pose_bone.rotation_axis_angle[0], pose_bone.rotation_axis_angle[1], pose_bone.rotation_axis_angle[2], pose_bone.rotation_axis_angle[3]))
        else:
            q = pose_bone.rotation_euler.to_quaternion()
        q.normalize()
        return q
    except Exception:
        return Quaternion((1.0, 0.0, 0.0, 0.0))


def _capture_helper_offsets_from_current_pose(settings, armature, action=None) -> tuple[Vector, Vector]:
    """Capture the current pose-bone delta from the action value at the current frame.

    This is the workflow the helper is meant for: import UAX, scrub to a bad pose,
    move/rotate a bone visually, then press Apply. The tool captures that visual
    change and bakes the same correction across the whole action timeline.
    """
    if not settings or not armature:
        raise RuntimeError("Select an armature first")
    bone_name = getattr(settings, "helper_bone", "")
    active_pb = getattr(bpy.context, "active_pose_bone", None)
    if active_pb and active_pb.name in armature.pose.bones:
        bone_name = active_pb.name
        try:
            settings.helper_bone = bone_name
        except Exception:
            pass
    if not bone_name or bone_name not in armature.pose.bones:
        raise RuntimeError("Choose or select a valid pose bone")
    if action is None:
        action = _resolve_helper_action(settings, armature)
    if not action:
        raise RuntimeError("Choose an action to compare against")
    frame = float(bpy.context.scene.frame_current) + float(getattr(bpy.context.scene, "frame_subframe", 0.0))
    pb = armature.pose.bones[bone_name]

    base_loc = _eval_action_location(action, bone_name, frame)
    pose_loc = pb.location.copy()
    loc_delta = pose_loc - base_loc

    base_q = _eval_action_quaternion(action, bone_name, frame)
    pose_q = _pose_bone_quaternion(pb)
    try:
        rot_delta_q = base_q.inverted() @ pose_q
        rot_delta_q.normalize()
        rot_delta = Vector(rot_delta_q.to_euler('XYZ'))
    except Exception:
        rot_delta = Vector((0.0, 0.0, 0.0))

    settings.helper_location_offset = tuple(loc_delta)
    settings.helper_rotation_offset = tuple(rot_delta)
    return loc_delta, rot_delta


def _best_root_bone_for_ground(armature, requested: str = "") -> str:
    names = {b.name for b in armature.data.bones}
    for n in (requested, "b_root", "bone_root", "root", "GrannyRootBone", "GrannyRootBone_stormmarine01"):
        if n and n in names:
            return n
    for n in names:
        low = n.lower()
        if low == "b_root" or low.endswith("root"):
            return n
    return armature.data.bones[0].name if armature and armature.data.bones else ""


def _selected_pose_bone_names(armature) -> list[str]:
    """Return selected pose-bone names for the active armature.

    This lets the helper work the way you described: select one or more toe/foot
    bones in Pose Mode, hit Clamp, and the chosen bones become the contact bones.
    """
    if not armature or armature.type != "ARMATURE":
        return []
    picked = []
    try:
        for pb in bpy.context.selected_pose_bones or []:
            if getattr(pb, "id_data", None) == armature and pb.name not in picked:
                picked.append(pb.name)
    except Exception:
        pass
    return picked


def _auto_detect_foot_bones(armature, left_hint="", right_hint="", *, use_selected=True) -> list[str]:
    if not armature or armature.type != "ARMATURE":
        return []
    names = [b.name for b in armature.data.bones]
    picked = []

    # Manual fields win when they are filled.
    for hint in (left_hint, right_hint):
        if hint and hint in names and hint not in picked:
            picked.append(hint)
    if picked:
        return picked[:2]

    # Next, use the actual selected pose bones. This is usually the most reliable
    # way to say "these toe/foot/contact bones should be on the floor".
    if use_selected:
        for name in _selected_pose_bone_names(armature):
            if name in names and name not in picked:
                picked.append(name)
        if picked:
            return picked

    # Halo Wars/Granny rigs vary, so prefer actual foot/toe names, then ankle names.
    candidates = []
    for name in names:
        low = name.lower()
        score = 0
        if "toe" in low:
            score += 120
        if "foot" in low or "feet" in low:
            score += 100
        if "ankle" in low:
            score += 60
        if "heel" in low:
            score += 40
        if any(side in low for side in ("_l", "l_", "left")):
            score += 5
        if any(side in low for side in ("_r", "r_", "right")):
            score += 5
        if score:
            candidates.append((score, name))
    candidates.sort(reverse=True)
    for _score, name in candidates:
        if name not in picked:
            picked.append(name)
        if len(picked) >= 2:
            break

    # Last resort: use the two lowest rest-pose bone tails.
    if len(picked) < 2:
        low_bones = []
        for b in armature.data.bones:
            try:
                low_bones.append((min(b.head_local.z, b.tail_local.z), b.name))
            except Exception:
                pass
        low_bones.sort()
        for _z, name in low_bones:
            if name not in picked:
                picked.append(name)
            if len(picked) >= 2:
                break
    return picked


def _action_key_frames(action) -> list[float]:
    frames = set()
    for fc in iter_action_fcurves(action):
        for kp in fc.keyframe_points:
            frames.add(float(kp.co.x))
    if not frames:
        start, end = _action_frame_range(action)
        frames.update((float(start), float(end)))
    return sorted(frames)


def _action_whole_frames(action) -> list[float]:
    start, end = _action_frame_range(action)
    start_i = int(math.floor(start))
    end_i = int(math.ceil(end))
    if end_i < start_i:
        end_i = start_i
    return [float(f) for f in range(start_i, end_i + 1)]


def _set_scene_frame_float(scene, frame: float):
    whole = int(math.floor(frame))
    sub = float(frame) - float(whole)
    try:
        scene.frame_set(whole, subframe=sub)
    except TypeError:
        scene.frame_set(int(round(frame)))


def _floor_z_at_world_xy(floor_obj, world_point: Vector) -> float:
    """Return the floor height directly under world_point.

    If a Floor Plane object is set, its local Z=0 plane is used, including object
    rotation. We intersect a vertical world-space line through the contact bone
    with that plane and use the intersection's Z. If no floor object is set or it
    is parallel to the vertical line, Blender world Z=0 is used.
    """
    if floor_obj is None:
        return 0.0
    try:
        plane_point = floor_obj.matrix_world.translation
        normal = (floor_obj.matrix_world.to_3x3() @ Vector((0.0, 0.0, 1.0))).normalized()
        p1 = Vector((world_point.x, world_point.y, world_point.z + 100000.0))
        p2 = Vector((world_point.x, world_point.y, world_point.z - 100000.0))
        hit = mathutils.geometry.intersect_line_plane(p1, p2, plane_point, normal, False)
        if hit is not None:
            return float(hit.z)
    except Exception:
        pass
    try:
        return float(floor_obj.matrix_world.translation.z)
    except Exception:
        return 0.0


def _pose_bone_contact_points_world(armature, pose_bone) -> list[Vector]:
    """Get useful contact points for a pose bone in world space.

    For toe/foot bones, either head or tail may be the actual contact point, so we
    consider both plus the pose matrix translation. The clamp then uses the
    lowest/average mode from the helper settings.
    """
    pts = []
    try:
        pts.append(armature.matrix_world @ pose_bone.head)
    except Exception:
        pass
    try:
        pts.append(armature.matrix_world @ pose_bone.tail)
    except Exception:
        pass
    try:
        pts.append(armature.matrix_world @ pose_bone.matrix.translation)
    except Exception:
        pass
    return pts




def _mesh_objects_for_armature(armature) -> list:
    """Return mesh objects visibly driven by the chosen armature."""
    if not armature or armature.type != "ARMATURE":
        return []
    meshes = []
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        linked = False
        try:
            if obj.parent == armature:
                linked = True
        except Exception:
            pass
        if not linked:
            for mod in obj.modifiers:
                if mod.type == "ARMATURE" and getattr(mod, "object", None) == armature:
                    linked = True
                    break
        if linked:
            meshes.append(obj)
    return meshes


def _bone_descendant_names(armature, bone_name: str) -> set[str]:
    """Selected foot bones often drive contact through toe/child vertex groups."""
    names = set()
    if not armature or armature.type != "ARMATURE" or bone_name not in armature.data.bones:
        return names
    root = armature.data.bones[bone_name]
    names.add(bone_name)
    stack = list(root.children)
    while stack:
        b = stack.pop()
        if b.name not in names:
            names.add(b.name)
            stack.extend(list(b.children))
    return names


def _contact_groups_from_bones(armature, contact_bones: list[str]) -> list[list[str]]:
    """Build independent contact groups so walk/run clips can alternate left/right."""
    groups = []
    seen = set()
    for bn in contact_bones:
        if not bn or bn in seen or bn not in armature.pose.bones:
            continue
        # Keep each requested/selected contact as its own stance candidate.
        names = sorted(_bone_descendant_names(armature, bn) or {bn})
        groups.append(names)
        seen.add(bn)
    return groups


def _lowest_weighted_mesh_point_world(armature, bone_names: list[str], *, weight_threshold=0.001):
    """Find the lowest evaluated mesh vertex weighted to any contact bone.

    This is the core of the overhauled grounding workflow: instead of grounding a
    bone head/tail, it grounds the visible mesh region assigned to the chosen foot
    or toe bones. If evaluated vertex counts do not line up, the function simply
    skips that mesh and lets the bone fallback handle it.
    """
    bone_set = {n for n in bone_names if n}
    if not bone_set:
        return None
    depsgraph = bpy.context.evaluated_depsgraph_get()
    best = None
    for obj in _mesh_objects_for_armature(armature):
        try:
            group_indices = {vg.index for vg in obj.vertex_groups if vg.name in bone_set}
            if not group_indices:
                continue
            eval_obj = obj.evaluated_get(depsgraph)
            eval_mesh = eval_obj.to_mesh()
        except Exception:
            continue
        try:
            src_verts = obj.data.vertices
            if not eval_mesh or not src_verts:
                continue
            count = min(len(src_verts), len(eval_mesh.vertices))
            mw = eval_obj.matrix_world
            for i in range(count):
                v = src_verts[i]
                weighted = False
                for g in v.groups:
                    if g.group in group_indices and g.weight >= weight_threshold:
                        weighted = True
                        break
                if not weighted:
                    continue
                p = mw @ eval_mesh.vertices[i].co
                if best is None or p.z < best.z:
                    best = p.copy()
        finally:
            try:
                eval_obj.to_mesh_clear()
            except Exception:
                pass
    return best


def _lowest_contact_point_world(armature, bone_names: list[str], *, use_mesh=True):
    if use_mesh:
        p = _lowest_weighted_mesh_point_world(armature, bone_names)
        if p is not None:
            return p
    pts = []
    for bn in bone_names:
        pb = armature.pose.bones.get(bn)
        if not pb:
            continue
        pts.extend(_pose_bone_contact_points_world(armature, pb))
    if not pts:
        return None
    return min(pts, key=lambda item: item.z)


def _ensure_root_location_fcurves(action, armature, root_bone: str):
    path = f'pose.bones["{root_bone}"].location'
    curves = []
    for idx in range(3):
        fc = find_action_fcurve(action, path, idx)
        if fc is None:
            fc = ensure_uax_fcurve(action, armature, path, idx, root_bone)
        curves.append(fc)
    return curves


def _root_location_values(curves, frame: float) -> Vector:
    vals = []
    for fc in curves:
        vals.append(float(fc.evaluate(frame)) if fc else 0.0)
    return Vector(vals)


def _root_world_response_matrix(armature, root_bone: str) -> Matrix:
    """Numerically map root pose-location XYZ to world-space displacement.

    Some HW2/Granny roots are not aligned to Blender world axes. Writing only the
    root's local Z curve can therefore create sideways drift. This response matrix
    lets the solver ask for pure world-Z motion and convert it back into whatever
    local root channels are needed to produce that visible vertical move.
    """
    pb = armature.pose.bones.get(root_bone)
    if pb is None:
        return Matrix.Identity(3)
    scene = bpy.context.scene
    try:
        old = pb.location.copy()
    except Exception:
        return Matrix.Identity(3)
    eps = 0.01
    bpy.context.view_layer.update()
    try:
        base = armature.matrix_world @ pb.matrix.translation
    except Exception:
        base = armature.matrix_world.translation.copy()
    cols = []
    for axis in range(3):
        try:
            pb.location = old.copy()
            pb.location[axis] += eps
            bpy.context.view_layer.update()
            moved = armature.matrix_world @ pb.matrix.translation
            cols.append((moved - base) / eps)
        except Exception:
            fallback = Vector((0.0, 0.0, 0.0))
            fallback[axis] = 1.0
            cols.append(fallback)
    try:
        pb.location = old
        bpy.context.view_layer.update()
    except Exception:
        pass
    try:
        return Matrix(((cols[0].x, cols[1].x, cols[2].x),
                       (cols[0].y, cols[1].y, cols[2].y),
                       (cols[0].z, cols[1].z, cols[2].z)))
    except Exception:
        return Matrix.Identity(3)


def _local_delta_for_world_vertical(armature, root_bone: str, world_delta_z: float) -> Vector:
    if abs(world_delta_z) < 1e-10:
        return Vector((0.0, 0.0, 0.0))
    response = _root_world_response_matrix(armature, root_bone)
    target = Vector((0.0, 0.0, float(world_delta_z)))
    try:
        return response.inverted() @ target
    except Exception:
        # Fallback keeps old behavior if the numeric response is singular.
        return Vector((0.0, 0.0, float(world_delta_z)))


def _smooth_numeric_values(vals: list[float], radius: int) -> list[float]:
    if radius <= 0 or len(vals) <= 2:
        return list(vals)
    out = []
    n = len(vals)
    for i in range(n):
        lo = max(0, i - radius)
        hi = min(n, i + radius + 1)
        out.append(sum(vals[lo:hi]) / max(1, hi - lo))
    return out


def _limit_delta_steps(vals: list[float], max_step: float) -> list[float]:
    if max_step <= 0.0 or len(vals) <= 1:
        return list(vals)
    out = [vals[0]]
    for v in vals[1:]:
        prev = out[-1]
        diff = v - prev
        if diff > max_step:
            out.append(prev + max_step)
        elif diff < -max_step:
            out.append(prev - max_step)
        else:
            out.append(v)
    return out


def _ground_action_contacts_to_floor(action, armature, root_bone: str, contact_bones: list[str], operator=None) -> int:
    """Unified weighted-mesh, alternating-foot grounding solver.

    It samples the action, identifies whichever contact group is actually closest
    to the floor each frame, and offsets the root so that contact's weighted mesh
    region touches the selected floor plane. The correction is converted into root
    local channels so the visible movement is world-Z only, avoiding sideways drift
    from rotated Granny root axes.
    """
    settings = get_scene_export_settings(bpy.context)
    if not action or not armature or not root_bone or root_bone not in armature.pose.bones:
        return 0
    contact_bones = [b for b in contact_bones if b in armature.pose.bones and b != root_bone]
    groups = _contact_groups_from_bones(armature, contact_bones)
    if not groups:
        if operator:
            operator.report({"ERROR"}, "No contact bones found. Select toe/foot bones in Pose Mode, fill Contact A/B, or let auto-detect find foot/toe bones.")
        return 0

    scene = bpy.context.scene
    old_frame = float(scene.frame_current) + float(getattr(scene, "frame_subframe", 0.0))
    old_action = armature.animation_data.action if armature.animation_data else None
    if armature.animation_data is None:
        armature.animation_data_create()
    armature.animation_data.action = action

    curves = _ensure_root_location_fcurves(action, armature, root_bone)
    floor_obj = getattr(settings, "helper_ground_floor_object", None) if settings else None
    use_mesh = bool(getattr(settings, "helper_ground_use_mesh_contact", True)) if settings else True
    pure_world_z = bool(getattr(settings, "helper_ground_world_z_only", True)) if settings else True
    smoothing = int(getattr(settings, "helper_walkrun_smoothing", 0)) if settings else 0
    max_step = float(getattr(settings, "helper_walkrun_max_step", 0.0)) if settings else 0.0
    stickiness = float(getattr(settings, "helper_ground_contact_stickiness", 0.025)) if settings else 0.025
    frames = _action_whole_frames(action)
    if not frames:
        return 0

    raw_world_deltas = []
    original_root_locations = []
    chosen_idx = []
    prev_idx = None
    try:
        # First pass samples the original animation only. Store original root
        # curve values so the write pass never compounds against keys inserted on
        # earlier frames.
        for frame in frames:
            _set_scene_frame_float(scene, frame)
            bpy.context.view_layer.update()
            original_root_locations.append(_root_location_values(curves, frame))
            candidates = []
            for gi, group in enumerate(groups):
                p = _lowest_contact_point_world(armature, group, use_mesh=use_mesh)
                if p is None:
                    continue
                floor_z = _floor_z_at_world_xy(floor_obj, p)
                distance = float(p.z) - float(floor_z)
                candidates.append((distance, gi, p))
            if not candidates:
                raw_world_deltas.append(0.0)
                chosen_idx.append(prev_idx if prev_idx is not None else -1)
                continue
            candidates.sort(key=lambda item: item[0])
            best_distance, best_idx, _p = candidates[0]
            if prev_idx is not None:
                prev_candidate = next((c for c in candidates if c[1] == prev_idx), None)
                if prev_candidate is not None and prev_candidate[0] <= best_distance + stickiness:
                    best_distance, best_idx, _p = prev_candidate
            prev_idx = best_idx
            chosen_idx.append(best_idx)
            raw_world_deltas.append(-float(best_distance))

        solved_deltas = _smooth_numeric_values(raw_world_deltas, smoothing)
        solved_deltas = _limit_delta_steps(solved_deltas, max_step)

        changed = 0
        for frame, world_delta, original_loc in zip(frames, solved_deltas, original_root_locations):
            if abs(world_delta) < 0.000001:
                continue
            _set_scene_frame_float(scene, frame)
            bpy.context.view_layer.update()
            local_delta = _local_delta_for_world_vertical(armature, root_bone, world_delta) if pure_world_z else Vector((0.0, 0.0, float(world_delta)))
            target = original_loc + local_delta
            for idx, fc in enumerate(curves):
                if fc is None:
                    continue
                if abs(float(fc.evaluate(frame)) - float(target[idx])) > 0.000001:
                    _set_or_insert_key(fc, frame, float(target[idx]))
                    changed += 1
        for fc in curves:
            try:
                fc.update()
            except Exception:
                pass
        try:
            action["hwugx_ground_solver"] = "weighted_mesh_world_z"
            action["hwugx_ground_root"] = root_bone
            action["hwugx_ground_contacts"] = ",".join(contact_bones)
        except Exception:
            pass
    finally:
        if armature.animation_data:
            armature.animation_data.action = old_action
        _set_scene_frame_float(scene, old_frame)
        bpy.context.view_layer.update()
    return changed

def _clamp_action_feet_to_ground(action, armature, root_bone: str, foot_bones: list[str], operator=None) -> int:
    settings = get_scene_export_settings(bpy.context)
    if not action or not armature or not root_bone or root_bone not in armature.pose.bones:
        return 0
    foot_bones = [b for b in foot_bones if b in armature.pose.bones]
    if not foot_bones:
        if operator:
            operator.report({"ERROR"}, "No contact bones found. Select toe/foot bones in Pose Mode or fill Contact Bone A/B manually.")
        return 0

    scene = bpy.context.scene
    old_frame = float(scene.frame_current) + float(getattr(scene, "frame_subframe", 0.0))
    old_action = armature.animation_data.action if armature.animation_data else None
    if armature.animation_data is None:
        armature.animation_data_create()
    armature.animation_data.action = action

    path = f'pose.bones["{root_bone}"].location'
    fc_z = find_action_fcurve(action, path, 2)
    if fc_z is None:
        fc_z = ensure_uax_fcurve(action, armature, path, 2, root_bone)

    sample_every_frame = bool(getattr(settings, "helper_ground_sample_every_frame", True)) if settings else True
    contact_mode = getattr(settings, "helper_ground_contact_mode", "LOWEST") if settings else "LOWEST"
    floor_obj = getattr(settings, "helper_ground_floor_object", None) if settings else None
    frames = _action_whole_frames(action) if sample_every_frame else _action_key_frames(action)

    changed = 0
    try:
        for frame in frames:
            _set_scene_frame_float(scene, frame)
            bpy.context.view_layer.update()

            contact_deltas = []
            for bn in foot_bones:
                pb = armature.pose.bones.get(bn)
                if not pb:
                    continue
                pts = _pose_bone_contact_points_world(armature, pb)
                if not pts:
                    continue
                # Use the lowest point of each selected contact bone. This avoids
                # using the high end of a toe/foot bone when the low end is the
                # actual floor contact.
                lowest_pt = min(pts, key=lambda p: p.z)
                floor_z = _floor_z_at_world_xy(floor_obj, lowest_pt)
                contact_deltas.append(float(floor_z) - float(lowest_pt.z))

            if not contact_deltas:
                continue
            if contact_mode == "AVERAGE" and len(contact_deltas) > 1:
                delta = sum(contact_deltas) / len(contact_deltas)
            else:
                # The lowest contact is the one farthest below its floor plane, or
                # if all are above, the one closest to needing the least drop.
                delta = max(contact_deltas)

            if abs(delta) < 0.00001:
                continue
            current_root_z = fc_z.evaluate(frame) if fc_z else 0.0
            _set_or_insert_key(fc_z, frame, current_root_z + delta)
            changed += 1

        try:
            fc_z.update()
        except Exception:
            pass
    finally:
        if armature.animation_data:
            armature.animation_data.action = old_action
        _set_scene_frame_float(scene, old_frame)
        bpy.context.view_layer.update()
    return changed


def _median_values(vals: list[float], radius: int) -> list[float]:
    if radius <= 0 or len(vals) <= 2:
        return list(vals)
    out = []
    n = len(vals)
    for i in range(n):
        lo = max(0, i - radius)
        hi = min(n, i + radius + 1)
        window = sorted(vals[lo:hi])
        out.append(window[len(window)//2])
    return out


def _limit_frame_steps(vals: list[float], max_step: float) -> list[float]:
    if max_step <= 0.0 or len(vals) <= 1:
        return list(vals)
    out = [vals[0]]
    for v in vals[1:]:
        prev = out[-1]
        diff = v - prev
        if diff > max_step:
            out.append(prev + max_step)
        elif diff < -max_step:
            out.append(prev - max_step)
        else:
            out.append(v)
    # reverse pass prevents one-direction lag from creating too much float
    for i in range(len(out) - 2, -1, -1):
        diff = out[i] - out[i+1]
        if diff > max_step:
            out[i] = out[i+1] + max_step
        elif diff < -max_step:
            out[i] = out[i+1] - max_step
    return out


def _solve_walkrun_feet_to_ground(action, armature, root_bone: str, foot_bones: list[str], operator=None) -> int:
    """Alternating walk/run grounding solver.

    The normal clamp tries to satisfy every selected contact bone at once. That can
    fight walk/run cycles where left/right feet alternate. This solver chooses a
    support contact per frame from all selected/manual/auto foot bones, writes only
    root Z, then smooths/limits the root-Z correction so the stance foot is grounded
    without forcing both feet to be planted together.
    """
    settings = get_scene_export_settings(bpy.context)
    if not action or not armature or not root_bone or root_bone not in armature.pose.bones:
        return 0
    foot_bones = [b for b in foot_bones if b in armature.pose.bones and b != root_bone]
    if not foot_bones:
        if operator:
            operator.report({"ERROR"}, "No walk/run contact bones found. Select toe/foot bones in Pose Mode or fill Contact Bone A/B manually.")
        return 0

    scene = bpy.context.scene
    old_frame = float(scene.frame_current) + float(getattr(scene, "frame_subframe", 0.0))
    old_action = armature.animation_data.action if armature.animation_data else None
    if armature.animation_data is None:
        armature.animation_data_create()
    armature.animation_data.action = action

    path = f'pose.bones["{root_bone}"].location'
    fc_z = find_action_fcurve(action, path, 2)
    if fc_z is None:
        fc_z = ensure_uax_fcurve(action, armature, path, 2, root_bone)

    floor_obj = getattr(settings, "helper_ground_floor_object", None) if settings else None
    smoothing = int(getattr(settings, "helper_walkrun_smoothing", 2)) if settings else 2
    max_step = float(getattr(settings, "helper_walkrun_max_step", 0.35)) if settings else 0.35
    frames = _action_whole_frames(action)
    if not frames:
        return 0

    raw_root_z = []
    chosen = []
    try:
        # First pass: evaluate the untouched animation and compute the root-Z value
        # required to place the best support foot/toe on the floor at each frame.
        prev_choice = None
        for frame in frames:
            _set_scene_frame_float(scene, frame)
            bpy.context.view_layer.update()
            candidates = []
            for idx, bn in enumerate(foot_bones):
                pb = armature.pose.bones.get(bn)
                if not pb:
                    continue
                pts = _pose_bone_contact_points_world(armature, pb)
                if not pts:
                    continue
                lowest_pt = min(pts, key=lambda p: p.z)
                floor_z = _floor_z_at_world_xy(floor_obj, lowest_pt)
                # distance > 0 = above floor, distance < 0 = clipping below floor
                distance = float(lowest_pt.z) - float(floor_z)
                # Hysteresis: keep the previous stance foot unless another foot is
                # meaningfully more grounded. This avoids left/right flicker.
                keep_bonus = -0.05 if bn == prev_choice else 0.0
                score = distance + keep_bonus
                candidates.append((score, distance, bn))
            if not candidates:
                current = fc_z.evaluate(frame) if fc_z else 0.0
                raw_root_z.append(current)
                chosen.append(prev_choice or "")
                continue
            candidates.sort(key=lambda item: item[0])
            _score, distance, bn = candidates[0]
            prev_choice = bn
            current_root_z = fc_z.evaluate(frame) if fc_z else 0.0
            raw_root_z.append(float(current_root_z) - float(distance))
            chosen.append(bn)

        solved = _median_values(raw_root_z, smoothing)
        solved = _limit_frame_steps(solved, max_step)

        changed = 0
        for frame, z in zip(frames, solved):
            if abs((fc_z.evaluate(frame) if fc_z else 0.0) - z) < 0.00001:
                continue
            _set_or_insert_key(fc_z, frame, z)
            changed += 1
        try:
            fc_z.update()
        except Exception:
            pass
    finally:
        if armature.animation_data:
            armature.animation_data.action = old_action
        _set_scene_frame_float(scene, old_frame)
        bpy.context.view_layer.update()
    return changed


class HWUGX_OT_capture_helper_offset(bpy.types.Operator):
    bl_idname = "hwugx.capture_helper_offset"
    bl_label = "Capture Current Pose Offset"
    bl_description = "Fill the helper offset fields from the difference between the current pose bone and the selected Action at this frame"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = get_scene_export_settings(context)
        armature = find_target_armature(context)
        try:
            loc_delta, rot_delta = _capture_helper_offsets_from_current_pose(settings, armature)
        except Exception as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Captured offset: loc {tuple(round(v, 5) for v in loc_delta)}, rot {tuple(round(math.degrees(v), 3) for v in rot_delta)} deg")
        return {"FINISHED"}



class HWUGX_OT_set_floor_from_selected(bpy.types.Operator):
    bl_idname = "hwugx.set_floor_from_selected"
    bl_label = "Use Selected Floor"
    bl_description = "Set the Grounding floor object from the selected non-armature object"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = get_scene_export_settings(context)
        if not settings:
            return {"CANCELLED"}
        active = context.view_layer.objects.active
        chosen = None
        if active and active.type != "ARMATURE":
            chosen = active
        else:
            for obj in context.selected_objects:
                if obj.type != "ARMATURE":
                    chosen = obj
                    break
        if not chosen:
            self.report({"ERROR"}, "Select a floor plane/object, then press this button.")
            return {"CANCELLED"}
        settings.helper_ground_floor_object = chosen
        self.report({"INFO"}, f"Floor set to {chosen.name}")
        return {"FINISHED"}


class HWUGX_OT_capture_contact_bones(bpy.types.Operator):
    bl_idname = "hwugx.capture_contact_bones"
    bl_label = "Use Selected Contacts"
    bl_description = "Fill Contact A/B from selected pose bones. Leave them blank to keep using live pose selection"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = get_scene_export_settings(context)
        armature = find_target_armature(context)
        if not settings or not armature:
            self.report({"ERROR"}, "Select the target armature first.")
            return {"CANCELLED"}
        names = _selected_pose_bone_names(armature)
        if not names:
            self.report({"ERROR"}, "Select one or two toe/foot/contact bones in Pose Mode.")
            return {"CANCELLED"}
        def side_score(name):
            low = name.lower()
            if any(s in low for s in ("left", "_l", ".l", " l", "l_")):
                return 0
            if any(s in low for s in ("right", "_r", ".r", " r", "r_")):
                return 1
            try:
                pb = armature.pose.bones.get(name)
                return 0 if pb and (armature.matrix_world @ pb.head).x < 0.0 else 1
            except Exception:
                return 1
        names = sorted(names, key=side_score)
        settings.helper_left_foot_bone = names[0]
        settings.helper_right_foot_bone = names[1] if len(names) > 1 else ""
        self.report({"INFO"}, "Captured contact bone(s): " + ", ".join(names[:2]))
        return {"FINISHED"}


class HWUGX_OT_ground_contacts_to_floor(bpy.types.Operator):
    bl_idname = "hwugx.ground_contacts_to_floor"
    bl_label = "Ground Contacts To Floor"
    bl_description = "One-button foot/contact solver: chosen toe/foot weighted mesh regions touch the selected floor while the root compensation stays vertical in world space"
    bl_options = {"REGISTER", "UNDO"}

    apply_all_actions: BoolProperty(
        name="Apply To All Actions",
        description="Ground all actions instead of only the selected helper action",
        default=False,
    )

    def execute(self, context):
        settings = get_scene_export_settings(context)
        armature = find_target_armature(context)
        if not settings or not armature:
            self.report({"ERROR"}, "Select the target armature first.")
            return {"CANCELLED"}
        root = _best_root_bone_for_ground(armature, getattr(settings, "helper_ground_root_bone", ""))
        contacts = _auto_detect_foot_bones(
            armature,
            getattr(settings, "helper_left_foot_bone", ""),
            getattr(settings, "helper_right_foot_bone", ""),
            use_selected=getattr(settings, "helper_ground_use_selected_bones", True),
        )
        actions = list(bpy.data.actions) if self.apply_all_actions else [_resolve_helper_action(settings, armature)]
        actions = [a for a in actions if a]
        if not actions:
            self.report({"ERROR"}, "No Action found to ground.")
            return {"CANCELLED"}
        if not contacts:
            self.report({"ERROR"}, "No contact bones found. Select toe/foot bones in Pose Mode or fill Contact A/B.")
            return {"CANCELLED"}
        total = 0
        for action in actions:
            ensure_action_channelbag_for_object(action, armature)
            total += _ground_action_contacts_to_floor(action, armature, root, contacts, self)
        if total <= 0:
            self.report({"WARNING"}, f"No grounding keys changed. Root={root}; contacts={', '.join(contacts) if contacts else 'none'}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Grounded {len(actions)} action(s), {total} root key value(s). Root={root}; contacts={', '.join(contacts)}")
        return {"FINISHED"}


class HWUGX_OT_clamp_feet_to_ground(bpy.types.Operator):
    bl_idname = "hwugx.clamp_feet_to_ground"
    bl_label = "Clamp Feet To Ground"
    bl_description = "Adjust the root bone Z curve so selected/manual toe/foot/contact bones touch the chosen floor plane across the selected action"
    bl_options = {"REGISTER", "UNDO"}

    apply_all_actions: BoolProperty(
        name="Apply To All Actions",
        description="Clamp all actions instead of only the selected helper action",
        default=False,
    )

    def execute(self, context):
        settings = get_scene_export_settings(context)
        armature = find_target_armature(context)
        if not settings or not armature:
            self.report({"ERROR"}, "Select the target armature first.")
            return {"CANCELLED"}
        root = _best_root_bone_for_ground(armature, getattr(settings, "helper_ground_root_bone", ""))
        feet = _auto_detect_foot_bones(armature, getattr(settings, "helper_left_foot_bone", ""), getattr(settings, "helper_right_foot_bone", ""), use_selected=getattr(settings, "helper_ground_use_selected_bones", True))
        actions = list(bpy.data.actions) if self.apply_all_actions else [_resolve_helper_action(settings, armature)]
        actions = [a for a in actions if a]
        if not actions:
            self.report({"ERROR"}, "No Action found to clamp.")
            return {"CANCELLED"}
        total = 0
        for action in actions:
            ensure_action_channelbag_for_object(action, armature)
            total += _clamp_action_feet_to_ground(action, armature, root, feet, self)
        if total <= 0:
            self.report({"WARNING"}, f"No ground clamp keys changed. Root={root}, feet={', '.join(feet) if feet else 'none'}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Ground clamped {len(actions)} action(s), {total} root Z key(s). Root={root}; feet={', '.join(feet)}")
        return {"FINISHED"}


class HWUGX_OT_solve_walkrun_grounding(bpy.types.Operator):
    bl_idname = "hwugx.solve_walkrun_grounding"
    bl_label = "Solve Walk/Run Grounding"
    bl_description = "Alternating-foot ground solver for walk/run cycles. Chooses the stance foot each frame and adjusts only root Z so feet contact the floor without forcing both feet down at once"
    bl_options = {"REGISTER", "UNDO"}

    apply_all_actions: BoolProperty(
        name="Apply To All Actions",
        description="Solve walk/run grounding for all actions instead of only the selected helper action",
        default=False,
    )

    def execute(self, context):
        settings = get_scene_export_settings(context)
        armature = find_target_armature(context)
        if not settings or not armature:
            self.report({"ERROR"}, "Select the target armature first.")
            return {"CANCELLED"}
        root = _best_root_bone_for_ground(armature, getattr(settings, "helper_ground_root_bone", ""))
        feet = _auto_detect_foot_bones(
            armature,
            getattr(settings, "helper_left_foot_bone", ""),
            getattr(settings, "helper_right_foot_bone", ""),
            use_selected=getattr(settings, "helper_ground_use_selected_bones", True),
        )
        actions = list(bpy.data.actions) if self.apply_all_actions else [_resolve_helper_action(settings, armature)]
        actions = [a for a in actions if a]
        if not actions:
            self.report({"ERROR"}, "No Action found to solve.")
            return {"CANCELLED"}
        total = 0
        for action in actions:
            ensure_action_channelbag_for_object(action, armature)
            total += _solve_walkrun_feet_to_ground(action, armature, root, feet, self)
        if total <= 0:
            self.report({"WARNING"}, f"No walk/run grounding keys changed. Root={root}, contacts={', '.join(feet) if feet else 'none'}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Walk/Run solved {len(actions)} action(s), {total} root Z key(s). Root={root}; contacts={', '.join(feet)}")
        return {"FINISHED"}


class HWUGX_OT_apply_bone_timeline_offset(bpy.types.Operator):
    bl_idname = "hwugx.apply_bone_timeline_offset"
    bl_label = "Apply Timeline Bone Offset"
    bl_description = "Apply a constant location/rotation offset to a selected bone across the whole selected Action"
    bl_options = {"REGISTER", "UNDO"}

    apply_all_actions: BoolProperty(
        name="Apply To All Actions",
        description="Apply this helper offset to every Action instead of only the selected/active Action",
        default=False,
    )

    def execute(self, context):
        settings = get_scene_export_settings(context)
        armature = find_target_armature(context)
        if not settings or not armature:
            self.report({"ERROR"}, "Select the target armature first.")
            return {"CANCELLED"}
        bone_name = getattr(settings, "helper_bone", "")
        if not bone_name or bone_name not in armature.data.bones:
            self.report({"ERROR"}, "Choose a valid helper bone.")
            return {"CANCELLED"}
        actions = list(bpy.data.actions) if self.apply_all_actions else [_resolve_helper_action(settings, armature)]
        actions = [a for a in actions if a]
        if not actions:
            self.report({"ERROR"}, "No Action found to edit.")
            return {"CANCELLED"}
        total = 0
        if getattr(settings, "helper_auto_capture_on_apply", True) and not self.apply_all_actions:
            try:
                _capture_helper_offsets_from_current_pose(settings, armature, actions[0])
            except Exception as exc:
                self.report({"WARNING"}, f"Auto capture skipped: {exc}")
        for action in actions:
            ensure_action_channelbag_for_object(action, armature)
            if settings.helper_apply_location:
                total += _offset_location_curves(action, armature, bone_name, settings.helper_location_offset, settings.helper_create_missing_keys)
            if settings.helper_apply_rotation:
                total += _offset_quaternion_curves(action, armature, bone_name, settings.helper_rotation_offset, settings.helper_create_missing_keys)
            try:
                action["hwugx_helper_modified"] = True
                action["hwugx_helper_last_bone"] = bone_name
            except Exception:
                pass
        self.report({"INFO"}, f"Applied helper offset to {len(actions)} action(s), {total} key value(s) updated/inserted.")
        return {"FINISHED"}


class HWUGX_OT_zero_helper_offsets(bpy.types.Operator):
    bl_idname = "hwugx.zero_helper_offsets"
    bl_label = "Zero Helper Fields"
    bl_description = "Reset the helper offset values back to zero"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = get_scene_export_settings(context)
        if not settings:
            return {"CANCELLED"}
        settings.helper_location_offset = (0.0, 0.0, 0.0)
        settings.helper_rotation_offset = (0.0, 0.0, 0.0)
        self.report({"INFO"}, "Helper offset fields reset.")
        return {"FINISHED"}


class HWUGX_PT_animation_helper_panel(bpy.types.Panel):
    bl_label = "UAX Animation Helper"
    bl_idname = "HWUGX_PT_animation_helper_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UGX"

    def draw_header(self, context):
        self.layout.label(icon="ACTION_TWEAK")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        settings = get_scene_export_settings(context)
        armature = find_target_armature(context)
        if settings is None:
            layout.label(text="Scene settings unavailable.", icon="ERROR")
            return
        status = layout.box()
        row = status.row(align=True)
        row.alert = armature is None
        row.label(text=("Target: " + armature.name if armature else "Select an armature"), icon=("ARMATURE_DATA" if armature else "ERROR"))
        status.prop(settings, "helper_action")
        status.prop(settings, "helper_bone")

        loc_box = layout.box()
        loc_box.label(text="Uniform Bone Offset", icon="ORIENTATION_LOCAL")
        loc_box.prop(settings, "helper_auto_capture_on_apply")
        row = loc_box.row(align=True)
        row.operator("hwugx.capture_helper_offset", text="Capture From Current Pose", icon="EYEDROPPER")
        loc_box.prop(settings, "helper_apply_location")
        sub = loc_box.column()
        sub.enabled = settings.helper_apply_location
        sub.prop(settings, "helper_location_offset")
        loc_box.prop(settings, "helper_apply_rotation")
        sub = loc_box.column()
        sub.enabled = settings.helper_apply_rotation
        sub.prop(settings, "helper_rotation_offset")
        loc_box.prop(settings, "helper_create_missing_keys")

        buttons = layout.row(align=True)
        buttons.operator("hwugx.apply_bone_timeline_offset", text="Apply To Action", icon="CHECKMARK").apply_all_actions = False
        buttons.operator("hwugx.apply_bone_timeline_offset", text="Apply To All", icon="ACTION_TWEAK").apply_all_actions = True
        layout.operator("hwugx.zero_helper_offsets", text="Zero Helper Fields", icon="LOOP_BACK")

        ground_box = layout.box()
        ground_box.label(text="Foot Grounding", icon="CON_FLOOR")
        row = ground_box.row(align=True)
        row.prop(settings, "helper_ground_floor_object")
        row.operator("hwugx.set_floor_from_selected", text="Pick", icon="EYEDROPPER")
        ground_box.prop(settings, "helper_ground_root_bone")
        row = ground_box.row(align=True)
        row.prop(settings, "helper_left_foot_bone")
        row.prop(settings, "helper_right_foot_bone")
        row = ground_box.row(align=True)
        row.operator("hwugx.capture_contact_bones", text="Use Selected Contacts", icon="BONE_DATA")
        row.prop(settings, "helper_ground_use_selected_bones", text="Live Pose Selection")
        ground_box.prop(settings, "helper_ground_use_mesh_contact")
        ground_box.prop(settings, "helper_ground_world_z_only")
        row = ground_box.row(align=True)
        row.operator("hwugx.ground_contacts_to_floor", text="Ground Action", icon="CON_FLOOR").apply_all_actions = False
        row.operator("hwugx.ground_contacts_to_floor", text="Ground All", icon="ACTION_TWEAK").apply_all_actions = True
        adv = ground_box.box()
        adv.scale_y = 0.85
        adv.label(text="Fine Tune", icon="PREFERENCES")
        adv.prop(settings, "helper_walkrun_smoothing")
        adv.prop(settings, "helper_walkrun_max_step")
        adv.prop(settings, "helper_ground_contact_stickiness")

        hint = layout.box()
        hint.scale_y = 0.75
        hint.label(text="Offset helper: pose a bone, then Apply. Auto Capture calculates the timeline offset.", icon="INFO")
        hint.label(text="Grounding: pick a floor, select toe/foot contact bones, then Ground Action.", icon="INFO")

def _ui_card(layout, title: str, icon: str = "NONE", *, alert: bool = False):
    box = layout.box()
    row = box.row(align=True)
    row.alert = alert
    row.label(text=title, icon=icon)
    return box

# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------

class HWUGX_PT_main_panel(bpy.types.Panel):
    bl_label = "Halo Wars UGX Pipeline"
    bl_idname = "HWUGX_PT_main_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "UGX"

    def draw_header(self, context):
        self.layout.label(icon="OUTLINER_OB_ARMATURE")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = False
        ugx_path = get_ugx_exe_path(context)
        converter_ok = bool(ugx_path and os.path.isfile(ugx_path))
        armature = find_target_armature(context)
        settings = get_scene_export_settings(context)

        # Blender panels do not support arbitrary RGB colors, but alert rows/icons
        # give us a clearer colored status feel inside Blender's theme.
        hero = layout.box()
        hero.scale_y = 1.05
        row = hero.row(align=True)
        row.label(text="HALO WARS 2 PIPELINE", icon="SHADERFX")
        status = hero.row(align=True)
        status.alert = not converter_ok
        status.label(text=("Converter Ready" if converter_ok else "Converter Missing"), icon=("CHECKMARK" if converter_ok else "ERROR"))
        status.operator("hwugx.set_converter_path", text="Browse", icon="FILE_FOLDER")
        hero.label(text=(os.path.basename(ugx_path) if converter_ok else "Set ugx.exe before importing/exporting"), icon=("FILE_TICK" if converter_ok else "INFO"))

        import_box = _ui_card(layout, "Import", "IMPORT")
        import_box.operator("import_scene.hw_ugx", text="Import UGX Model", icon="MESH_DATA")
        row = import_box.row(align=True)
        row.alert = armature is None
        row.label(text=("Target: " + armature.name if armature else "Select armature before UAX import"), icon=("ARMATURE_DATA" if armature else "ERROR"))
        import_box.operator("import_anim.hw_uax", text="Import UAX Animation", icon="ACTION")
        note = import_box.column(align=True)
        note.scale_y = 0.75
        note.label(text="UGX cleanup is automatic: HW2 scale, bones, materials, normals, and weights.", icon="CHECKMARK")

        if settings is not None:
            export_box = _ui_card(layout, "Export Settings", "SETTINGS")
            draw_export_settings_ui(export_box, settings, include_hw2=False, include_header=False)
            buttons = export_box.row(align=True)
            buttons.operator("export_scene.hw_ugx", text="Export UGX", icon="EXPORT")
            buttons.operator("export_scene.hw_ugx_animation_sidecar", text="Export UAX", icon="ACTION")


# -----------------------------------------------------------------------------
# v5.3.0 repo-audited template-free UAX binding fixes
# -----------------------------------------------------------------------------
# The old working Blender 4 custom UAX exporter wrote a transform track for the
# whole armature track group, not only the bones that happened to have keys.
# HW2 can load a tiny one-track file without crashing, but the animation may not
# bind to the in-game skeleton. These overrides restore full-skeleton track group
# export and make unkeyed bones write their native rest transforms instead of
# unsafe identity rotations.


def _scratch_export_bone_names(action: bpy.types.Action, armature: bpy.types.Object) -> list[str]:
    """Return the full armature bone list in Blender order for template-free UAX.

    Earlier v5.x builds filtered this down to keyed bones plus the Granny root.
    That matched one tiny legacy test file, but it is not reliable for real HW2
    skeleton binding. The known-good walk01 custom UAX contains every armature
    track, so template-free export now does the same by default.
    """
    if not armature or armature.type != "ARMATURE" or not armature.data:
        return []
    return [b.name for b in armature.data.bones]


def _scratch_pose_quat(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, axis_q: Quaternion) -> Quaternion:
    """Sample a native Granny rotation for one bone.

    Import path from the old script:
        blender_quat = bone.matrix^-1 @ native_quat

    Therefore export must be:
        native_quat = bone.matrix @ blender_quat

    If a bone has no authored rotation curve, Blender's pose value is local
    identity, so the native value must be the bone rest matrix rotation, not a
    raw identity quaternion. This was a major reason exported custom UAX files
    could load in-game but appear to do nothing.
    """
    if not armature or armature.type != "ARMATURE" or bone_name not in armature.data.bones:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    q_blender = _action_quaternion_at(action, bone_name, frame)
    if q_blender is None:
        q_blender = Quaternion((1.0, 0.0, 0.0, 0.0))
    bone = armature.data.bones[bone_name]
    q_uax = bone.matrix.to_quaternion() @ q_blender
    try:
        if axis_q is not None and abs(axis_q.angle) >= 0.000001:
            q_uax = axis_q.inverted() @ q_uax @ axis_q
    except Exception:
        pass
    try:
        q_uax.normalize()
    except Exception:
        q_uax = Quaternion((1.0, 0.0, 0.0, 0.0))
    return q_uax


def _scratch_native_scale_multiplier(armature: bpy.types.Object, settings=None) -> float:
    """Best-effort native scale-shear multiplier for template-free custom UAX.

    v5.3.0 note: the coconutbird/uax crate confirmed that scale-shear is a real
    3x3 curve payload, not just UI padding. The old working custom UAX samples
    the user provided use ~1.6 on the scale-shear diagonal even when Blender has
    no explicit scale keys. If we have an imported HW2 scale, invert that. If not,
    fall back to 1.6 instead of identity so template-free exports match the old
    Blender 4 custom UAX behavior more closely.
    """
    try:
        scale = float(armature.get("hwugx_import_scale", 1.0)) if armature else 1.0
        if abs(scale) > 0.000001 and abs(scale - 1.0) > 0.000001:
            return 1.0 / scale
    except Exception:
        pass
    return 1.6


def _scratch_pose_scale_shear(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float) -> tuple[float, float, float, float, float, float, float, float, float]:
    """Sample native Granny 3x3 scale-shear for one bone.

    Pose scale is multiplied by the native import-scale compensation when present.
    This keeps template-free UAX output closer to old working custom exports while
    still preserving authored Blender scale keys.
    """
    scl = _action_scale_at(action, bone_name, frame)
    native_mul = _scratch_native_scale_multiplier(armature)
    if scl is None:
        sx = sy = sz = native_mul
    else:
        sx = float(scl.x) * native_mul
        sy = float(scl.y) * native_mul
        sz = float(scl.z) * native_mul
    return (sx, 0.0, 0.0, 0.0, sy, 0.0, 0.0, 0.0, sz)


def _build_scratch_granny_chunk_from_action(action: bpy.types.Action, armature: bpy.types.Object, settings, *, operator=None) -> tuple[bytes | None, dict]:
    """Build a template-free UAX Granny chunk using the old-plugin layout.

    v5.2.0 differences from v5.1.0:
      * exports every armature bone track, like the working walk01 UAX;
      * writes native rest rotations for unkeyed bones;
      * writes native-rest position for unkeyed bones;
      * keeps scale-shear curves for every bone.
    """
    stats = {"tracks": 0, "rot_keys": 0, "pos_keys": 0, "duration": 0.0, "error": ""}
    if not action or not armature or armature.type != "ARMATURE":
        stats["error"] = "missing_action_or_armature"
        return None, stats

    fps = bpy.context.scene.render.fps / max(bpy.context.scene.render.fps_base, 0.0001)
    frames = _scratch_action_sample_frames(action, every_frame=True)
    if not frames:
        stats["error"] = "no_action_frames"
        return None, stats

    start = min(frames)
    duration = max(0.001, (max(frames) - start) / max(fps, 0.0001))
    times = [max(0.0, (float(f) - start) / max(fps, 0.0001)) for f in frames]
    count = len(times)
    stats["duration"] = duration

    bone_names = _scratch_export_bone_names(action, armature)
    if not bone_names:
        stats["error"] = "no_bones"
        return None, stats

    root_name = _scratch_root_bone_name_for_group(armature, bone_names)
    axis_m = _settings_axis_matrix(settings)
    axis_q = _settings_axis_quaternion(settings) if not _scratch_export_axes_are_default(settings) else Quaternion((1.0, 0.0, 0.0, 0.0))

    arena = _BinaryArena(0x94)
    art_tool_info_off = arena.add_zeros(len(_HWUGX_OLD_ART_TOOL_INFO_TEMPLATE), align=4)
    track_group_ptr_list_off = arena.add_zeros(8, align=4)
    group_off = arena.add_zeros(len(_HWUGX_OLD_TRACK_GROUP_TEMPLATE), align=4)
    tracks_off = arena.add_zeros(len(bone_names) * 60, align=4)
    type_off = arena.add_zeros(len(_HWUGX_OLD_DAK32FC32F_TYPE_TEMPLATE), align=4)

    curve_array_info = []
    stats["scale_keys"] = 0
    stats["scale_min"] = None
    stats["scale_max"] = None
    stats["exported_bones"] = bone_names
    stats["keyed_bones"] = sorted(_action_keyed_bone_names(action))
    stats["native_scale_multiplier"] = _scratch_native_scale_multiplier(armature)
    stats["repo_audit"] = "coconutbird/uax: chunk_is_file_info, 0x94 header, 0x3c transform tracks, DaK32fC32f curve payloads"

    for bone_name in bone_names:
        quats = [_scratch_pose_quat(action, armature, bone_name, f, axis_q) for f in frames]
        poss = [_scratch_pose_pos(action, armature, bone_name, f, settings, axis_q, axis_m) for f in frames]
        scales = [_scratch_pose_scale_shear(action, armature, bone_name, f) for f in frames]
        rot_knot_off = _scratch_add_float_array(arena, times, 1)
        rot_ctrl_off = _scratch_add_float_array(arena, [(q.x, q.y, q.z, q.w) for q in quats], 4)
        pos_knot_off = _scratch_add_float_array(arena, times, 1)
        pos_ctrl_off = _scratch_add_float_array(arena, [(v.x, v.y, v.z) for v in poss], 3)
        scl_knot_off = _scratch_add_float_array(arena, times, 1)
        scl_ctrl_off = _scratch_add_float_array(arena, scales, 9)
        curve_array_info.append((rot_knot_off, rot_ctrl_off, pos_knot_off, pos_ctrl_off, scl_knot_off, scl_ctrl_off))
        stats["tracks"] += 1
        stats["rot_keys"] += len(quats)
        stats["pos_keys"] += len(poss)
        if _action_has_scale_curves(action, bone_name):
            stats["scale_keys"] += len(scales)
        for ss in scales:
            for idx in (0, 4, 8):
                val = float(ss[idx])
                stats["scale_min"] = val if stats["scale_min"] is None else min(float(stats["scale_min"]), val)
                stats["scale_max"] = val if stats["scale_max"] is None else max(float(stats["scale_max"]), val)

    # Curve headers.
    curve_header_info = []
    for rot_knot_off, rot_ctrl_off, pos_knot_off, pos_ctrl_off, scl_knot_off, scl_ctrl_off in curve_array_info:
        rot_obj = arena.add(struct.pack("<IIQIQ", 0x201, count, rot_knot_off, count * 4, rot_ctrl_off), align=4)
        pos_obj = arena.add(struct.pack("<IIQIQ", 0x201, count, pos_knot_off, count * 3, pos_ctrl_off), align=4)
        scl_obj = arena.add(struct.pack("<IIQIQ", 0x201, count, scl_knot_off, count * 9, scl_ctrl_off), align=4)
        curve_header_info.append((rot_obj, pos_obj, scl_obj))

    animation_ptr_list_off = arena.add_zeros(8, align=4)
    animation_off = arena.add_zeros(100, align=4)
    animation_group_ref_list_off = arena.add_zeros(8, align=4)

    # Strings live late in the file in old working exports.
    string_offsets = {}
    for name in bone_names:
        if name not in string_offsets:
            string_offsets[name] = arena.add_cstring(name, align=4)
    if root_name not in string_offsets:
        string_offsets[root_name] = arena.add_cstring(root_name, align=4)
    for text in ("CurveDataHeader_DaK32fC32f", "Format", "Degree", "Padding", "Knots", "Real32", "Controls", "gr2ugx"):
        if text not in string_offsets:
            string_offsets[text] = arena.add_cstring(text, align=4)
    art_tool_string = "Blender 4.0.0 commit date:2023-11-13, commit time:17:26, hash:878f71061b8e"
    string_offsets[art_tool_string] = arena.add_cstring(art_tool_string, align=4)
    default_name_off = arena.add_cstring("Default", align=4)

    arena.patch(art_tool_info_off, _patch_old_art_tool_info(_HWUGX_OLD_ART_TOOL_INFO_TEMPLATE, string_offsets[art_tool_string]))
    arena.patch(type_off, _patch_old_dak32fc32f_type(_HWUGX_OLD_DAK32FC32F_TYPE_TEMPLATE, string_offsets, type_off))

    # Tracks.
    for i, bone_name in enumerate(bone_names):
        rot_obj, pos_obj, scl_obj = curve_header_info[i]
        track = bytearray(60)
        struct.pack_into("<QI", track, 0, string_offsets[bone_name], 0)
        struct.pack_into("<QQ", track, 12, type_off, rot_obj)
        struct.pack_into("<QQ", track, 28, type_off, pos_obj)
        struct.pack_into("<QQ", track, 44, type_off, scl_obj)
        arena.patch(tracks_off + i * 60, bytes(track))

    group = bytearray(_HWUGX_OLD_TRACK_GROUP_TEMPLATE)
    struct.pack_into("<Q", group, 0, string_offsets[root_name])
    struct.pack_into("<IQ", group, 20, len(bone_names), tracks_off)
    struct.pack_into("<I", group, 124, 0x2)
    try:
        struct.pack_into("<Q", group, 164, string_offsets[root_name])
    except Exception:
        pass
    arena.patch(group_off, bytes(group))
    arena.patch(track_group_ptr_list_off, struct.pack("<Q", group_off))

    # Animation object mirrors old generated UAX: internal clip name is Default,
    # duration/timestep/track-group reference list included.
    anim = bytearray(100)
    struct.pack_into("<Q", anim, 0, default_name_off)
    struct.pack_into("<ff", anim, 8, float(duration), 0.04)
    struct.pack_into("<f", anim, 16, 1.0)
    struct.pack_into("<I", anim, 20, 1)
    struct.pack_into("<Q", anim, 24, animation_group_ref_list_off)
    struct.pack_into("<II", anim, 32, 1, 1)
    struct.pack_into("<Q", anim, 56, group_off)
    arena.patch(animation_off, bytes(anim))
    arena.patch(animation_group_ref_list_off, struct.pack("<Q", group_off))
    arena.patch(animation_ptr_list_off, struct.pack("<Q", animation_off))

    file_info = bytearray(0xBC)
    struct.pack_into("<Q", file_info, 0, art_tool_info_off)
    struct.pack_into("<Q", file_info, 16, string_offsets["gr2ugx"])
    struct.pack_into("<IQ", file_info, 108, 1, track_group_ptr_list_off)
    struct.pack_into("<IQ", file_info, 120, 1, animation_ptr_list_off)
    struct.pack_into("<Q", file_info, 148, art_tool_info_off)
    struct.pack_into("<I", file_info, 156, 1)
    struct.pack_into("<I", file_info, 164, 32)
    struct.pack_into("<f", file_info, 168, 1.0)
    struct.pack_into("<f", file_info, 184, 1.0)
    arena.patch(0, bytes(file_info))

    granny = arena.bytes()
    stats["granny_size"] = len(granny)
    return granny, stats



# v5.3.0 repo audit notes:
# The coconutbird/ensemble-formats uax crate confirms that a UAX chunk is the
# Granny file_info directly, with 64-bit offsets relative to the chunk start.
# It also models transform tracks as 0x3c-byte records and DaK32fC32f curves as
# format=1 / degree=2 / padding + knot ref_arr + control ref_arr. The scratch
# writer above keeps the old working HW Suite 0xac track-group record footprint
# because uploaded old-addon UAX files and the old importer use a 172-byte
# trackGroupSize, but the individual transform tracks/curve payloads now match
# the crate's interpretation.



# -----------------------------------------------------------------------------
# v5.6.0 root-only structure export compatibility
# -----------------------------------------------------------------------------
# HW2 can load a template-free UAX but still appear static when a custom structure
# animation only keys GrannyRootBone_* orientation/location. Existing working
# custom root-only UAX files generated by the old pipeline put the visible motion
# into the scale-shear curve while keeping root orientation effectively stable.
# These overrides keep normal multi-bone walk/run exports untouched, but for
# one-track GrannyRootBone actions they bake the authored pose rotation into the
# native 3x3 scale-shear matrix. This makes root-only structure animations much
# closer to the old Blender 4 exporter behavior and avoids relying on root-motion
# channels that the HW2 runtime may discard for entity placement.


def _hwugx_action_bone_has_curve(action, bone_name: str, prop: str) -> bool:
    try:
        path = f'pose.bones["{bone_name}"].{prop}'
        for fc in iter_action_fcurves(action):
            if getattr(fc, 'data_path', '') == path:
                return True
    except Exception:
        pass
    return False


def _hwugx_is_root_only_structure_action(action, armature, bone_name: str) -> bool:
    try:
        if not action or not armature or armature.type != 'ARMATURE':
            return False
        if not bone_name or not bone_name.startswith('GrannyRootBone'):
            return False
        keyed = set(_action_keyed_bone_names(action))
        keyed = {k for k in keyed if k in armature.data.bones}
        if keyed and keyed - {bone_name}:
            return False
        # Only engage when the root really has authored transform curves. Static
        # roots should stay as the normal legacy constant root track.
        return (
            _hwugx_action_bone_has_curve(action, bone_name, 'rotation_quaternion')
            or _hwugx_action_bone_has_curve(action, bone_name, 'rotation_euler')
            or _hwugx_action_bone_has_curve(action, bone_name, 'location')
            or _hwugx_action_bone_has_curve(action, bone_name, 'scale')
        )
    except Exception:
        return False


def _hwugx_matrix3_to_rows(m):
    return (
        float(m[0][0]), float(m[0][1]), float(m[0][2]),
        float(m[1][0]), float(m[1][1]), float(m[1][2]),
        float(m[2][0]), float(m[2][1]), float(m[2][2]),
    )


def _scratch_pose_quat(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, axis_q: Quaternion) -> Quaternion:
    """Sample native Granny rotation for one bone.

    v5.6 override: if this is a root-only structure action, keep the root
    orientation stable and let _scratch_pose_scale_shear() carry the visible
    authored rotation. This mirrors the behavior seen in old working custom UAX
    files and avoids HW2 ignoring root-motion orientation channels.
    """
    if not armature or armature.type != "ARMATURE" or bone_name not in armature.data.bones:
        return Quaternion((1.0, 0.0, 0.0, 0.0))
    bone = armature.data.bones[bone_name]
    if _hwugx_is_root_only_structure_action(action, armature, bone_name):
        q_uax = bone.matrix.to_quaternion()
    else:
        q_blender = _action_quaternion_at(action, bone_name, frame)
        if q_blender is None:
            q_blender = Quaternion((1.0, 0.0, 0.0, 0.0))
        q_uax = bone.matrix.to_quaternion() @ q_blender
    try:
        if axis_q is not None and abs(axis_q.angle) >= 0.000001:
            q_uax = axis_q.inverted() @ q_uax @ axis_q
    except Exception:
        pass
    try:
        q_uax.normalize()
    except Exception:
        q_uax = Quaternion((1.0, 0.0, 0.0, 0.0))
    return q_uax


def _scratch_pose_pos(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, settings, axis_q: Quaternion, axis_m: Matrix) -> Vector:
    """Sample native Granny position for one bone.

    v5.6 override: for root-only structure clips, root location is intentionally
    zeroed. HW2 commonly treats GrannyRootBone movement as entity/root motion and
    discards it for placement. Keeping it out of position prevents a valid UAX
    from being loaded as a no-op/placement-only clip.
    """
    if _hwugx_is_root_only_structure_action(action, armature, bone_name):
        return Vector((0.0, 0.0, 0.0))
    vec = _action_location_at(action, bone_name, frame)
    if vec is None:
        try:
            bone = armature.data.bones[bone_name]
            vec = bone.head.copy()
        except Exception:
            vec = Vector((0.0, 0.0, 0.0))
    else:
        try:
            bone = armature.data.bones[bone_name]
            vec = vec + bone.head
            if bone.parent:
                vec.z = -vec.z
        except Exception:
            pass
    try:
        if not _scratch_export_axes_are_default(settings):
            vec = axis_m @ vec
    except Exception:
        pass
    return vec


def _scratch_pose_scale_shear(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float) -> tuple[float, float, float, float, float, float, float, float, float]:
    """Sample native Granny 3x3 scale-shear for one bone.

    v5.6 override: for a root-only GrannyRootBone action, bake the authored root
    rotation into the 3x3 scale-shear matrix. This is the key difference from
    v5.5, whose exported UAX loaded but appeared static in-game because the
    visible motion lived in root orientation/location channels.
    """
    native_mul = _scratch_native_scale_multiplier(armature)
    scl = _action_scale_at(action, bone_name, frame)
    if scl is None:
        sx = sy = sz = native_mul
    else:
        sx = float(scl.x) * native_mul
        sy = float(scl.y) * native_mul
        sz = float(scl.z) * native_mul
    if _hwugx_is_root_only_structure_action(action, armature, bone_name):
        q = _action_quaternion_at(action, bone_name, frame)
        if q is None:
            q = Quaternion((1.0, 0.0, 0.0, 0.0))
        try:
            q.normalize()
            m = q.to_matrix().to_3x3()
            # Matrix * diagonal scale. Stored row-major, matching old DaK32fC32f
            # scale-shear controls observed in working exports.
            m[0][0] *= sx; m[0][1] *= sy; m[0][2] *= sz
            m[1][0] *= sx; m[1][1] *= sy; m[1][2] *= sz
            m[2][0] *= sx; m[2][1] *= sy; m[2][2] *= sz
            return _hwugx_matrix3_to_rows(m)
        except Exception:
            pass
    return (sx, 0.0, 0.0, 0.0, sy, 0.0, 0.0, 0.0, sz)



# v5.8.0 hotfix: previous root-only structure detection accidentally used the
# removed helper name `_iter_action_fcurves`, so the scale-shear bake never
# activated. Keep this compatibility alias too, in case future helper blocks use
# the legacy name again.
def _iter_action_fcurves(action):
    return iter_action_fcurves(action)


def menu_import_ugx(self, context):
    self.layout.operator(HWUGX_OT_import_ugx.bl_idname, text="Halo Wars UGX Model (.ugx)")


def menu_import_uax(self, context):
    self.layout.operator(HWUGX_OT_import_uax.bl_idname, text="Halo Wars UAX Animation (.uax)")


def menu_export_ugx(self, context):
    self.layout.operator(HWUGX_OT_export_ugx.bl_idname, text="Halo Wars UGX Model (.ugx)")


classes = (
    HWUGX_AddonPreferences,
    HWUGX_ExportSettings,
    HWUGX_OT_import_ugx,
    HWUGX_OT_export_ugx,
    HWUGX_OT_import_uax,
    HWUGX_OT_fix_selected_cleanup,
    HWUGX_OT_repair_skin_weights,
    HWUGX_OT_set_converter_path,
    HWUGX_OT_export_animation_gltf,
    HWUGX_OT_capture_helper_offset,
    HWUGX_OT_set_floor_from_selected,
    HWUGX_OT_capture_contact_bones,
    HWUGX_OT_ground_contacts_to_floor,
    HWUGX_OT_clamp_feet_to_ground,
    HWUGX_OT_solve_walkrun_grounding,
    HWUGX_OT_apply_bone_timeline_offset,
    HWUGX_OT_zero_helper_offsets,
    HWUGX_PT_main_panel,
    HWUGX_PT_animation_helper_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.hwugx_export_settings = PointerProperty(type=HWUGX_ExportSettings)
    bpy.types.TOPBAR_MT_file_import.append(menu_import_ugx)
    bpy.types.TOPBAR_MT_file_import.append(menu_import_uax)
    bpy.types.TOPBAR_MT_file_export.append(menu_export_ugx)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(menu_export_ugx)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import_uax)
    bpy.types.TOPBAR_MT_file_import.remove(menu_import_ugx)
    if hasattr(bpy.types.Scene, "hwugx_export_settings"):
        del bpy.types.Scene.hwugx_export_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)




# -----------------------------------------------------------------------------
# v5.4.0 native UGX skeleton basis cache / template-free UAX export fix
# -----------------------------------------------------------------------------
# The glTF bridge is excellent for Blender editing, but glTF conversion can change
# the armature rest basis compared with the native UGX/Granny skeleton. The old
# direct Blender importer built bones from the UGX Granny chunk and UAX import/
# export math was based on those native matrices. Template-free UAX export must
# use that native rest basis or HW2 can load the file but not visibly apply the
# clip. These late-bound overrides keep the UI/workflow intact while restoring the
# old native-basis math for custom UAX export.

_HWUGX_NATIVE_SKEL_PROP = "hwugx_native_skeleton_json"


def _hwugx_read_cstr_blob(data: bytes, offset: int) -> str:
    try:
        offset = int(offset)
        if offset < 0 or offset >= len(data):
            return ""
        end = data.find(b"\x00", offset)
        if end < 0:
            end = min(len(data), offset + 256)
        return data[offset:end].decode("utf-8", "replace")
    except Exception:
        return ""


def _extract_ugx_granny_chunk(ugx_path: str) -> bytes | None:
    try:
        with open(ugx_path, "rb") as f:
            fd = f.read()
        if len(fd) < 64:
            return None
        # UGX/ECF chunk table: same pattern used by the original HW Suite importer.
        possible_counts = []
        try:
            possible_counts.append(struct.unpack("h", fd[17:19])[0])
        except Exception:
            pass
        try:
            possible_counts.append(struct.unpack(">H", fd[16:18])[0])
        except Exception:
            pass
        for num_chunks in possible_counts:
            if num_chunks <= 0 or num_chunks > 64:
                continue
            table_ok = True
            for i in range(num_chunks):
                off = 32 + i * 24
                if off + 16 > len(fd):
                    table_ok = False
                    break
                try:
                    cid, chunk_off, chunk_len = struct.unpack(">QII", fd[off:off + 16])
                except Exception:
                    table_ok = False
                    break
                if cid == 0x703 and 0 <= chunk_off < len(fd) and chunk_len > 0 and chunk_off + chunk_len <= len(fd):
                    return fd[chunk_off:chunk_off + chunk_len]
            if table_ok:
                pass
    except Exception:
        return None
    return None


def _extract_native_skeleton_from_ugx(ugx_path: str) -> dict | None:
    granny = _extract_ugx_granny_chunk(ugx_path)
    if not granny or len(granny) < 128:
        return None
    try:
        skel_off = struct.unpack("<Q", granny[52:60])[0]
        bones_len, bones_off = struct.unpack("<IQ", granny[skel_off + 24: skel_off + 36])
        if bones_len <= 0 or bones_len > 512:
            return None
        bones = []
        for i in range(int(bones_len)):
            cur = int(bones_off) + i * 164
            if cur + 144 > len(granny):
                return None
            name_off, parent_index = struct.unpack("<Qi", granny[cur:cur + 12])
            name = _hwugx_read_cstr_blob(granny, name_off)
            if not name:
                name = f"bone_{i}"
            vals = struct.unpack("<ffffffffffffffff", granny[cur + 80:cur + 144])
            mat = mathutils.Matrix((vals[0:4], vals[4:8], vals[8:12], vals[12:16]))
            # This exactly mirrors the old direct importer before assigning
            # bpyBone.matrix = invWorldMat.
            mat.invert()
            mat.transpose()
            bones.append({
                "name": name,
                "parent_index": int(parent_index),
                "parent": "",
                "matrix": [[float(mat[r][c]) for c in range(4)] for r in range(4)],
                "head": [float(mat.translation.x), float(mat.translation.y), float(mat.translation.z)],
            })
        for i, b in enumerate(bones):
            pi = int(b.get("parent_index", -1))
            if 0 <= pi < len(bones):
                b["parent"] = bones[pi]["name"]
        return {"source": os.path.basename(ugx_path), "bone_count": len(bones), "bones": bones}
    except Exception:
        return None


def _attach_ugx_native_skeleton_to_armatures(imported_objects, ugx_path: str, operator=None):
    skel = _extract_native_skeleton_from_ugx(ugx_path)
    if not skel:
        if operator:
            operator.report({"WARNING"}, "Could not read native UGX skeleton basis; custom UAX export will use glTF rest basis.")
        return False
    blob = json.dumps(skel, separators=(",", ":"))
    count = 0
    for obj in imported_objects:
        try:
            if obj and obj.type == "ARMATURE":
                obj[_HWUGX_NATIVE_SKEL_PROP] = blob
                obj["hwugx_native_skeleton_source"] = skel.get("source", "")
                obj["hwugx_native_skeleton_bone_count"] = int(skel.get("bone_count", 0))
                count += 1
        except Exception:
            pass
    if operator and count:
        operator.report({"INFO"}, f"Cached native UGX skeleton basis for UAX export ({skel.get('bone_count', 0)} bones).")
    return bool(count)


def _native_skeleton_map(armature: bpy.types.Object) -> dict:
    try:
        raw = armature.get(_HWUGX_NATIVE_SKEL_PROP, "") if armature else ""
        if not raw:
            return {}
        data = json.loads(raw)
        out = {}
        for b in data.get("bones", []):
            name = b.get("name", "")
            if name:
                out[name] = b
        return out
    except Exception:
        return {}


def _native_matrix_for_bone(armature: bpy.types.Object, bone_name: str):
    try:
        item = _native_skeleton_map(armature).get(bone_name)
        if not item:
            return None
        rows = item.get("matrix")
        if not rows or len(rows) != 4:
            return None
        return mathutils.Matrix(tuple(tuple(float(x) for x in row) for row in rows))
    except Exception:
        return None


def _native_head_for_bone(armature: bpy.types.Object, bone_name: str):
    try:
        item = _native_skeleton_map(armature).get(bone_name)
        if item and "head" in item:
            return Vector(tuple(float(x) for x in item["head"][:3]))
        mat = _native_matrix_for_bone(armature, bone_name)
        if mat is not None:
            return Vector(mat.translation)
    except Exception:
        pass
    try:
        return Vector(armature.data.bones[bone_name].head)
    except Exception:
        return Vector((0.0, 0.0, 0.0))


def _native_parent_name_for_bone(armature: bpy.types.Object, bone_name: str) -> str:
    try:
        item = _native_skeleton_map(armature).get(bone_name)
        if item:
            return str(item.get("parent", ""))
    except Exception:
        pass
    try:
        p = armature.data.bones[bone_name].parent
        return p.name if p else ""
    except Exception:
        return ""


def _scratch_export_bone_names(action: bpy.types.Action, armature: bpy.types.Object) -> list[str]:
    """Full native skeleton order for template-free UAX export.

    v5.4.0: prefer UGX-native bone order cached at import time. The old direct
    pipeline exported against native Granny skeleton order/basis, while the glTF
    bridge may reorder or re-basis the Blender armature for editing.
    """
    if not armature or armature.type != "ARMATURE" or not armature.data:
        return []
    native = _native_skeleton_map(armature)
    if native:
        ordered = []
        # Preserve JSON order from the UGX chunk.
        try:
            data = json.loads(armature.get(_HWUGX_NATIVE_SKEL_PROP, ""))
            for b in data.get("bones", []):
                name = b.get("name", "")
                if name and name in armature.data.bones and name not in ordered:
                    ordered.append(name)
        except Exception:
            pass
        # Add any Blender-only bones last, but normal exports should not need this.
        for b in armature.data.bones:
            if b.name not in ordered:
                ordered.append(b.name)
        return ordered
    return [b.name for b in armature.data.bones]


def _scratch_root_bone_name_for_group(armature: bpy.types.Object, bone_names: list[str]) -> str:
    native_names = []
    try:
        data = json.loads(armature.get(_HWUGX_NATIVE_SKEL_PROP, "")) if armature else {}
        native_names = [b.get("name", "") for b in data.get("bones", [])]
    except Exception:
        native_names = []
    for name in native_names + list(bone_names or []):
        if str(name).startswith("GrannyRootBone"):
            return str(name)
    if bone_names:
        return bone_names[0]
    return armature.data.bones[0].name if armature and armature.type == "ARMATURE" and armature.data.bones else "GrannyRootBone"


def _scratch_pose_quat(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, axis_q: Quaternion) -> Quaternion:
    """Native Granny rotation sample using UGX-native rest basis when available."""
    q_blender = _action_quaternion_at(action, bone_name, frame)
    if q_blender is None:
        q_blender = Quaternion((1.0, 0.0, 0.0, 0.0))
    native_mat = _native_matrix_for_bone(armature, bone_name)
    if native_mat is not None:
        q_uax = native_mat.to_quaternion() @ q_blender
    else:
        try:
            bone = armature.data.bones[bone_name]
            q_uax = bone.matrix.to_quaternion() @ q_blender
        except Exception:
            q_uax = Quaternion((1.0, 0.0, 0.0, 0.0))
    try:
        if axis_q is not None and abs(axis_q.angle) >= 0.000001:
            q_uax = axis_q.inverted() @ q_uax @ axis_q
    except Exception:
        pass
    try:
        q_uax.normalize()
    except Exception:
        q_uax = Quaternion((1.0, 0.0, 0.0, 0.0))
    return q_uax


def _scratch_pose_pos(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, settings, axis_q: Quaternion, axis_m: Matrix) -> Vector:
    """Native Granny position sample using UGX-native rest head when available."""
    loc = _action_location_at(action, bone_name, frame)
    if loc is None:
        loc = Vector((0.0, 0.0, 0.0))
    # Blender action locations are in edited/import-scaled Blender units. Convert
    # only the delta back to native units, then add the native rest head from UGX.
    try:
        import_scale = float(armature.get("hwugx_import_scale", 1.0)) if armature else 1.0
    except Exception:
        import_scale = 1.0
    if abs(import_scale) > 0.000001:
        native_delta = Vector(loc) / import_scale
    else:
        native_delta = Vector(loc)
    final = _native_head_for_bone(armature, bone_name) + native_delta
    # Reverse the old UAX import child-Z flip only when a native/Blender parent exists.
    if _native_parent_name_for_bone(armature, bone_name):
        final.z = -final.z
    try:
        if axis_q is not None and abs(axis_q.angle) >= 0.000001:
            final = axis_q.inverted() @ final
    except Exception:
        pass
    try:
        if not _scratch_export_axes_are_default(settings):
            final = axis_m @ final
    except Exception:
        pass
    return final


def _write_scratch_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    granny, stats = _build_scratch_granny_chunk_from_action(action, armature, settings, operator=operator)
    if not granny:
        if operator:
            operator.report({"ERROR"}, f"Could not build scratch UAX for {action.name}: {stats.get('error', 'unknown')}")
        return False
    raw = _build_ecf_uax_from_granny_chunk(granny)
    os.makedirs(os.path.dirname(uax_path), exist_ok=True)
    with open(uax_path, "wb") as f:
        f.write(raw)
    try:
        report_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({
                "version": "5.4.0",
                "action": action.name,
                "armature": armature.name if armature else "",
                "output": uax_path,
                "tracks": stats.get("tracks", 0),
                "duration": stats.get("duration", 0.0),
                "rot_keys": stats.get("rot_keys", 0),
                "pos_keys": stats.get("pos_keys", 0),
                "scale_keys": stats.get("scale_keys", 0),
                "native_skeleton_cached": bool(_native_skeleton_map(armature)),
                "native_skeleton_source": armature.get("hwugx_native_skeleton_source", "") if armature else "",
                "native_basis_note": "Template-free UAX export used UGX-native skeleton matrices when cached; reimport the UGX with v5.4+ if this is false.",
            }, f, indent=2)
    except Exception:
        pass
    if operator:
        native_note = "native UGX basis" if _native_skeleton_map(armature) else "glTF fallback basis"
        operator.report({"INFO"}, f"Exported template-free UAX: {os.path.basename(uax_path)} ({len(raw):,} bytes, tracks {stats.get('tracks', 0)}, {native_note})")
        if not _native_skeleton_map(armature):
            operator.report({"WARNING"}, "This armature has no cached native UGX skeleton basis. Reimport the UGX with v5.4+ before exporting custom UAX for best in-game accuracy.")
    return True



# -----------------------------------------------------------------------------
# v5.5.0 template-free UAX visibility fix
# -----------------------------------------------------------------------------
# The lekgolo working custom UAX from the old plug-in exports only the actually
# animated GrannyRootBone track. v5.4 exported cached/native helper tracks too
# (bone_vfx_main, Sparks, Streamers, etc.) when they only had constant/rest keys.
# HW2 loads that file, but for this model it can bind the clip without applying
# the visible motion. These late-bound overrides prune static tracks by default
# while still keeping full animated walk/run actions when their bones really move.


def _hwugx_fcurve_values_vary(fc, eps: float = 0.00001) -> bool:
    try:
        vals = [float(kp.co[1]) for kp in getattr(fc, "keyframe_points", [])]
        if len(vals) <= 1:
            return False
        return (max(vals) - min(vals)) > float(eps)
    except Exception:
        return False


def _hwugx_action_significantly_animated_bones(action: bpy.types.Action, eps: float = 0.00001) -> set[str]:
    """Bones whose authored curves actually change over time.

    Blender actions often contain constant location/rotation/scale keys for helper
    bones after pose baking or whole-armature key insertion. The old working UAX
    exporter did not emit those static helper tracks for the lekgolo wall test;
    exporting them can make the in-game clip load but visually do nothing.
    """
    animated = set()
    if not action:
        return animated
    for fc in iter_action_fcurves(action):
        path = getattr(fc, "data_path", "")
        if not path.startswith('pose.bones["'):
            continue
        try:
            bone_name = path.split('pose.bones["', 1)[1].split('"]', 1)[0]
        except Exception:
            continue
        # Keep any bone with real value changes. A constant keyed curve is treated
        # as rest/helper data and is pruned unless no animated tracks exist.
        if _hwugx_fcurve_values_vary(fc, eps=eps):
            animated.add(bone_name)
    return animated


def _hwugx_native_bone_order(armature: bpy.types.Object) -> list[str]:
    if not armature or armature.type != "ARMATURE" or not armature.data:
        return []
    ordered = []
    try:
        data = json.loads(armature.get(_HWUGX_NATIVE_SKEL_PROP, "")) if armature else {}
        for b in data.get("bones", []):
            name = b.get("name", "")
            if name and name in armature.data.bones and name not in ordered:
                ordered.append(name)
    except Exception:
        pass
    for b in armature.data.bones:
        if b.name not in ordered:
            ordered.append(b.name)
    return ordered


def _scratch_export_bone_names(action: bpy.types.Action, armature: bpy.types.Object) -> list[str]:
    """Export only visible/animated tracks, with root first.

    This is the important behavioral correction after v5.4. The exporter now
    behaves closer to the old Blender 4 custom-UAX path:
      * animated bones are included;
      * static helper/VFX/rest tracks are pruned;
      * the GrannyRootBone/root track is included first for binding;
      * if the Action only animates the root, the output becomes a one-track UAX,
        matching the old working lekgolo test file.
    """
    all_bones = _hwugx_native_bone_order(armature)
    if not all_bones:
        return []
    root_name = _scratch_root_bone_name_for_group(armature, all_bones)
    animated = _hwugx_action_significantly_animated_bones(action)
    keyed = _action_keyed_bone_names(action)

    chosen = []
    if root_name in all_bones:
        # Always include the root binding track, but only this plus animated bones,
        # not every constant helper track.
        chosen.append(root_name)

    if animated:
        for name in all_bones:
            if name in animated and name not in chosen:
                chosen.append(name)
    elif keyed:
        # Fallback for deliberately constant pose clips: keep keyed tracks rather
        # than exporting the full skeleton.
        for name in all_bones:
            if name in keyed and name not in chosen:
                chosen.append(name)

    if not chosen and all_bones:
        chosen = [root_name if root_name in all_bones else all_bones[0]]
    return chosen


def _scratch_native_scale_multiplier(armature: bpy.types.Object, settings=None) -> float:
    """Use the legacy custom UAX scale-shear baseline by default.

    The old lekgolo custom UAX that plays in HW2 uses ~1.6 on the scale-shear
    diagonal. v5.4 used the Blender import scale inverse (about 1.575), which is
    mathematically tidy but does not match the known-good custom-export baseline.
    Authored Blender scale curves are still multiplied by this native baseline.
    """
    return 1.6


def _write_scratch_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    granny, stats = _build_scratch_granny_chunk_from_action(action, armature, settings, operator=operator)
    if not granny:
        if operator:
            operator.report({"ERROR"}, f"Could not build scratch UAX for {action.name}: {stats.get('error', 'unknown')}")
        return False
    raw = _build_ecf_uax_from_granny_chunk(granny)
    os.makedirs(os.path.dirname(uax_path), exist_ok=True)
    with open(uax_path, "wb") as f:
        f.write(raw)
    try:
        report_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({
                "version": "5.5.0",
                "action": action.name,
                "armature": armature.name if armature else "",
                "output": uax_path,
                "tracks": stats.get("tracks", 0),
                "duration": stats.get("duration", 0.0),
                "rot_keys": stats.get("rot_keys", 0),
                "pos_keys": stats.get("pos_keys", 0),
                "scale_keys": stats.get("scale_keys", 0),
                "exported_bones": stats.get("exported_bones", []),
                "animated_bones_detected": sorted(_hwugx_action_significantly_animated_bones(action)),
                "keyed_bones": sorted(_action_keyed_bone_names(action)),
                "static_track_pruning": True,
                "native_scale_multiplier": _scratch_native_scale_multiplier(armature),
                "native_skeleton_cached": bool(_native_skeleton_map(armature)),
                "native_skeleton_source": armature.get("hwugx_native_skeleton_source", "") if armature else "",
                "note": "v5.5 exports root + significantly animated tracks only. Constant helper/VFX tracks are pruned to match the old working lekgolo custom UAX behavior.",
            }, f, indent=2)
    except Exception:
        pass
    if operator:
        operator.report({"INFO"}, f"Exported template-free UAX: {os.path.basename(uax_path)} ({len(raw):,} bytes, tracks {stats.get('tracks', 0)}, static helpers pruned)")
    return True

if __name__ == "__main__":
    register()

# -----------------------------------------------------------------------------
# v5.7.0 UAX metadata repair / old custom-export compatibility
# -----------------------------------------------------------------------------
# Uploaded working old-plugin UAX files reveal a subtle but important difference:
# the art_tool_info block at Granny chunk offset 0x94 must point to the real
# "Blender 4.0.0 ..." string in the late string table. Some prior scratch-writer
# paths accidentally left that first qword pointing back at 0x94 (the
# art_tool_info struct itself). The file still loads, but HW2 can treat the clip
# as a valid/static no-op. Repairing these pointers makes template-free output
# match the old add-on's working files much more closely.

def _hwugx_repair_template_free_uax_raw(raw: bytes) -> bytes:
    import struct as _struct
    data = bytearray(raw or b"")
    try:
        if len(data) < 0x40 or _struct.unpack_from(">I", data, 0)[0] != 0xDABA7737:
            return bytes(data)
        # Current writer uses one ECF chunk at table offset 0x20.
        cid, chunk_off, chunk_len = _struct.unpack_from(">QII", data, 0x20)
        if cid != 0x700 or chunk_off <= 0 or chunk_off + chunk_len > len(data):
            return bytes(data)
        g0 = int(chunk_off)
        g1 = g0 + int(chunk_len)
        granny = data[g0:g1]

        def find_in_granny(text: bytes) -> int:
            idx = bytes(granny).find(text)
            return idx if idx >= 0 else -1

        # file_info.ExporterName should point to gr2ugx.
        gr2ugx_off = find_in_granny(b"gr2ugx\x00")
        if gr2ugx_off >= 0:
            _struct.pack_into("<Q", data, g0 + 16, gr2ugx_off)

        # file_info.ArtToolInfo pointer should be 0x94 in old generated files.
        art_info_off = 0x94
        if chunk_len >= art_info_off + 8:
            _struct.pack_into("<Q", data, g0 + 0, art_info_off)

        # art_tool_info.ArtToolName must point to the actual Blender string.
        blender_off = find_in_granny(b"Blender 4.0.0")
        if blender_off >= 0 and chunk_len >= art_info_off + 8:
            _struct.pack_into("<Q", data, g0 + art_info_off, blender_off)

        # Keep the old known-good metadata constants on the art_tool_info block.
        # These are visible in the old add-on's working UAX exports.
        if chunk_len >= art_info_off + 0x4C:
            _struct.pack_into("<I", data, g0 + art_info_off + 8, 1)
            _struct.pack_into("<I", data, g0 + art_info_off + 16, 32)
            _struct.pack_into("<f", data, g0 + art_info_off + 20, 1.0)
            _struct.pack_into("<f", data, g0 + art_info_off + 36, 1.0)
            _struct.pack_into("<f", data, g0 + art_info_off + 52, 1.0)
            _struct.pack_into("<f", data, g0 + art_info_off + 68, -1.0)

        # The full DaK32fC32f type block should point at its relocated type name.
        # This does not create the block; it only fixes the name pointer if the
        # block exists at the old-compatible offset 0x1D4 / 468.
        curve_name_off = find_in_granny(b"CurveDataHeader_DaK32fC32f\x00")
        if curve_name_off >= 0 and chunk_len >= 468 + 12:
            _struct.pack_into("<I", data, g0 + 468 + 0, 1)
            _struct.pack_into("<Q", data, g0 + 468 + 4, curve_name_off)

        # Recompute total file size and chunk size from the actual buffer.
        _struct.pack_into(">I", data, 12, len(data))
        _struct.pack_into(">I", data, 0x2C, chunk_len)
    except Exception:
        return bytes(raw or b"")
    return bytes(data)


# Wrap the latest scratch writer so every template-free export is repaired before
# it hits disk. This is intentionally late-bound so it overrides all previous
# v5.x experimental writer variants without touching import/UAX patch export.
_hwugx_previous_write_scratch_uax_from_action = _write_scratch_uax_from_action

def _write_scratch_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    granny, stats = _build_scratch_granny_chunk_from_action(action, armature, settings, operator=operator)
    if not granny:
        if operator:
            operator.report({"ERROR"}, f"Could not build scratch UAX for {action.name}: {stats.get('error', 'unknown')}")
        return False
    raw = _build_ecf_uax_from_granny_chunk(granny)
    raw = _hwugx_repair_template_free_uax_raw(raw)
    os.makedirs(os.path.dirname(uax_path), exist_ok=True)
    with open(uax_path, "wb") as f:
        f.write(raw)
    try:
        report_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
        # Re-read repaired metadata for the report.
        cid, chunk_off, chunk_len = struct.unpack_from(">QII", raw, 0x20)
        chunk = raw[chunk_off:chunk_off + chunk_len]
        art_ptr = struct.unpack_from("<Q", chunk, 0)[0] if len(chunk) >= 8 else None
        art_name_ptr = struct.unpack_from("<Q", chunk, 0x94)[0] if len(chunk) >= 0x9C else None
        exporter_ptr = struct.unpack_from("<Q", chunk, 16)[0] if len(chunk) >= 24 else None
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({
                "version": "5.8.0",
                "action": action.name,
                "armature": armature.name if armature else "",
                "output": uax_path,
                "tracks": stats.get("tracks", 0),
                "duration": stats.get("duration", 0.0),
                "rot_keys": stats.get("rot_keys", 0),
                "pos_keys": stats.get("pos_keys", 0),
                "scale_keys": stats.get("scale_keys", 0),
                "exported_bones": stats.get("exported_bones", []),
                "animated_bones_detected": sorted(_hwugx_action_significantly_animated_bones(action)) if '_hwugx_action_significantly_animated_bones' in globals() else [],
                "keyed_bones": sorted(_action_keyed_bone_names(action)),
                "repaired_metadata": True,
                "chunk_id": hex(cid),
                "chunk_len": chunk_len,
                "file_info_art_tool_info_ptr": art_ptr,
                "art_tool_info_name_ptr": art_name_ptr,
                "exporter_name_ptr": exporter_ptr,
                "art_tool_string_offset_found": chunk.find(b"Blender 4.0.0"),
                "gr2ugx_offset_found": chunk.find(b"gr2ugx\x00"),
                "note": "v5.8 fixes the root-only GrannyRootBone bake path so root rotation/location authored in Blender can be baked into scale-shear, matching old working structure UAX behavior more closely.",
            }, f, indent=2)
    except Exception:
        pass
    if operator:
        operator.report({"INFO"}, f"Exported template-free UAX: {os.path.basename(uax_path)} ({len(raw):,} bytes, tracks {stats.get('tracks', 0)}, v5.7 metadata repaired)")
    return True


# -----------------------------------------------------------------------------
# v5.9.0 FINAL root-only structure export override
# -----------------------------------------------------------------------------
# Earlier v5.6/v5.8 notes were correct conceptually, but the later v5.4 native-basis
# compatibility block redefined _scratch_pose_quat/_scratch_pose_pos after the
# bake helpers.  That meant root-only GrannyRootBone clips still exported animated
# rotation/location curves, which HW2 appears to load but ignore for structure
# placement clips.  These definitions are intentionally LAST so the final writer
# actually exports root-only structure clips like the known-good old add-on files:
#   * rotation curve = stable native/rest GrannyRootBone quaternion
#   * position curve = 0,0,0
#   * scale-shear curve = authored visual root transform baked as a 3x3 matrix
# Normal multi-bone unit animations still use the native UGX basis path.

def _hwugx_v590_action_has_curve(action, bone_name: str, prop: str) -> bool:
    try:
        path = f'pose.bones["{bone_name}"].{prop}'
        for fc in iter_action_fcurves(action):
            if getattr(fc, "data_path", "") == path:
                return True
    except Exception:
        pass
    return False


def _hwugx_v590_is_root_only_structure_action(action, armature, bone_name: str) -> bool:
    try:
        if not action or not armature or armature.type != "ARMATURE":
            return False
        if not str(bone_name or "").startswith("GrannyRootBone"):
            return False
        keyed = {k for k in _action_keyed_bone_names(action) if k in armature.data.bones}
        # Root-only clips may have a single keyed GrannyRootBone. If anything else
        # is keyed/animated, keep the regular multi-bone exporter.
        if keyed and keyed - {bone_name}:
            return False
        return (
            _hwugx_v590_action_has_curve(action, bone_name, "rotation_quaternion")
            or _hwugx_v590_action_has_curve(action, bone_name, "rotation_euler")
            or _hwugx_v590_action_has_curve(action, bone_name, "location")
            or _hwugx_v590_action_has_curve(action, bone_name, "scale")
        )
    except Exception:
        return False


def _hwugx_v590_native_rest_quat(armature, bone_name: str) -> Quaternion:
    try:
        native_mat = _native_matrix_for_bone(armature, bone_name)
        if native_mat is not None:
            q = native_mat.to_quaternion()
        else:
            q = armature.data.bones[bone_name].matrix.to_quaternion()
        q.normalize()
        return q
    except Exception:
        return Quaternion((1.0, 0.0, 0.0, 0.0))


def _scratch_pose_quat(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, axis_q: Quaternion) -> Quaternion:
    """FINAL v5.9 native Granny rotation sample.

    For root-only structure clips, keep the native/root rest rotation stable.
    For normal clips, preserve the old importer inverse-basis rule:
        native_quat = native_rest_matrix_quat @ blender_pose_quat
    """
    if _hwugx_v590_is_root_only_structure_action(action, armature, bone_name):
        q_uax = _hwugx_v590_native_rest_quat(armature, bone_name)
    else:
        q_blender = _action_quaternion_at(action, bone_name, frame)
        if q_blender is None:
            q_blender = Quaternion((1.0, 0.0, 0.0, 0.0))
        native_mat = _native_matrix_for_bone(armature, bone_name)
        if native_mat is not None:
            q_uax = native_mat.to_quaternion() @ q_blender
        else:
            try:
                q_uax = armature.data.bones[bone_name].matrix.to_quaternion() @ q_blender
            except Exception:
                q_uax = q_blender
    try:
        if axis_q is not None and abs(axis_q.angle) >= 0.000001:
            q_uax = axis_q.inverted() @ q_uax @ axis_q
    except Exception:
        pass
    try:
        q_uax.normalize()
    except Exception:
        q_uax = Quaternion((1.0, 0.0, 0.0, 0.0))
    return q_uax


def _scratch_pose_pos(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, settings, axis_q: Quaternion, axis_m: Matrix) -> Vector:
    """FINAL v5.9 native Granny position sample.

    Root-only structure clips keep root position zero, matching the old working
    lekgolo custom UAX behavior. Normal clips still use native rest head + action
    location delta.
    """
    if _hwugx_v590_is_root_only_structure_action(action, armature, bone_name):
        return Vector((0.0, 0.0, 0.0))

    loc = _action_location_at(action, bone_name, frame)
    if loc is None:
        loc = Vector((0.0, 0.0, 0.0))
    try:
        import_scale = float(armature.get("hwugx_import_scale", 1.0)) if armature else 1.0
    except Exception:
        import_scale = 1.0
    native_delta = Vector(loc) / import_scale if abs(import_scale) > 0.000001 else Vector(loc)
    final = _native_head_for_bone(armature, bone_name) + native_delta
    if _native_parent_name_for_bone(armature, bone_name):
        final.z = -final.z
    try:
        if axis_q is not None and abs(axis_q.angle) >= 0.000001:
            final = axis_q.inverted() @ final
    except Exception:
        pass
    try:
        if not _scratch_export_axes_are_default(settings):
            final = axis_m @ final
    except Exception:
        pass
    return final


def _hwugx_v590_matrix3_to_tuple(m) -> tuple[float, float, float, float, float, float, float, float, float]:
    return (
        float(m[0][0]), float(m[0][1]), float(m[0][2]),
        float(m[1][0]), float(m[1][1]), float(m[1][2]),
        float(m[2][0]), float(m[2][1]), float(m[2][2]),
    )


def _scratch_pose_scale_shear(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float) -> tuple[float, float, float, float, float, float, float, float, float]:
    """FINAL v5.9 native Granny 3x3 scale-shear sample.

    For root-only structure clips, bake the authored Blender root transform into
    scale-shear so HW2 sees a visual deformation channel rather than discarded
    entity/root motion.
    """
    native_mul = _scratch_native_scale_multiplier(armature)
    scl = _action_scale_at(action, bone_name, frame)
    if scl is None:
        sx = sy = sz = native_mul
    else:
        sx = float(scl.x) * native_mul
        sy = float(scl.y) * native_mul
        sz = float(scl.z) * native_mul

    if _hwugx_v590_is_root_only_structure_action(action, armature, bone_name):
        # Combine authored rotation with authored scale. Location cannot be
        # represented in scale-shear, so if the Blender clip is pure translation,
        # it must be authored as rotation/scale or use a non-root visible bone.
        q = _action_quaternion_at(action, bone_name, frame)
        if q is None:
            q = Quaternion((1.0, 0.0, 0.0, 0.0))
        try:
            q.normalize()
            m = q.to_matrix().to_3x3()
            # Apply diagonal scale to columns: M * diag(sx, sy, sz)
            m[0][0] *= sx; m[1][0] *= sx; m[2][0] *= sx
            m[0][1] *= sy; m[1][1] *= sy; m[2][1] *= sy
            m[0][2] *= sz; m[1][2] *= sz; m[2][2] *= sz
            return _hwugx_v590_matrix3_to_tuple(m)
        except Exception:
            pass

    return (sx, 0.0, 0.0, 0.0, sy, 0.0, 0.0, 0.0, sz)


# Wrap the writer one final time only to update report wording/version and expose
# whether the final root-only bake path actually applies to the exported action.
_hwugx_v590_previous_write_scratch_uax_from_action = _write_scratch_uax_from_action

def _write_scratch_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    result = _hwugx_v590_previous_write_scratch_uax_from_action(action, uax_path, armature, settings, operator)
    try:
        report_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
        root_name = _scratch_root_bone_name_for_group(armature, _scratch_export_bone_names(action, armature))
        root_bake_active = _hwugx_v590_is_root_only_structure_action(action, armature, root_name)
        if os.path.isfile(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        data.update({
            "version": "5.9.0",
            "final_root_only_structure_bake_active": bool(root_bake_active),
            "final_root_bake_bone": root_name,
            "final_root_export_rule": "root rotation/location held stable; authored root rotation/scale baked into scale-shear",
            "final_warning": "If the Blender action is pure root translation, scale-shear cannot represent that translation. Add/key a visible child bone or convert the motion to rotation/scale for root-only structure clips.",
        })
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
    return result


# -----------------------------------------------------------------------------
# v6.0.0 FINAL legacy-compatible root translation export
# -----------------------------------------------------------------------------
# The v5.6-v5.9 root-only structure bake was based on a wrong assumption: it
# zeroed GrannyRootBone location and tried to bake visible root motion into the
# scale-shear matrix. The older HW Suite importer proves the opposite path is the
# reliable round-trip rule:
#   import rotation: blender_quat = bone_rest^-1 @ native_quat
#   export rotation: native_quat  = bone_rest    @ blender_quat
#   import location: blender_loc  = native_pos - bone.head, with child Z flip only
#   export location: native_pos   = bone.head + blender_loc, with child Z flip only
# Therefore v6 intentionally lets GrannyRootBone translation/rotation write as
# real UAX curves again. Scale-shear is kept as scale only, not a fake motion
# channel. This is placed last so it overrides every older compatibility block.

def _hwugx_v600_native_rest_quat(armature, bone_name: str) -> Quaternion:
    try:
        native_mat = _native_matrix_for_bone(armature, bone_name)
        if native_mat is not None:
            q = native_mat.to_quaternion()
        else:
            q = armature.data.bones[bone_name].matrix.to_quaternion()
        q.normalize()
        return q
    except Exception:
        return Quaternion((1.0, 0.0, 0.0, 0.0))


def _scratch_pose_quat(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, axis_q: Quaternion) -> Quaternion:
    """v6 final native Granny rotation sample.

    No root-only bake. Root motion is exported as real native UAX rotation and
    location, matching the old importer/exporter inverse basis behavior.
    """
    q_blender = _action_quaternion_at(action, bone_name, frame)
    if q_blender is None:
        q_blender = Quaternion((1.0, 0.0, 0.0, 0.0))
    try:
        q_blender.normalize()
    except Exception:
        q_blender = Quaternion((1.0, 0.0, 0.0, 0.0))

    try:
        q_uax = _hwugx_v600_native_rest_quat(armature, bone_name) @ q_blender
    except Exception:
        q_uax = q_blender

    try:
        if axis_q is not None and abs(axis_q.angle) >= 0.000001:
            q_uax = axis_q.inverted() @ q_uax @ axis_q
    except Exception:
        pass
    try:
        q_uax.normalize()
    except Exception:
        q_uax = Quaternion((1.0, 0.0, 0.0, 0.0))
    return q_uax


def _scratch_pose_pos(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, settings, axis_q: Quaternion, axis_m: Matrix) -> Vector:
    """v6 final native Granny position sample.

    This is the inverse of the old Blender 4 importer position rule. It keeps
    GrannyRootBone pure translation as an actual position curve, instead of
    forcing it to zero.
    """
    loc = _action_location_at(action, bone_name, frame)
    if loc is None:
        loc = Vector((0.0, 0.0, 0.0))
    else:
        loc = Vector(loc)

    try:
        import_scale = float(armature.get("hwugx_import_scale", 1.0)) if armature else 1.0
    except Exception:
        import_scale = 1.0
    native_delta = loc / import_scale if abs(import_scale) > 0.000001 else loc

    try:
        final = _native_head_for_bone(armature, bone_name) + native_delta
    except Exception:
        try:
            final = armature.data.bones[bone_name].head_local + native_delta
        except Exception:
            final = native_delta

    # Old importer did: if bone.parent: final.z = -final.z; vec = final - head.
    # Inverse for export: child tracks get the same Z flip; root tracks do not.
    try:
        if _native_parent_name_for_bone(armature, bone_name):
            final.z = -final.z
    except Exception:
        try:
            if armature.data.bones[bone_name].parent:
                final.z = -final.z
        except Exception:
            pass

    try:
        if axis_q is not None and abs(axis_q.angle) >= 0.000001:
            final = axis_q.inverted() @ final
    except Exception:
        pass
    try:
        if not _scratch_export_axes_are_default(settings):
            final = axis_m @ final
    except Exception:
        pass
    return Vector(final)


def _scratch_pose_scale_shear(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float) -> tuple[float, float, float, float, float, float, float, float, float]:
    """v6 final scale-shear sample.

    Scale-shear is only scale/shear, not a hidden replacement for root motion.
    This keeps pure GrannyRootBone translation in the position curve where the
    legacy pipeline put it.
    """
    native_mul = _scratch_native_scale_multiplier(armature)
    scl = _action_scale_at(action, bone_name, frame)
    if scl is None:
        sx = sy = sz = native_mul
    else:
        sx = float(scl.x) * native_mul
        sy = float(scl.y) * native_mul
        sz = float(scl.z) * native_mul
    return (sx, 0.0, 0.0, 0.0, sy, 0.0, 0.0, 0.0, sz)


_hwugx_v600_previous_write_scratch_uax_from_action = _write_scratch_uax_from_action

def _write_scratch_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    result = _hwugx_v600_previous_write_scratch_uax_from_action(action, uax_path, armature, settings, operator)
    try:
        report_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
        root_name = _scratch_root_bone_name_for_group(armature, _scratch_export_bone_names(action, armature))
        if os.path.isfile(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        data.update({
            "version": "6.0.0",
            "v600_legacy_root_translation_export": True,
            "v600_root_bone": root_name,
            "final_root_only_structure_bake_active": False,
            "final_root_export_rule": "legacy inverse math: GrannyRootBone rotation/location are written as real native UAX curves; scale-shear is scale only",
            "final_warning": "Root translation is no longer zeroed or baked into scale-shear. If the in-game clip is still static, compare the position curve values and the model XML animation binding/name next.",
        })
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
    return result


# -----------------------------------------------------------------------------
# v6.1.0 FINAL unkeyed rotation = native UGX/Granny rest rotation
# -----------------------------------------------------------------------------
# The working Blender 4 scale-only UAX proved that HW2 expects the transform
# track rotation curve to contain the native/rest Granny quaternion even when the
# Blender Action has no authored rotation keys. Writing identity (0,0,0,1) makes
# scale-only clips valid-but-ignored for assets such as GrannyRootBone_lekgolowall.
# This block is intentionally LAST so it overrides the v6.0 fallback behavior.

_HWUGX_V610_REST_ROT_USED = {}
_HWUGX_V610_REST_ROT_MISSING_NATIVE = set()


def _hwugx_v610_rotation_paths_for_bone(bone_name: str) -> tuple[str, str, str]:
    return (
        f'pose.bones["{bone_name}"].rotation_quaternion',
        f'pose.bones["{bone_name}"].rotation_euler',
        f'pose.bones["{bone_name}"].rotation_axis_angle',
    )


def _hwugx_v610_action_has_rotation_keys(action: bpy.types.Action, bone_name: str) -> bool:
    """True only when the Blender Action actually contains authored rotation curves.

    The scratch UAX writer always emits a native rotation curve for every exported
    track, so output rot_keys in the debug file does not tell us whether the user
    authored rotation. This function checks the source Blender Action instead.
    """
    if not action or not bone_name:
        return False
    paths = set(_hwugx_v610_rotation_paths_for_bone(bone_name))
    try:
        for fc in iter_action_fcurves(action):
            if getattr(fc, "data_path", "") in paths and len(getattr(fc, "keyframe_points", [])) > 0:
                return True
    except Exception:
        pass
    return False


def _hwugx_v610_eval_action_rotation_any(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float) -> Quaternion | None:
    """Evaluate authored Blender pose rotation, supporting quaternion and Euler curves.

    Older helper code only looked at rotation_quaternion. This keeps existing
    quaternion behavior, but avoids treating Euler-authored actions as unkeyed.
    """
    if not _hwugx_v610_action_has_rotation_keys(action, bone_name):
        return None

    q = _action_quaternion_at(action, bone_name, frame)
    if q is not None:
        try:
            q.normalize()
        except Exception:
            q = Quaternion((1.0, 0.0, 0.0, 0.0))
        return q

    # Euler fallback for actions authored in Euler mode.
    euler_path = f'pose.bones["{bone_name}"].rotation_euler'
    has_euler = any(getattr(fc, "data_path", "") == euler_path for fc in iter_action_fcurves(action))
    if has_euler:
        vals = []
        for i in range(3):
            vals.append(_fcurve_value(action, bone_name, "rotation_euler", i, frame, 0.0))
        order = "XYZ"
        try:
            pb = armature.pose.bones.get(bone_name) if armature and armature.pose else None
            if pb and pb.rotation_mode in {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}:
                order = pb.rotation_mode
        except Exception:
            pass
        try:
            q = mathutils.Euler(tuple(float(v) for v in vals), order).to_quaternion()
            q.normalize()
            return q
        except Exception:
            return Quaternion((1.0, 0.0, 0.0, 0.0))

    # Axis-angle fallback.
    aa_path = f'pose.bones["{bone_name}"].rotation_axis_angle'
    has_aa = any(getattr(fc, "data_path", "") == aa_path for fc in iter_action_fcurves(action))
    if has_aa:
        vals = [
            _fcurve_value(action, bone_name, "rotation_axis_angle", 0, frame, 0.0),
            _fcurve_value(action, bone_name, "rotation_axis_angle", 1, frame, 0.0),
            _fcurve_value(action, bone_name, "rotation_axis_angle", 2, frame, 1.0),
            _fcurve_value(action, bone_name, "rotation_axis_angle", 3, frame, 0.0),
        ]
        try:
            axis = Vector((float(vals[1]), float(vals[2]), float(vals[3])))
            if axis.length < 0.000001:
                return Quaternion((1.0, 0.0, 0.0, 0.0))
            q = Quaternion(axis.normalized(), float(vals[0]))
            q.normalize()
            return q
        except Exception:
            return Quaternion((1.0, 0.0, 0.0, 0.0))

    return None


def _hwugx_v610_native_rest_quat_required(armature: bpy.types.Object, bone_name: str) -> Quaternion:
    """Return native UGX/Granny rest quaternion or raise if unavailable.

    For template-free HW2 UAX export, identity fallback is now known-bad for
    scale-only clips. If native UGX skeleton metadata is missing, fail loudly.
    """
    native_mat = _native_matrix_for_bone(armature, bone_name)
    if native_mat is None:
        _HWUGX_V610_REST_ROT_MISSING_NATIVE.add(str(bone_name))
        raise RuntimeError(
            f"Native UGX/Granny rest basis is missing for bone '{bone_name}'. "
            "Template-free UAX export cannot safely use identity rotation fallback; "
            "re-import the UGX with v5.4+ / v6.1+ so the native skeleton cache is attached."
        )
    q = native_mat.to_quaternion()
    try:
        q.normalize()
    except Exception:
        pass
    return q


def _scratch_pose_quat(action: bpy.types.Action, armature: bpy.types.Object, bone_name: str, frame: float, axis_q: Quaternion) -> Quaternion:
    """v6.1 native Granny rotation sample.

    Authored rotation: native = native_rest * authored_blender_pose_rotation.
    No authored rotation: native = native_rest, NOT identity.
    """
    authored_q = _hwugx_v610_eval_action_rotation_any(action, armature, bone_name, frame)
    has_authored_rotation = authored_q is not None

    native_rest = _hwugx_v610_native_rest_quat_required(armature, bone_name)

    if has_authored_rotation:
        try:
            q_uax = native_rest @ authored_q
        except Exception:
            q_uax = native_rest.copy()
    else:
        q_uax = native_rest.copy()
        try:
            _HWUGX_V610_REST_ROT_USED[str(bone_name)] = {
                "wxyz": [float(q_uax.w), float(q_uax.x), float(q_uax.y), float(q_uax.z)],
                "xyzw": [float(q_uax.x), float(q_uax.y), float(q_uax.z), float(q_uax.w)],
            }
        except Exception:
            pass

    try:
        if axis_q is not None and abs(axis_q.angle) >= 0.000001:
            q_uax = axis_q.inverted() @ q_uax @ axis_q
    except Exception:
        pass
    try:
        q_uax.normalize()
    except Exception:
        q_uax = native_rest.copy()
    if not has_authored_rotation:
        # Record the final value that is actually written into the UAX curve.
        # With default axes this is the native rest quaternion verbatim; with
        # custom Back/Right/Up it reflects the exported basis.
        try:
            _HWUGX_V610_REST_ROT_USED[str(bone_name)] = {
                "wxyz": [float(q_uax.w), float(q_uax.x), float(q_uax.y), float(q_uax.z)],
                "xyzw": [float(q_uax.x), float(q_uax.y), float(q_uax.z), float(q_uax.w)],
            }
        except Exception:
            pass
    return q_uax


_hwugx_v610_previous_write_scratch_uax_from_action = _write_scratch_uax_from_action


def _write_scratch_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    """v6.1 wrapper: refuse identity fallback and annotate debug report."""
    try:
        _HWUGX_V610_REST_ROT_USED.clear()
        _HWUGX_V610_REST_ROT_MISSING_NATIVE.clear()
    except Exception:
        pass

    # Preflight: if any exported bone has no authored rotation keys, native UGX
    # rest basis must be present. This is what prevents silent identity fallback.
    try:
        bone_names = _scratch_export_bone_names(action, armature)
    except Exception:
        bone_names = []
    unkeyed_rot_bones = []
    missing_native = []
    for bn in bone_names:
        try:
            if not _hwugx_v610_action_has_rotation_keys(action, bn):
                unkeyed_rot_bones.append(bn)
                if _native_matrix_for_bone(armature, bn) is None:
                    missing_native.append(bn)
        except Exception:
            pass
    if missing_native:
        msg = (
            "Template-free UAX export refused: unkeyed rotation tracks require "
            "native UGX/Granny rest quaternions. Missing native rest basis for: "
            + ", ".join(missing_native[:12])
            + ("..." if len(missing_native) > 12 else "")
        )
        if operator:
            operator.report({"ERROR"}, msg)
        try:
            report_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
            data = {
                "version": "6.1.0",
                "action": getattr(action, "name", ""),
                "armature": getattr(armature, "name", ""),
                "output": uax_path,
                "export_refused": True,
                "reason": "missing_native_rest_rotation_for_unkeyed_bones",
                "used_native_rest_rotation_for_unkeyed_bones": False,
                "unkeyed_rotation_bones": unkeyed_rot_bones,
                "missing_native_rest_rotation_bones": missing_native,
                "hard_warning": "Identity rotation fallback is invalid for HW2 template-free UAX export. Re-import the UGX so native skeleton basis is cached.",
            }
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        return False

    result = _hwugx_v610_previous_write_scratch_uax_from_action(action, uax_path, armature, settings, operator)

    try:
        report_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
        if os.path.isfile(report_path):
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}

        root_name = ""
        try:
            root_name = _scratch_root_bone_name_for_group(armature, bone_names)
        except Exception:
            root_name = bone_names[0] if bone_names else ""

        root_quat = None
        if root_name:
            try:
                q = _hwugx_v610_native_rest_quat_required(armature, root_name)
                root_quat = {
                    "wxyz": [float(q.w), float(q.x), float(q.y), float(q.z)],
                    "xyzw": [float(q.x), float(q.y), float(q.z), float(q.w)],
                }
            except Exception as exc:
                root_quat = {"error": str(exc)}

        data.update({
            "version": "6.1.0",
            "used_native_rest_rotation_for_unkeyed_bones": bool(unkeyed_rot_bones) and not bool(missing_native),
            "unkeyed_rotation_bones": unkeyed_rot_bones,
            "native_rest_rotation_quaternions_written": dict(_HWUGX_V610_REST_ROT_USED),
            "granny_root_bone": root_name,
            "granny_root_native_rest_quaternion_written": root_quat,
            "v610_rotation_export_rule": "missing authored bone rotation writes native UGX/Granny rest quaternion; identity fallback is refused",
            "v610_expected_lekgolo_root_xyzw": "For the lekgolo wall scale-only test this should match approximately [-0.0638989, -0.00153917, 0.00216844, 0.99795294] when the correct native UGX cache is present.",
        })
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
    return result

# -----------------------------------------------------------------------------
# v6.3.0 legacy HW Suite UAX export path
# v6.4.0 treats gr2ugx nonzero warning codes as success when a valid UAX is produced; keeps manual Collada fallback for Blender builds without bpy.ops.wm.collada_export
# -----------------------------------------------------------------------------
# The old working exporter did not write UAX directly in Python. It sampled the
# selected armature/action to Collada DAE, converted DAE -> GR2 with DAEtoGR2.exe,
# then converted GR2 -> UAX with gr2ugx.exe -auto -anim. This restores that path
# for template-free custom UAX export when a bundled ./tool folder is present.

try:
    HWUGX_AddonPreferences.__annotations__["legacy_tool_dir"] = StringProperty(
        name="Legacy HW Suite Tool Folder",
        description="Folder containing DAEtoGR2.exe and gr2ugx.exe. Leave blank to auto-detect a bundled ./tool folder beside the add-on.",
        subtype="DIR_PATH",
        default="",
    )
    HWUGX_AddonPreferences.__annotations__["prefer_legacy_uax_export"] = BoolProperty(
        name="Prefer Legacy UAX Export",
        description="For custom/template-free UAX actions, use the old sampled DAE -> GR2 -> UAX toolchain before the native scratch writer.",
        default=True,
    )
except Exception:
    pass

_hwugx_v620_previous_prefs_draw = getattr(HWUGX_AddonPreferences, "draw", None)


def _hwugx_v620_prefs_draw(self, context):
    layout = self.layout
    if _hwugx_v620_previous_prefs_draw:
        try:
            _hwugx_v620_previous_prefs_draw(self, context)
        except Exception:
            pass
    box = layout.box()
    box.label(text="Legacy UAX Export", icon="ACTION")
    try:
        box.prop(self, "prefer_legacy_uax_export")
        box.prop(self, "legacy_tool_dir")
    except Exception:
        pass
    box.label(text="Auto-detects: add-on folder/tool/DAEtoGR2.exe and gr2ugx.exe", icon="INFO")


try:
    HWUGX_AddonPreferences.draw = _hwugx_v620_prefs_draw
except Exception:
    pass


def _hwugx_v620_addon_dir() -> str:
    try:
        return os.path.dirname(os.path.realpath(__file__))
    except Exception:
        return os.getcwd()


def _hwugx_v620_legacy_tool_candidates(context=None) -> list[str]:
    candidates = []
    try:
        prefs = get_prefs(context)
        pref_dir = bpy.path.abspath(getattr(prefs, "legacy_tool_dir", "") or "") if prefs else ""
        if pref_dir:
            candidates.append(pref_dir)
    except Exception:
        pass
    base = _hwugx_v620_addon_dir()
    candidates.extend([
        os.path.join(base, "tool"),
        os.path.join(base, "tools"),
        base,
        os.path.join(os.path.dirname(base), "tool"),
    ])
    out = []
    seen = set()
    for c in candidates:
        try:
            c = os.path.abspath(c)
            if c not in seen:
                seen.add(c)
                out.append(c)
        except Exception:
            pass
    return out


def _hwugx_v620_find_legacy_tools(context=None) -> tuple[str, str, str]:
    """Return (tool_dir, DAEtoGR2.exe, gr2ugx.exe), or empty strings."""
    for folder in _hwugx_v620_legacy_tool_candidates(context):
        dae = os.path.join(folder, "DAEtoGR2.exe")
        gr2 = os.path.join(folder, "gr2ugx.exe")
        if os.path.isfile(dae) and os.path.isfile(gr2):
            return folder, dae, gr2
    return "", "", ""


def _hwugx_v620_prefer_legacy(context=None) -> bool:
    try:
        prefs = get_prefs(context)
        return bool(getattr(prefs, "prefer_legacy_uax_export", True)) if prefs else True
    except Exception:
        return True


def _hwugx_v620_write_json(path: str, data: dict):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _hwugx_v620_run_external(cmd: list[str], cwd: str, operator=None) -> tuple[bool, dict]:
    info = {
        "cmd": cmd,
        "cwd": cwd,
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd or None,
            shell=False,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        info["returncode"] = int(result.returncode)
        info["stdout"] = (result.stdout or "")[-4000:]
        info["stderr"] = (result.stderr or "")[-4000:]
        ok = (result.returncode == 0)
        if operator:
            msg = (result.stdout or result.stderr or "").strip()
            if msg:
                operator.report({"INFO" if ok else "WARNING"}, msg[:900])
        return ok, info
    except Exception as exc:
        info["stderr"] = str(exc)
        if operator:
            operator.report({"ERROR"}, f"Legacy tool failed to run: {exc}")
        return False, info


def _hwugx_v620_ensure_collada_exporter(operator=None) -> bool:
    try:
        getattr(bpy.ops.wm, "collada_export")
        return True
    except Exception:
        pass
    # Some Blender builds expose Collada through an add-on module. Try enabling it.
    try:
        bpy.ops.preferences.addon_enable(module="io_scene_dae")
        getattr(bpy.ops.wm, "collada_export")
        return True
    except Exception as exc:
        if operator:
            operator.report({"ERROR"}, "Blender Collada exporter is unavailable. Enable/install the Collada exporter, or use the native scratch path.")
        return False


def _hwugx_v620_collada_export_sampled_action(context, armature: bpy.types.Object, action: bpy.types.Action, dae_path: str, operator=None) -> tuple[bool, dict]:
    """Match the old Blender 2.91/4 HW Suite export settings as closely as Blender 5 allows."""
    report = {
        "dae_path": dae_path,
        "collada_export_attempts": [],
        "frame_range": [float(action.frame_range[0]), float(action.frame_range[1])],
        "selected_armature": armature.name if armature else "",
        "action": action.name if action else "",
    }
    if not _hwugx_v620_ensure_collada_exporter(operator):
        report["error"] = "collada_export_operator_unavailable"
        return False, report

    old_active = context.view_layer.objects.active
    old_selected = list(context.selected_objects)
    old_action = armature.animation_data.action if armature.animation_data else None
    old_start = int(context.scene.frame_start)
    old_end = int(context.scene.frame_end)
    old_frame = int(context.scene.frame_current)
    try:
        if armature.animation_data is None:
            armature.animation_data_create()
        armature.animation_data.action = action
        frame0 = int(action.frame_range[0])
        frame1 = int(action.frame_range[1])
        context.scene.frame_start = frame0
        context.scene.frame_end = frame1
        try:
            context.scene.frame_set(frame0)
        except Exception:
            pass

        bpy.ops.object.select_all(action="DESELECT")
        armature.select_set(True)
        context.view_layer.objects.active = armature

        common = dict(
            filepath=dae_path,
            selected=True,
            apply_modifiers=True,
            include_animations=True,
            include_all_actions=False,
        )
        # Different Blender versions accept slightly different Collada keyword sets.
        # Try the old exact settings first, then strip only unsupported options.
        attempts = [
            dict(common, use_object_instantiation=False, use_blender_profile=False, deform_bones_only=True, export_animation_type_selection="sample", sampling_rate=1),
            dict(common, use_object_instantiation=False, use_blender_profile=False, deform_bones_only=True, sampling_rate=1),
            dict(common, use_object_instantiation=False, use_blender_profile=False, deform_bones_only=True),
            dict(common, deform_bones_only=True),
            dict(common),
        ]
        last_exc = None
        for kwargs in attempts:
            attempt_info = {"kwargs": {k: str(v) for k, v in kwargs.items() if k != "filepath"}}
            try:
                bpy.ops.wm.collada_export(**kwargs)
                exists = os.path.isfile(dae_path) and os.path.getsize(dae_path) > 0
                attempt_info["ok"] = bool(exists)
                attempt_info["size"] = os.path.getsize(dae_path) if os.path.isfile(dae_path) else 0
                report["collada_export_attempts"].append(attempt_info)
                if exists:
                    return True, report
            except Exception as exc:
                last_exc = exc
                attempt_info["ok"] = False
                attempt_info["error"] = str(exc)
                report["collada_export_attempts"].append(attempt_info)
        report["error"] = str(last_exc) if last_exc else "collada_export_did_not_write_file"
        return False, report
    finally:
        try:
            if armature.animation_data:
                armature.animation_data.action = old_action
            context.scene.frame_start = old_start
            context.scene.frame_end = old_end
            context.scene.frame_set(old_frame)
            bpy.ops.object.select_all(action="DESELECT")
            for obj in old_selected:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if old_active and old_active.name in bpy.data.objects:
                context.view_layer.objects.active = old_active
        except Exception:
            pass


def _write_legacy_dae_gr2_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    """Old HW Suite path: Blender sampled Collada -> DAEtoGR2.exe -> gr2ugx.exe -auto -anim."""
    debug_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
    tool_dir, dae_to_gr2, gr2ugx = _hwugx_v620_find_legacy_tools(bpy.context)
    debug = {
        "version": "6.4.0",
        "export_mode": "legacy_dae_gr2ugx_toolchain",
        "action": getattr(action, "name", ""),
        "armature": getattr(armature, "name", ""),
        "output": uax_path,
        "tool_dir": tool_dir,
        "DAEtoGR2.exe": dae_to_gr2,
        "gr2ugx.exe": gr2ugx,
        "axis_back": getattr(settings, "axis_back", "Z-"),
        "axis_right": getattr(settings, "axis_right", "X-"),
        "axis_up": getattr(settings, "axis_up", "Y+"),
        "old_plugin_equivalent": "selected armature only; sampled Collada animation; DAEtoGR2.exe -debug; gr2ugx.exe -auto -anim",
    }
    if not tool_dir or not dae_to_gr2 or not gr2ugx:
        debug["success"] = False
        debug["error"] = "legacy_tools_not_found"
        debug["searched_folders"] = _hwugx_v620_legacy_tool_candidates(bpy.context)
        _hwugx_v620_write_json(debug_path, debug)
        if operator:
            operator.report({"WARNING"}, "Legacy UAX tools not found. Put DAEtoGR2.exe and gr2ugx.exe in a bundled 'tool' folder beside the add-on.")
        return False

    temp_dir = tempfile.mkdtemp(prefix="hw_legacy_uax_")
    try:
        safe_name = _sanitize_filename(str(action.get("uax_source", "")) or action.name or "animation")
        dae_path = os.path.join(temp_dir, safe_name + "_tmp.dae")
        gr2_path = os.path.join(temp_dir, safe_name + "_tmp.gr2")
        os.makedirs(os.path.dirname(uax_path), exist_ok=True)

        ok_dae, dae_report = _hwugx_v620_collada_export_sampled_action(bpy.context, armature, action, dae_path, operator)
        debug["collada"] = dae_report
        if not ok_dae:
            debug["success"] = False
            debug["error"] = "collada_export_failed"
            _hwugx_v620_write_json(debug_path, debug)
            return False

        cmd1 = [dae_to_gr2, "-debug", dae_path, gr2_path]
        ok1, run1 = _hwugx_v620_run_external(cmd1, tool_dir, operator)
        debug["DAEtoGR2"] = run1
        debug["gr2_exists_after_DAEtoGR2"] = os.path.isfile(gr2_path)
        debug["gr2_size"] = os.path.getsize(gr2_path) if os.path.isfile(gr2_path) else 0
        if not ok1 or not os.path.isfile(gr2_path) or os.path.getsize(gr2_path) <= 0:
            debug["success"] = False
            debug["error"] = "DAEtoGR2_failed_or_no_gr2"
            _hwugx_v620_write_json(debug_path, debug)
            return False

        back = getattr(settings, "axis_back", "Z-") or "Z-"
        right = getattr(settings, "axis_right", "X-") or "X-"
        up = getattr(settings, "axis_up", "Y+") or "Y+"
        cmd2 = [gr2ugx, "-auto", "-anim", gr2_path, uax_path, back, right, up]
        ok2, run2 = _hwugx_v620_run_external(cmd2, tool_dir, operator)
        debug["gr2ugx"] = run2
        debug["uax_exists_after_gr2ugx"] = os.path.isfile(uax_path)
        debug["uax_size"] = os.path.getsize(uax_path) if os.path.isfile(uax_path) else 0
        if not ok2 or not os.path.isfile(uax_path) or os.path.getsize(uax_path) <= 0:
            debug["success"] = False
            debug["error"] = "gr2ugx_failed_or_no_uax"
            _hwugx_v620_write_json(debug_path, debug)
            return False

        debug["success"] = True
        debug["template_free"] = True
        debug["writer"] = "legacy_external_tools"
        debug["note"] = "This restores the old plugin's successful template-free UAX path instead of using the Python scratch UAX writer."
        _hwugx_v620_write_json(debug_path, debug)
        if operator:
            operator.report({"INFO"}, f"Exported legacy-template-free UAX: {os.path.basename(uax_path)} ({os.path.getsize(uax_path):,} bytes)")
        return True
    finally:
        clean_temp_dir(temp_dir, bpy.context)


_hwugx_v620_previous_export_uax_sidecars = _export_uax_sidecars_from_current_settings


def _export_uax_sidecars_from_current_settings(context, temp_dir: str, destination_dir: str, base_name: str, operator=None) -> int:
    """v6.2 override: use the old DAE->GR2->UAX toolchain for template-free custom actions."""
    settings = get_scene_export_settings(context)
    if not destination_dir:
        return 0
    dest = bpy.path.abspath(destination_dir)
    os.makedirs(dest, exist_ok=True)

    armature = _resolve_export_armature(context, settings)
    if not armature:
        if operator:
            operator.report({"ERROR"}, "Cannot export UAX animations: select or choose an armature.")
        return 0
    if not armature.animation_data:
        armature.animation_data_create()

    actions = _iter_candidate_actions_for_armature(
        armature,
        all_actions=(settings.export_all_actions if settings else True),
        specific_action_name=(getattr(settings, "uax_export_action", "") if settings and not settings.export_all_actions else ""),
    )
    if not actions:
        if operator:
            operator.report({"ERROR"}, "No actions found to export as UAX.")
        return 0

    old_action = armature.animation_data.action
    old_active = context.view_layer.objects.active
    old_selected = list(context.selected_objects)
    exported = 0
    try:
        for action in actions:
            action_base = _sanitize_filename(str(action.get("uax_source", "")) or action.name or base_name)
            uax_path = os.path.join(dest, action_base + ".uax")

            # Edited imported UAX cache remains the safest exact round-trip path.
            if _write_native_uax_from_action(
                action,
                uax_path,
                armature,
                operator,
                rewrite_clip_name=bool(getattr(settings, "uax_export_rewrite_names", True)),
            ):
                exported += 1
                continue

            # Optional UAX template fallback is still honored if supplied.
            template_raw, template_path = _read_template_uax_from_settings(settings)
            if template_raw:
                retime = str(getattr(settings, "uax_custom_timing_mode", "ACTION_RANGE")) == "ACTION_RANGE"
                if _write_native_uax_from_action(
                    action,
                    uax_path,
                    armature,
                    operator,
                    raw_override=template_raw,
                    template_path=template_path,
                    retime_to_action=retime,
                    rewrite_clip_name=bool(getattr(settings, "uax_export_rewrite_names", True)),
                ):
                    exported += 1
                    continue

            custom_mode = str(getattr(settings, "uax_custom_export_mode", "SCRATCH_NATIVE"))
            if custom_mode == "SCRATCH_NATIVE":
                tool_dir, dae_to_gr2, gr2ugx = _hwugx_v620_find_legacy_tools(context)
                if _hwugx_v620_prefer_legacy(context) and tool_dir and dae_to_gr2 and gr2ugx:
                    if _write_legacy_dae_gr2_uax_from_action(action, uax_path, armature, settings, operator):
                        exported += 1
                        continue
                    # If the old toolchain exists but fails, do not silently emit
                    # a scratch file that may load as a no-op. The debug report is
                    # the source of truth for the failed old-path export.
                    if operator:
                        operator.report({"ERROR"}, f"Legacy UAX toolchain failed for {action.name}; not falling back to scratch writer automatically.")
                    continue

                # No legacy tools were found: retain native scratch as a fallback,
                # but make the report explicit so this cannot be mistaken for the
                # old plugin's proven export path.
                if getattr(settings, "uax_write_debug_report", True):
                    reason = "legacy_tools_missing; falling back to native scratch writer"
                    _write_uax_export_debug_report(action, uax_path, armature, reason, settings, operator)
                if operator:
                    operator.report({"WARNING"}, "Legacy UAX tools not found; using native scratch writer fallback.")
                if _write_scratch_uax_from_action(action, uax_path, armature, settings, operator):
                    exported += 1
                    continue
                if operator:
                    operator.report({"ERROR"}, f"Skipped {action.name}: template-free UAX export failed.")
                continue

            reason = "No imported UAX cache, no optional native template, and custom UAX mode is Imported/Template Only."
            if getattr(settings, "uax_write_debug_report", True):
                _write_uax_export_debug_report(action, uax_path, armature, reason, settings, operator)
            if operator:
                operator.report({"ERROR"}, f"Skipped {action.name}: enable Template-Free Legacy/Native mode or provide a UAX template.")
            continue

        if operator and exported:
            operator.report({"INFO"}, f"Exported {exported} UAX animation file(s) to {dest}")
        return exported
    finally:
        try:
            armature.animation_data.action = old_action
            bpy.ops.object.select_all(action="DESELECT")
            for obj in old_selected:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if old_active and old_active.name in bpy.data.objects:
                context.view_layer.objects.active = old_active
        except Exception:
            pass

# -----------------------------------------------------------------------------
# v6.3.0 manual Collada fallback for Blender 5.x
# -----------------------------------------------------------------------------
# Blender 5 builds may not ship bpy.ops.wm.collada_export anymore. The old HWDE
# exporter depended on sampled DAE -> DAEtoGR2.exe -> gr2ugx.exe. To preserve that
# workflow, this fallback writes a minimal animation-only COLLADA 1.4.1 file with
# a joint hierarchy and sampled transform matrices for every armature bone.

import re as _hwugx_v630_re
import datetime as _hwugx_v630_datetime
from xml.sax.saxutils import escape as _hwugx_v630_xml_escape, quoteattr as _hwugx_v630_xml_quoteattr


def _hwugx_v630_safe_xml_id(name: str, used: set | None = None) -> str:
    raw = str(name or "node")
    sid = _hwugx_v630_re.sub(r"[^A-Za-z0-9_.-]", "_", raw)
    if not sid or not _hwugx_v630_re.match(r"^[A-Za-z_]", sid):
        sid = "n_" + sid
    if used is not None:
        base = sid
        i = 1
        while sid in used:
            i += 1
            sid = f"{base}_{i}"
        used.add(sid)
    return sid


def _hwugx_v630_floats(vals, precision: int = 9) -> str:
    out = []
    for v in vals:
        try:
            f = float(v)
        except Exception:
            f = 0.0
        # Keep deterministic, compact decimal output. DAEtoGR2 accepts normal floats.
        out.append((f"{f:.{precision}g}" if abs(f) >= 1e-8 else "0"))
    return " ".join(out)


def _hwugx_v630_matrix_values_row_major(mat) -> list[float]:
    """Return COLLADA matrix values in the same row-major order Blender-style DAE expects."""
    return [float(mat[r][c]) for r in range(4) for c in range(4)]


def _hwugx_v630_local_rest_matrix(bone):
    try:
        if bone.parent:
            return bone.parent.matrix_local.inverted() @ bone.matrix_local
        return bone.matrix_local.copy()
    except Exception:
        return Matrix.Identity(4)


def _hwugx_v630_pose_local_matrix(armature: bpy.types.Object, bone_name: str):
    try:
        pb = armature.pose.bones.get(bone_name)
        if not pb:
            return Matrix.Identity(4)
        if pb.parent:
            return pb.parent.matrix.inverted() @ pb.matrix
        return pb.matrix.copy()
    except Exception:
        return Matrix.Identity(4)


def _hwugx_v630_bone_hierarchy_order(armature: bpy.types.Object) -> list:
    """Return edit/rest bone objects in parent-before-child order."""
    bones = list(getattr(getattr(armature, "data", None), "bones", []) or [])
    remaining = {b.name: b for b in bones}
    ordered = []
    while remaining:
        progressed = False
        for name, b in list(remaining.items()):
            if (not b.parent) or (b.parent.name not in remaining):
                ordered.append(b)
                del remaining[name]
                progressed = True
        if not progressed:
            # Corrupt/cyclic should not happen, but avoid infinite loops.
            ordered.extend(remaining.values())
            break
    return ordered


def _hwugx_v630_write_joint_node(f, bone, children_by_parent, id_by_bone, indent="      "):
    bid = id_by_bone.get(bone.name, _hwugx_v630_safe_xml_id(bone.name))
    name_attr = _hwugx_v630_xml_quoteattr(bone.name)
    id_attr = _hwugx_v630_xml_quoteattr(bid)
    rest = _hwugx_v630_local_rest_matrix(bone)
    f.write(f'{indent}<node id={id_attr} name={name_attr} sid={name_attr} type="JOINT">\n')
    f.write(f'{indent}  <matrix sid="transform">{_hwugx_v630_floats(_hwugx_v630_matrix_values_row_major(rest))}</matrix>\n')
    for child in children_by_parent.get(bone.name, []):
        _hwugx_v630_write_joint_node(f, child, children_by_parent, id_by_bone, indent + "  ")
    f.write(f'{indent}</node>\n')


def _hwugx_v630_write_manual_collada_sampled_action(context, armature: bpy.types.Object, action: bpy.types.Action, dae_path: str, operator=None) -> tuple[bool, dict]:
    """Write a minimal sampled skeleton-animation COLLADA file for DAEtoGR2.exe.

    This is not a general-purpose DAE exporter. It intentionally mirrors the old
    UAX path's important animation behavior: selected armature only, sampled every
    frame, full transform matrices, and all armature bones in hierarchy order.
    """
    report = {
        "writer": "manual_collada_fallback_v640",
        "dae_path": dae_path,
        "action": getattr(action, "name", ""),
        "armature": getattr(armature, "name", ""),
        "reason": "bpy.ops.wm.collada_export unavailable in this Blender build",
    }
    try:
        bones = _hwugx_v630_bone_hierarchy_order(armature)
        if not bones:
            report["error"] = "armature_has_no_bones"
            return False, report

        # Frame/time setup matches the old plugin: frame_range is copied to the scene,
        # animation is sampled every integer frame, and time is seconds from first frame.
        fps = float(context.scene.render.fps) / float(context.scene.render.fps_base or 1.0)
        if fps <= 0:
            fps = 24.0
        frame0 = int(round(float(action.frame_range[0])))
        frame1 = int(round(float(action.frame_range[1])))
        if frame1 < frame0:
            frame1 = frame0
        frames = list(range(frame0, frame1 + 1))
        times = [(fr - frame0) / fps for fr in frames]

        used_ids = set()
        id_by_bone = {b.name: _hwugx_v630_safe_xml_id(b.name, used_ids) for b in bones}
        children_by_parent = {b.name: [] for b in bones}
        roots = []
        for b in bones:
            if b.parent and b.parent.name in children_by_parent:
                children_by_parent[b.parent.name].append(b)
            else:
                roots.append(b)

        old_action = armature.animation_data.action if armature.animation_data else None
        old_start = int(context.scene.frame_start)
        old_end = int(context.scene.frame_end)
        old_frame = int(context.scene.frame_current)
        sampled = {b.name: [] for b in bones}
        try:
            if armature.animation_data is None:
                armature.animation_data_create()
            armature.animation_data.action = action
            context.scene.frame_start = frame0
            context.scene.frame_end = frame1
            for fr in frames:
                context.scene.frame_set(fr)
                try:
                    context.view_layer.update()
                except Exception:
                    pass
                for b in bones:
                    sampled[b.name].append(_hwugx_v630_pose_local_matrix(armature, b.name).copy())
        finally:
            try:
                if armature.animation_data:
                    armature.animation_data.action = old_action
                context.scene.frame_start = old_start
                context.scene.frame_end = old_end
                context.scene.frame_set(old_frame)
            except Exception:
                pass

        os.makedirs(os.path.dirname(dae_path), exist_ok=True)
        now = _hwugx_v630_datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        scene_id = "Scene"
        arm_id = _hwugx_v630_safe_xml_id(getattr(armature, "name", "Armature") + "_node")
        action_id = _hwugx_v630_safe_xml_id(getattr(action, "name", "Action"))

        with open(dae_path, "w", encoding="utf-8", newline="\n") as f:
            f.write('<?xml version="1.0" encoding="utf-8"?>\n')
            f.write('<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">\n')
            f.write('  <asset>\n')
            f.write('    <contributor><authoring_tool>HWUGX Blender 5.1 Manual Legacy UAX DAE v6.4.0</authoring_tool></contributor>\n')
            f.write(f'    <created>{now}</created><modified>{now}</modified>\n')
            f.write('    <unit name="meter" meter="1"/>\n')
            f.write('    <up_axis>Z_UP</up_axis>\n')
            f.write('  </asset>\n')

            f.write('  <library_animations>\n')
            for b in bones:
                bid = id_by_bone[b.name]
                anim_id = _hwugx_v630_safe_xml_id(f"{action_id}_{bid}_transform")
                input_id = anim_id + "_input"
                input_arr_id = input_id + "_array"
                output_id = anim_id + "_output"
                output_arr_id = output_id + "_array"
                interp_id = anim_id + "_interpolation"
                interp_arr_id = interp_id + "_array"
                sampler_id = anim_id + "_sampler"
                matrices = []
                for m in sampled[b.name]:
                    matrices.extend(_hwugx_v630_matrix_values_row_major(m))
                f.write(f'    <animation id={_hwugx_v630_xml_quoteattr(anim_id)} name={_hwugx_v630_xml_quoteattr(b.name + "_transform")}>\n')
                f.write(f'      <source id={_hwugx_v630_xml_quoteattr(input_id)}>\n')
                f.write(f'        <float_array id={_hwugx_v630_xml_quoteattr(input_arr_id)} count="{len(times)}">{_hwugx_v630_floats(times)}</float_array>\n')
                f.write(f'        <technique_common><accessor source="#{_hwugx_v630_xml_escape(input_arr_id)}" count="{len(times)}" stride="1"><param name="TIME" type="float"/></accessor></technique_common>\n')
                f.write('      </source>\n')
                f.write(f'      <source id={_hwugx_v630_xml_quoteattr(output_id)}>\n')
                f.write(f'        <float_array id={_hwugx_v630_xml_quoteattr(output_arr_id)} count="{len(matrices)}">{_hwugx_v630_floats(matrices)}</float_array>\n')
                f.write(f'        <technique_common><accessor source="#{_hwugx_v630_xml_escape(output_arr_id)}" count="{len(times)}" stride="16"><param name="TRANSFORM" type="float4x4"/></accessor></technique_common>\n')
                f.write('      </source>\n')
                f.write(f'      <source id={_hwugx_v630_xml_quoteattr(interp_id)}>\n')
                f.write(f'        <Name_array id={_hwugx_v630_xml_quoteattr(interp_arr_id)} count="{len(times)}">{" ".join(["LINEAR"] * len(times))}</Name_array>\n')
                f.write(f'        <technique_common><accessor source="#{_hwugx_v630_xml_escape(interp_arr_id)}" count="{len(times)}" stride="1"><param name="INTERPOLATION" type="Name"/></accessor></technique_common>\n')
                f.write('      </source>\n')
                f.write(f'      <sampler id={_hwugx_v630_xml_quoteattr(sampler_id)}>\n')
                f.write(f'        <input semantic="INPUT" source="#{_hwugx_v630_xml_escape(input_id)}"/>\n')
                f.write(f'        <input semantic="OUTPUT" source="#{_hwugx_v630_xml_escape(output_id)}"/>\n')
                f.write(f'        <input semantic="INTERPOLATION" source="#{_hwugx_v630_xml_escape(interp_id)}"/>\n')
                f.write('      </sampler>\n')
                f.write(f'      <channel source="#{_hwugx_v630_xml_escape(sampler_id)}" target="{_hwugx_v630_xml_escape(bid)}/transform"/>\n')
                f.write('    </animation>\n')
            f.write('  </library_animations>\n')

            # Animation clips are not always required, but several older importers use them.
            f.write('  <library_animation_clips>\n')
            f.write(f'    <animation_clip id={_hwugx_v630_xml_quoteattr(action_id + "_clip")} name={_hwugx_v630_xml_quoteattr(action.name)} start="0" end="{times[-1] if times else 0}">\n')
            for b in bones:
                bid = id_by_bone[b.name]
                anim_id = _hwugx_v630_safe_xml_id(f"{action_id}_{bid}_transform")
                f.write(f'      <instance_animation url="#{_hwugx_v630_xml_escape(anim_id)}"/>\n')
            f.write('    </animation_clip>\n')
            f.write('  </library_animation_clips>\n')

            f.write('  <library_visual_scenes>\n')
            f.write(f'    <visual_scene id="{scene_id}" name="{scene_id}">\n')
            f.write(f'      <node id={_hwugx_v630_xml_quoteattr(arm_id)} name={_hwugx_v630_xml_quoteattr(armature.name)} type="NODE">\n')
            try:
                obj_mat = armature.matrix_local.copy()
            except Exception:
                obj_mat = Matrix.Identity(4)
            f.write(f'        <matrix sid="transform">{_hwugx_v630_floats(_hwugx_v630_matrix_values_row_major(obj_mat))}</matrix>\n')
            for root in roots:
                _hwugx_v630_write_joint_node(f, root, children_by_parent, id_by_bone, "        ")
            f.write('      </node>\n')
            f.write('    </visual_scene>\n')
            f.write('  </library_visual_scenes>\n')
            f.write(f'  <scene><instance_visual_scene url="#{scene_id}"/></scene>\n')
            f.write('</COLLADA>\n')

        report.update({
            "success": True,
            "manual_collada_fallback_used": True,
            "bone_count": len(bones),
            "bone_names": [b.name for b in bones],
            "frame_range": [frame0, frame1],
            "sample_count": len(frames),
            "fps": fps,
            "dae_size": os.path.getsize(dae_path) if os.path.isfile(dae_path) else 0,
            "channel_target_style": "bone_id/transform_matrix",
            "note": "Manual DAE fallback preserves the old sampled-action toolchain even when Blender has no built-in Collada exporter.",
        })
        return True, report
    except Exception as exc:
        report["success"] = False
        report["error"] = str(exc)
        return False, report


_hwugx_v630_previous_collada_export_sampled_action = globals().get("_hwugx_v620_collada_export_sampled_action")


def _hwugx_v620_collada_export_sampled_action(context, armature: bpy.types.Object, action: bpy.types.Action, dae_path: str, operator=None) -> tuple[bool, dict]:
    """v6.3 override: try Blender Collada first, then manual DAE fallback."""
    combined = {
        "dae_path": dae_path,
        "frame_range": [float(action.frame_range[0]), float(action.frame_range[1])],
        "selected_armature": armature.name if armature else "",
        "action": action.name if action else "",
        "blender_collada_operator_available": False,
        "manual_fallback_available": True,
    }

    # Try the real Blender Collada operator if this build still has it. This keeps
    # behavior exact on Blender versions that include Collada.
    if _hwugx_v630_previous_collada_export_sampled_action:
        try:
            has_op = hasattr(bpy.ops.wm, "collada_export")
        except Exception:
            has_op = False
        combined["blender_collada_operator_available"] = bool(has_op)
        if has_op:
            ok, rep = _hwugx_v630_previous_collada_export_sampled_action(context, armature, action, dae_path, operator)
            combined["blender_collada"] = rep
            if ok:
                combined["writer"] = "bpy.ops.wm.collada_export"
                return True, combined

    ok_manual, manual_report = _hwugx_v630_write_manual_collada_sampled_action(context, armature, action, dae_path, operator)
    combined["manual_collada"] = manual_report
    if ok_manual:
        combined["writer"] = "manual_collada_fallback_v640"
        combined["manual_collada_fallback_used"] = True
        return True, combined
    combined["error"] = manual_report.get("error", "manual_collada_fallback_failed")
    return False, combined


# Patch debug version string for the legacy writer without renaming all v620 helper
# functions; these names are intentionally retained as compatibility hooks.
_hwugx_v630_previous_write_legacy = globals().get("_write_legacy_dae_gr2_uax_from_action")

# The previous writer already calls _hwugx_v620_collada_export_sampled_action by
# global name, so the override above is enough. Keep this small wrapper only to
# make the debug version and export-mode note clearer.
def _write_legacy_dae_gr2_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    return _hwugx_v630_previous_write_legacy(action, uax_path, armature, settings, operator)


# -----------------------------------------------------------------------------
# v6.4.0 legacy toolchain success handling fix
# -----------------------------------------------------------------------------
# gr2ugx.exe can return a non-zero warning/status code even after producing a
# valid UAX. v6.3 treated any non-zero return as failure, which showed the
# user-facing error even when the output UAX existed and contained a valid ECF
# 0x700 animation chunk. This override validates the produced UAX first.

def _hwugx_v640_validate_uax_file(path: str) -> tuple[bool, dict]:
    info = {
        "path": path,
        "exists": False,
        "size": 0,
        "magic": None,
        "file_id": None,
        "file_size_field": None,
        "chunk_count": 0,
        "chunks": [],
        "has_animation_chunk_0x700": False,
        "error": "",
    }
    try:
        if not path or not os.path.isfile(path):
            info["error"] = "missing_file"
            return False, info
        info["exists"] = True
        info["size"] = int(os.path.getsize(path))
        if info["size"] < 64:
            info["error"] = "too_small"
            return False, info
        with open(path, "rb") as f:
            data = f.read()
        magic = struct.unpack(">I", data[0:4])[0]
        file_id = struct.unpack(">Q", data[4:12])[0]
        file_size = struct.unpack(">I", data[12:16])[0]
        chunk_count = struct.unpack(">H", data[16:18])[0]
        info["magic"] = hex(magic)
        info["file_id"] = hex(file_id)
        info["file_size_field"] = int(file_size)
        info["chunk_count"] = int(chunk_count)
        if magic != 0xDABA7737:
            info["error"] = "bad_ecf_magic"
            return False, info
        if file_size != len(data):
            # Some tools are strict here. Treat this as invalid so we do not mark
            # partial/truncated files successful.
            info["error"] = "file_size_field_mismatch"
            return False, info
        ok_chunk = False
        for i in range(chunk_count):
            off = 32 + (i * 24)
            if off + 16 > len(data):
                info["error"] = "chunk_header_out_of_bounds"
                return False, info
            chunk_id, chunk_off, chunk_len = struct.unpack(">QII", data[off:off + 16])
            entry = {
                "id": hex(chunk_id),
                "offset": int(chunk_off),
                "length": int(chunk_len),
                "in_bounds": bool(chunk_off + chunk_len <= len(data)),
            }
            info["chunks"].append(entry)
            if chunk_off + chunk_len > len(data):
                info["error"] = "chunk_out_of_bounds"
                return False, info
            if chunk_id == 0x700 and chunk_len > 0:
                ok_chunk = True
        info["has_animation_chunk_0x700"] = bool(ok_chunk)
        if not ok_chunk:
            info["error"] = "missing_0x700_animation_chunk"
            return False, info
        info["error"] = ""
        return True, info
    except Exception as exc:
        info["error"] = str(exc)
        return False, info


def _write_legacy_dae_gr2_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    """v6.4 override: old sampled DAE -> DAEtoGR2 -> gr2ugx path, with UAX-output validation.

    The old toolchain sometimes returns a non-zero gr2ugx process code even after
    writing a valid UAX. Use the file's ECF/UAX structure as the final success
    check instead of the process return code alone.
    """
    debug_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
    tool_dir, dae_to_gr2, gr2ugx = _hwugx_v620_find_legacy_tools(bpy.context)
    debug = {
        "version": "6.4.0",
        "export_mode": "legacy_dae_gr2ugx_toolchain",
        "action": getattr(action, "name", ""),
        "armature": getattr(armature, "name", ""),
        "output": uax_path,
        "tool_dir": tool_dir,
        "DAEtoGR2.exe": dae_to_gr2,
        "gr2ugx.exe": gr2ugx,
        "axis_back": getattr(settings, "axis_back", "Z-"),
        "axis_right": getattr(settings, "axis_right", "X-"),
        "axis_up": getattr(settings, "axis_up", "Y+"),
        "old_plugin_equivalent": "selected armature only; sampled Collada animation; DAEtoGR2.exe -debug; gr2ugx.exe -auto -anim",
        "v640_success_rule": "gr2ugx returncode is advisory; valid ECF/UAX with chunk 0x700 is treated as successful even if gr2ugx returns a nonzero warning/status code",
    }
    if not tool_dir or not dae_to_gr2 or not gr2ugx:
        debug["success"] = False
        debug["error"] = "legacy_tools_not_found"
        debug["searched_folders"] = _hwugx_v620_legacy_tool_candidates(bpy.context)
        _hwugx_v620_write_json(debug_path, debug)
        if operator:
            operator.report({"WARNING"}, "Legacy UAX tools not found. Put DAEtoGR2.exe and gr2ugx.exe in a bundled 'tool' folder beside the add-on.")
        return False

    temp_dir = tempfile.mkdtemp(prefix="hw_legacy_uax_")
    try:
        safe_name = _sanitize_filename(str(action.get("uax_source", "")) or action.name or "animation")
        dae_path = os.path.join(temp_dir, safe_name + "_tmp.dae")
        gr2_path = os.path.join(temp_dir, safe_name + "_tmp.gr2")
        os.makedirs(os.path.dirname(uax_path), exist_ok=True)

        ok_dae, dae_report = _hwugx_v620_collada_export_sampled_action(bpy.context, armature, action, dae_path, operator)
        debug["collada"] = dae_report
        if not ok_dae:
            debug["success"] = False
            debug["error"] = "collada_export_failed"
            _hwugx_v620_write_json(debug_path, debug)
            return False

        cmd1 = [dae_to_gr2, "-debug", dae_path, gr2_path]
        ok1, run1 = _hwugx_v620_run_external(cmd1, tool_dir, operator)
        debug["DAEtoGR2"] = run1
        debug["gr2_exists_after_DAEtoGR2"] = os.path.isfile(gr2_path)
        debug["gr2_size"] = os.path.getsize(gr2_path) if os.path.isfile(gr2_path) else 0
        if not ok1 or not os.path.isfile(gr2_path) or os.path.getsize(gr2_path) <= 0:
            debug["success"] = False
            debug["error"] = "DAEtoGR2_failed_or_no_gr2"
            _hwugx_v620_write_json(debug_path, debug)
            return False

        back = getattr(settings, "axis_back", "Z-") or "Z-"
        right = getattr(settings, "axis_right", "X-") or "X-"
        up = getattr(settings, "axis_up", "Y+") or "Y+"
        cmd2 = [gr2ugx, "-auto", "-anim", gr2_path, uax_path, back, right, up]
        ok2, run2 = _hwugx_v620_run_external(cmd2, tool_dir, operator)
        debug["gr2ugx"] = run2
        debug["uax_exists_after_gr2ugx"] = os.path.isfile(uax_path)
        debug["uax_size"] = os.path.getsize(uax_path) if os.path.isfile(uax_path) else 0
        valid_uax, uax_validation = _hwugx_v640_validate_uax_file(uax_path)
        debug["uax_validation"] = uax_validation
        debug["gr2ugx_returncode_accepted_as_warning"] = bool((not ok2) and valid_uax)

        if not valid_uax:
            debug["success"] = False
            debug["error"] = "gr2ugx_failed_or_invalid_uax"
            _hwugx_v620_write_json(debug_path, debug)
            return False

        debug["success"] = True
        debug["template_free"] = True
        debug["writer"] = "legacy_external_tools"
        debug["note"] = "DAEtoGR2 generated a GR2 and gr2ugx produced a valid UAX. Non-zero gr2ugx return codes are treated as warnings when the UAX validates."
        _hwugx_v620_write_json(debug_path, debug)
        if operator:
            extra = " with gr2ugx warning code" if not ok2 else ""
            operator.report({"INFO"}, f"Exported legacy-template-free UAX{extra}: {os.path.basename(uax_path)} ({os.path.getsize(uax_path):,} bytes)")
        return True
    finally:
        clean_temp_dir(temp_dir, bpy.context)


# -----------------------------------------------------------------------------
# v6.5.0 legacy manual-DAE orientation fix
# -----------------------------------------------------------------------------
# The legacy toolchain is now producing playable UAX files, but the manual DAE
# fallback used in Blender 5 emits raw Blender/armature matrices. The old Blender
# Collada exporter applied a coordinate-basis conversion before DAEtoGR2/gr2ugx.
# Without that conversion, gr2ugx bakes a +90 degree X rotation into the root
# Granny track, causing root-only structure/VFX animation to play sideways.
#
# Rather than disturb the working DAE->GR2->UAX path, v6.5 validates the UAX and
# then corrects the root transform track in-place when the manual DAE fallback was
# used: root rotation and root translation are premultiplied by -90 degrees about
# X. This removes the unwanted +90X basis offset while preserving the fact that
# the animation now plays in HW2.


def _hwugx_v650_quat_mul_xyzw(a, b):
    """Hamilton product a*b for quaternions stored as (x, y, z, w)."""
    ax, ay, az, aw = [float(v) for v in a]
    bx, by, bz, bw = [float(v) for v in b]
    x = aw * bx + ax * bw + ay * bz - az * by
    y = aw * by - ax * bz + ay * bw + az * bx
    z = aw * bz + ax * by - ay * bx + az * bw
    w = aw * bw - ax * bx - ay * by - az * bz
    length = math.sqrt(x*x + y*y + z*z + w*w)
    if length > 1.0e-12:
        return (x / length, y / length, z / length, w / length)
    return (0.0, 0.0, 0.0, 1.0)


def _hwugx_v650_quat_rotate_vec_xyzw(q, v):
    """Rotate vector v by quaternion q stored as (x, y, z, w)."""
    x, y, z, w = [float(t) for t in q]
    vx, vy, vz = [float(t) for t in v]
    # q * (v,0) * q^-1, expanded.
    tx = 2.0 * (y * vz - z * vy)
    ty = 2.0 * (z * vx - x * vz)
    tz = 2.0 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


def _hwugx_v650_cstr(data: bytes, off: int) -> str:
    try:
        if off < 0 or off >= len(data):
            return ""
        end = data.find(b"\0", off)
        if end < 0:
            end = len(data)
        return data[off:end].decode("utf-8", "replace")
    except Exception:
        return ""


def _hwugx_v650_curve_type_name(granny: bytes, type_ptr: int) -> str:
    try:
        _count, name_ptr = struct.unpack("<IQ", granny[type_ptr:type_ptr + 12])
        return _hwugx_v650_cstr(granny, int(name_ptr))
    except Exception:
        return ""


def _hwugx_v650_find_uax_chunk_700(raw: bytes):
    if len(raw) < 32:
        return None
    try:
        magic = struct.unpack(">I", raw[0:4])[0]
        if magic != 0xDABA7737:
            return None
        chunk_count = struct.unpack(">H", raw[16:18])[0]
        for i in range(chunk_count):
            off = 32 + (i * 24)
            if off + 16 > len(raw):
                return None
            chunk_id, chunk_off, chunk_len = struct.unpack(">QII", raw[off:off + 16])
            if chunk_id == 0x700:
                if chunk_off + chunk_len <= len(raw):
                    return int(chunk_off), int(chunk_len)
                return None
    except Exception:
        return None
    return None


def _hwugx_v650_patch_manual_dae_root_orientation(uax_path: str) -> dict:
    """Patch root track orientation in a produced UAX after manual DAE fallback.

    Returns a diagnostic dict. The patch is intentionally narrow: only the first
    transform track whose name starts with GrannyRootBone is changed, only when the
    curves use DaK32fC32f, which is what the legacy toolchain currently emits.
    """
    diag = {
        "enabled": True,
        "applied": False,
        "reason": "",
        "correction": "premultiply root rotation and root position by -90deg X to cancel manual DAE +90deg X basis offset",
        "root_track_name": "",
        "root_rot_first_before_xyzw": None,
        "root_rot_first_after_xyzw": None,
        "root_pos_first_before": None,
        "root_pos_first_after": None,
        "root_rot_key_count": 0,
        "root_pos_key_count": 0,
    }
    try:
        if not uax_path or not os.path.isfile(uax_path):
            diag["reason"] = "missing_uax"
            return diag
        raw = bytearray(open(uax_path, "rb").read())
        found = _hwugx_v650_find_uax_chunk_700(raw)
        if not found:
            diag["reason"] = "missing_chunk_0x700"
            return diag
        chunk_off, chunk_len = found
        granny = bytearray(raw[chunk_off:chunk_off + chunk_len])
        if len(granny) < 132:
            diag["reason"] = "granny_chunk_too_small"
            return diag

        track_groups_len, track_groups_off = struct.unpack("<IQ", granny[108:120])
        if track_groups_len <= 0:
            diag["reason"] = "no_track_groups"
            return diag

        root_track_off = None
        for gi in range(track_groups_len):
            if track_groups_off + gi * 8 + 8 > len(granny):
                continue
            group_ptr = struct.unpack("<Q", granny[track_groups_off + gi * 8:track_groups_off + gi * 8 + 8])[0]
            if group_ptr + 32 > len(granny):
                continue
            tracks_len, tracks_off = struct.unpack("<IQ", granny[group_ptr + 20:group_ptr + 32])
            for ti in range(tracks_len):
                cur = tracks_off + (ti * 60)
                if cur + 60 > len(granny):
                    continue
                name_off = struct.unpack("<Q", granny[cur:cur + 8])[0]
                name = _hwugx_v650_cstr(granny, int(name_off))
                if name.startswith("GrannyRootBone"):
                    root_track_off = cur
                    diag["root_track_name"] = name
                    break
            if root_track_off is not None:
                break

        if root_track_off is None:
            diag["reason"] = "no_GrannyRootBone_track"
            return diag

        rot_type_ptr, rot_obj_ptr, pos_type_ptr, pos_obj_ptr, scl_type_ptr, scl_obj_ptr = struct.unpack("<QQQQQQ", granny[root_track_off + 12:root_track_off + 60])
        rot_type = _hwugx_v650_curve_type_name(granny, int(rot_type_ptr))
        pos_type = _hwugx_v650_curve_type_name(granny, int(pos_type_ptr))
        diag["root_rot_curve_type"] = rot_type
        diag["root_pos_curve_type"] = pos_type
        if rot_type != "CurveDataHeader_DaK32fC32f":
            diag["reason"] = "unsupported_root_rot_curve_type"
            return diag

        # -90 degrees around X in xyzw.
        s = math.sqrt(0.5)
        qfix = (-s, 0.0, 0.0, s)
        diag["correction_quaternion_xyzw"] = list(qfix)

        # Patch rotation controls.
        if rot_obj_ptr + 28 > len(granny):
            diag["reason"] = "root_rot_curve_out_of_bounds"
            return diag
        header, knot_count, knot_off, control_count, control_off = struct.unpack("<IIQIQ", granny[rot_obj_ptr:rot_obj_ptr + 28])
        diag["root_rot_key_count"] = int(knot_count)
        if header != 0x201 or control_off + int(knot_count) * 16 > len(granny):
            diag["reason"] = "root_rot_controls_out_of_bounds"
            return diag
        for i in range(int(knot_count)):
            co = int(control_off) + i * 16
            q = struct.unpack("<ffff", granny[co:co + 16])
            if i == 0:
                diag["root_rot_first_before_xyzw"] = [float(v) for v in q]
            q2 = _hwugx_v650_quat_mul_xyzw(qfix, q)
            if i == 0:
                diag["root_rot_first_after_xyzw"] = [float(v) for v in q2]
            struct.pack_into("<ffff", granny, co, *q2)

        # Patch root position controls too, so root translation keeps Blender's
        # visible direction after the basis correction.
        if pos_type == "CurveDataHeader_DaK32fC32f" and pos_obj_ptr + 28 <= len(granny):
            p_header, p_knot_count, p_knot_off, p_control_count, p_control_off = struct.unpack("<IIQIQ", granny[pos_obj_ptr:pos_obj_ptr + 28])
            diag["root_pos_key_count"] = int(p_knot_count)
            if p_header == 0x201 and p_control_off + int(p_knot_count) * 12 <= len(granny):
                for i in range(int(p_knot_count)):
                    po = int(p_control_off) + i * 12
                    v = struct.unpack("<fff", granny[po:po + 12])
                    if i == 0:
                        diag["root_pos_first_before"] = [float(t) for t in v]
                    v2 = _hwugx_v650_quat_rotate_vec_xyzw(qfix, v)
                    if i == 0:
                        diag["root_pos_first_after"] = [float(t) for t in v2]
                    struct.pack_into("<fff", granny, po, *v2)

        raw[chunk_off:chunk_off + chunk_len] = granny
        with open(uax_path, "wb") as f:
            f.write(raw)
        diag["applied"] = True
        diag["reason"] = "ok"
        return diag
    except Exception as exc:
        diag["reason"] = str(exc)
        return diag


# Keep a handle to the v6.4 writer and wrap it so the orientation patch runs only
# when the manual Collada fallback created the DAE. If Blender's real Collada
# exporter is available, leave the old behavior untouched.
_hwugx_v650_previous_write_legacy = globals().get("_write_legacy_dae_gr2_uax_from_action")


def _write_legacy_dae_gr2_uax_from_action(action: bpy.types.Action, uax_path: str, armature: bpy.types.Object, settings, operator=None) -> bool:
    """v6.5 override: legacy DAE->GR2->UAX plus root orientation correction for manual DAE fallback."""
    ok = _hwugx_v650_previous_write_legacy(action, uax_path, armature, settings, operator)
    debug_path = os.path.splitext(uax_path)[0] + "_uax_export_debug.txt"
    try:
        debug = {}
        if os.path.isfile(debug_path):
            with open(debug_path, "r", encoding="utf-8") as f:
                debug = json.load(f)
        manual_used = bool((((debug.get("collada") or {}).get("manual_collada_fallback_used")) or ((debug.get("collada") or {}).get("manual_collada") or {}).get("manual_collada_fallback_used")))
        debug["version"] = "6.5.0"
        debug["v650_orientation_rule"] = "When manual DAE fallback is used, patch the produced UAX root track by -90deg X because Blender 5 lacks the old Collada exporter's basis conversion."
        debug["manual_collada_root_orientation_patch_required"] = bool(manual_used)
        if ok and manual_used and os.path.isfile(uax_path):
            patch_diag = _hwugx_v650_patch_manual_dae_root_orientation(uax_path)
            debug["manual_collada_root_orientation_patch"] = patch_diag
            # Revalidate after patch.
            valid_uax, uax_validation = _hwugx_v640_validate_uax_file(uax_path)
            debug["uax_validation_after_v650_orientation_patch"] = uax_validation
            if not valid_uax:
                debug["success"] = False
                debug["error"] = "v650_orientation_patch_invalidated_uax"
                ok = False
            else:
                debug["success"] = True
                debug["error"] = debug.get("error", "") if debug.get("error") not in ("v650_orientation_patch_invalidated_uax",) else ""
                if operator and patch_diag.get("applied"):
                    operator.report({"INFO"}, "Applied v6.5 manual-DAE root orientation fix (-90deg X) to UAX.")
        _hwugx_v620_write_json(debug_path, debug)
    except Exception as exc:
        try:
            debug = {"version": "6.5.0", "success": bool(ok), "v650_orientation_patch_error": str(exc)}
            _hwugx_v620_write_json(debug_path, debug)
        except Exception:
            pass
    return ok


# -----------------------------------------------------------------------------
# v6.6.0 clean export UI / proven legacy UAX workflow only
# -----------------------------------------------------------------------------
# The working in-game path is now known: sampled DAE -> DAEtoGR2.exe ->
# gr2ugx.exe -auto -anim, with the v6.5 manual-DAE root orientation patch.
# This block makes that workflow the front-and-center UI and hides older scratch,
# template, helper, and experimental controls from the normal export workflow.

bl_info["version"] = (6, 6, 0)
bl_info["description"] = "Clean Halo Wars 2 UGX/UAX pipeline. UAX export uses the proven legacy sampled DAE -> GR2 -> UAX toolchain with automatic v6.5 orientation correction."
bl_info["warning"] = "For UAX export, bundle tool/DAEtoGR2.exe and tool/gr2ugx.exe beside this add-on. ugx.exe is only needed for UGX model import/export."


def _hwugx_v660_legacy_tool_status(context=None):
    try:
        tool_dir, dae_to_gr2, gr2ugx = _hwugx_v620_find_legacy_tools(context)
    except Exception:
        tool_dir, dae_to_gr2, gr2ugx = "", "", ""
    return {
        "tool_dir": tool_dir or "",
        "dae_to_gr2": dae_to_gr2 or "",
        "gr2ugx": gr2ugx or "",
        "ok": bool(tool_dir and dae_to_gr2 and os.path.isfile(dae_to_gr2) and gr2ugx and os.path.isfile(gr2ugx)),
    }


def _hwugx_v660_clean_prefs_draw(self, context):
    layout = self.layout
    layout.use_property_split = True

    uax_status = _hwugx_v660_legacy_tool_status(context)
    uax_box = layout.box()
    row = uax_box.row(align=True)
    row.alert = not uax_status["ok"]
    row.label(text=("Legacy UAX Tools Ready" if uax_status["ok"] else "Legacy UAX Tools Missing"), icon=("CHECKMARK" if uax_status["ok"] else "ERROR"))
    uax_box.prop(self, "legacy_tool_dir", text="Tool Folder")
    hint = uax_box.column(align=True)
    hint.scale_y = 0.8
    hint.label(text="Expected: tool/DAEtoGR2.exe and tool/gr2ugx.exe beside the add-on.", icon="INFO")
    if uax_status["tool_dir"]:
        hint.label(text=os.path.normpath(uax_status["tool_dir"]), icon="FILE_FOLDER")

    model_box = layout.box()
    model_box.label(text="UGX Model Converter", icon="MESH_DATA")
    model_box.prop(self, "ugx_exe_path", text="ugx.exe")
    model_box.prop(self, "keep_temp_files", text="Keep Temp Files")
    note = model_box.column(align=True)
    note.scale_y = 0.8
    note.label(text="UAX animation export does not use ugx.exe; it uses the legacy tool folder above.", icon="INFO")


try:
    HWUGX_AddonPreferences.draw = _hwugx_v660_clean_prefs_draw
except Exception:
    pass


# Cleaner labels for the existing operators.
try:
    HWUGX_OT_export_animation_gltf.bl_label = "Export UAX Animation(s)"
    HWUGX_OT_export_animation_gltf.bl_description = "Export selected armature action(s) through the proven legacy DAE -> GR2 -> UAX toolchain"
    HWUGX_OT_export_ugx.bl_label = "Export UGX Model"
    HWUGX_OT_export_ugx.bl_description = "Export a Halo Wars UGX model through ugx.exe. UAX animation export is handled separately."
    HWUGX_OT_import_ugx.bl_label = "Import UGX Model"
    HWUGX_OT_import_uax.bl_label = "Import UAX Animation"
except Exception:
    pass


def _hwugx_v660_draw_uax_settings(layout, settings, context):
    tools = _hwugx_v660_legacy_tool_status(context)
    armature = _resolve_export_armature(context, settings) if settings else None

    box = layout.box()
    row = box.row(align=True)
    row.label(text="UAX Animation Export", icon="ACTION")
    row = box.row(align=True)
    row.alert = not tools["ok"]
    row.label(text=("Legacy toolchain ready" if tools["ok"] else "Legacy toolchain missing"), icon=("CHECKMARK" if tools["ok"] else "ERROR"))

    if not tools["ok"]:
        warn = box.column(align=True)
        warn.scale_y = 0.85
        warn.label(text="Bundle DAEtoGR2.exe and gr2ugx.exe in the add-on's tool folder.", icon="INFO")

    target = box.box()
    target.label(text="Target", icon="ARMATURE_DATA")
    target.prop(settings, "selected_armature", text="Armature")
    row = target.row(align=True)
    row.alert = armature is None
    row.label(text=("Selected: " + armature.name if armature else "No armature resolved"), icon=("CHECKMARK" if armature else "ERROR"))

    actions = box.box()
    actions.label(text="Actions", icon="ACTION_TWEAK")
    actions.prop(settings, "export_all_actions", text="Export All Actions")
    if not getattr(settings, "export_all_actions", True):
        actions.prop(settings, "uax_export_action", text="Action")
    actions.prop(settings, "animation_export_path", text="Output Folder")

    legacy = box.box()
    legacy.label(text="Working Method", icon="FILE_TICK")
    legacy.label(text="Sampled DAE  →  DAEtoGR2.exe  →  gr2ugx.exe  →  UAX", icon="RIGHTARROW")
    row = legacy.row(align=True)
    row.prop(settings, "axis_back", text="Back")
    row.prop(settings, "axis_right", text="Right")
    legacy.prop(settings, "axis_up", text="Up")
    legacy.prop(settings, "uax_write_debug_report", text="Write Debug Report")
    hint = legacy.column(align=True)
    hint.scale_y = 0.8
    hint.label(text="Default orientation is the confirmed working setup: Back Z-, Right X-, Up Y+.", icon="INFO")
    hint.label(text="v6.5+ automatically patches manual-DAE root orientation after gr2ugx.", icon="CHECKMARK")


def _hwugx_v660_draw_ugx_model_settings(layout, settings, context, *, include_hw2=True):
    ugx_path = get_ugx_exe_path(context)
    converter_ok = bool(ugx_path and os.path.isfile(ugx_path))
    box = layout.box()
    row = box.row(align=True)
    row.label(text="UGX Model Export", icon="MESH_DATA")
    row = box.row(align=True)
    row.alert = not converter_ok
    row.label(text=("ugx.exe ready" if converter_ok else "ugx.exe missing"), icon=("CHECKMARK" if converter_ok else "ERROR"))
    row.operator("hwugx.set_converter_path", text="Browse", icon="FILE_FOLDER")
    box.prop(settings, "export_meshes", text="Include Meshes")
    box.prop(settings, "export_selection_only", text="Selected Only")
    if include_hw2 and hasattr(settings, "hw2_version"):
        box.prop(settings, "hw2_version", text="Halo Wars 2 Format")
    note = box.column(align=True)
    note.scale_y = 0.8
    note.label(text="Model export still uses ugx.exe. Animation export uses the legacy UAX toolchain above.", icon="INFO")


def draw_export_settings_ui(layout, settings, *, include_hw2=True, include_header=True):
    """v6.6 clean export settings UI.

    This replaces the older panel that exposed scratch/native/template fallback
    controls. Those code paths remain internally for imported UAX round-trip and
    diagnostics, but the normal workflow now exposes only the method that was
    proven working in Halo Wars 2.
    """
    layout.use_property_split = False
    if include_header:
        header = layout.row(align=True)
        header.label(text="Halo Wars 2 Export", icon="SHADERFX")
    _hwugx_v660_draw_uax_settings(layout, settings, bpy.context)
    _hwugx_v660_draw_ugx_model_settings(layout, settings, bpy.context, include_hw2=include_hw2)


def _hwugx_v660_main_panel_draw(self, context):
    layout = self.layout
    layout.use_property_split = False
    settings = get_scene_export_settings(context)
    armature = find_target_armature(context)
    tools = _hwugx_v660_legacy_tool_status(context)
    ugx_path = get_ugx_exe_path(context)
    ugx_ok = bool(ugx_path and os.path.isfile(ugx_path))

    hero = layout.box()
    hero.scale_y = 1.05
    hero.label(text="HALO WARS 2 PIPELINE", icon="SHADERFX")
    row = hero.row(align=True)
    row.alert = not tools["ok"]
    row.label(text=("UAX Export Ready" if tools["ok"] else "UAX Tools Missing"), icon=("CHECKMARK" if tools["ok"] else "ERROR"))
    row = hero.row(align=True)
    row.alert = not ugx_ok
    row.label(text=("UGX Model Converter Ready" if ugx_ok else "UGX Model Converter Missing"), icon=("CHECKMARK" if ugx_ok else "INFO"))
    row.operator("hwugx.set_converter_path", text="Browse ugx.exe", icon="FILE_FOLDER")

    import_box = layout.box()
    import_box.label(text="Import", icon="IMPORT")
    row = import_box.row(align=True)
    row.operator("import_scene.hw_ugx", text="Import UGX Model", icon="MESH_DATA")
    row.operator("import_anim.hw_uax", text="Import UAX", icon="ACTION")
    note = import_box.column(align=True)
    note.scale_y = 0.75
    note.label(text="UGX import cleanup remains automatic.", icon="CHECKMARK")

    if settings is None:
        layout.label(text="Scene settings unavailable.", icon="ERROR")
        return

    uax = layout.box()
    uax.label(text="Export UAX Animation", icon="ACTION")
    row = uax.row(align=True)
    row.alert = armature is None
    row.label(text=("Target: " + armature.name if armature else "Choose/select an armature"), icon=("ARMATURE_DATA" if armature else "ERROR"))
    uax.prop(settings, "selected_armature", text="Armature")
    uax.prop(settings, "export_all_actions", text="All Actions")
    if not getattr(settings, "export_all_actions", True):
        uax.prop(settings, "uax_export_action", text="Action")
    uax.prop(settings, "animation_export_path", text="Output Folder")
    row = uax.row(align=True)
    row.scale_y = 1.15
    op = row.operator("export_scene.hw_ugx_animation_sidecar", text="Export UAX", icon="EXPORT")
    detail = uax.column(align=True)
    detail.scale_y = 0.75
    detail.label(text="Uses the proven sampled DAE → GR2 → UAX legacy toolchain.", icon="FILE_TICK")
    detail.label(text="Orientation patch is automatic for Blender 5 manual DAE output.", icon="CHECKMARK")

    model = layout.box()
    model.label(text="Model Tools", icon="MESH_DATA")
    row = model.row(align=True)
    row.operator("export_scene.hw_ugx", text="Export UGX Model", icon="MESH_DATA")
    row.operator("hwugx.set_converter_path", text="Set ugx.exe", icon="FILE_FOLDER")
    model.prop(settings, "export_selection_only", text="Selected Only")
    model.prop(settings, "export_meshes", text="Include Meshes")


try:
    HWUGX_PT_main_panel.bl_label = "Halo Wars 2 Pipeline"
    HWUGX_PT_main_panel.draw = _hwugx_v660_main_panel_draw
except Exception:
    pass


# Remove the old animation-helper panel from the normal add-on registration. The
# operators/properties remain in the file for backward compatibility with old
# saved projects, but the UI now focuses on the proven export workflow.
try:
    classes = tuple(cls for cls in classes if getattr(cls, "__name__", "") != "HWUGX_PT_animation_helper_panel")
except Exception:
    pass


# v6.6 export policy: for brand-new/custom actions, require the proven legacy
# toolchain. Keep imported-UAX exact round-trip first, but do not fall back to the
# old scratch writer just because tools are missing.
def _export_uax_sidecars_from_current_settings(context, temp_dir: str, destination_dir: str, base_name: str, operator=None) -> int:
    """v6.6 clean exporter: imported-cache round-trip first, then required legacy DAE->GR2->UAX."""
    settings = get_scene_export_settings(context)
    if not destination_dir:
        return 0
    dest = bpy.path.abspath(destination_dir)
    os.makedirs(dest, exist_ok=True)

    armature = _resolve_export_armature(context, settings)
    if not armature:
        if operator:
            operator.report({"ERROR"}, "Cannot export UAX animations: select or choose an armature.")
        return 0
    if not armature.animation_data:
        armature.animation_data_create()

    actions = _iter_candidate_actions_for_armature(
        armature,
        all_actions=(settings.export_all_actions if settings else True),
        specific_action_name=(getattr(settings, "uax_export_action", "") if settings and not settings.export_all_actions else ""),
    )
    if not actions:
        if operator:
            operator.report({"ERROR"}, "No actions found to export as UAX.")
        return 0

    tool_dir, dae_to_gr2, gr2ugx = _hwugx_v620_find_legacy_tools(context)
    legacy_ok = bool(tool_dir and dae_to_gr2 and os.path.isfile(dae_to_gr2) and gr2ugx and os.path.isfile(gr2ugx))
    if not legacy_ok:
        if operator:
            operator.report({"ERROR"}, "Legacy UAX tools missing. Bundle tool/DAEtoGR2.exe and tool/gr2ugx.exe beside the add-on, or set the Tool Folder in preferences.")
        return 0

    old_action = armature.animation_data.action
    old_active = context.view_layer.objects.active
    old_selected = list(context.selected_objects)
    exported = 0
    try:
        for action in actions:
            action_base = _sanitize_filename(str(action.get("uax_source", "")) or action.name or base_name)
            uax_path = os.path.join(dest, action_base + ".uax")

            # Exact round-trip for actions imported from existing UAX files.
            if _write_native_uax_from_action(
                action,
                uax_path,
                armature,
                operator,
                rewrite_clip_name=bool(getattr(settings, "uax_export_rewrite_names", True)),
            ):
                exported += 1
                continue

            # Proven custom/template-free export path.
            if _write_legacy_dae_gr2_uax_from_action(action, uax_path, armature, settings, operator):
                exported += 1
                continue

            if operator:
                operator.report({"ERROR"}, f"Legacy UAX toolchain failed for {action.name}. See the generated debug report beside the .uax path.")

        if operator and exported:
            operator.report({"INFO"}, f"Exported {exported} UAX animation file(s) to {dest}")
        return exported
    finally:
        try:
            armature.animation_data.action = old_action
            bpy.ops.object.select_all(action="DESELECT")
            for obj in old_selected:
                if obj and obj.name in bpy.data.objects:
                    obj.select_set(True)
            if old_active and old_active.name in bpy.data.objects:
                context.view_layer.objects.active = old_active
        except Exception:
            pass


# -----------------------------------------------------------------------------
# v6.7.0 UI restore: UAX Animation Helper in its own sidebar tab
# -----------------------------------------------------------------------------
# v6.6 intentionally cleaned the main UGX pipeline panel around the proven legacy
# DAE -> GR2 -> UAX exporter. The helper tools are still useful, so restore the
# panel but move it out of the UGX tab to keep the export UI straightforward.
try:
    HWUGX_PT_animation_helper_panel.bl_category = "UAX"
    HWUGX_PT_animation_helper_panel.bl_label = "Animation Helper"
    HWUGX_PT_animation_helper_panel.bl_description = "Timeline offset and grounding tools for UAX animation cleanup"

    _hwugx_v670_old_helper_draw = HWUGX_PT_animation_helper_panel.draw

    def _hwugx_v670_animation_helper_draw(self, context):
        layout = self.layout
        banner = layout.box()
        banner.scale_y = 0.85
        banner.label(text="UAX Animation Helper", icon="ACTION_TWEAK")
        hint = banner.column(align=True)
        hint.scale_y = 0.75
        hint.label(text="Use this before Export UAX to clean offsets or grounding.", icon="INFO")
        _hwugx_v670_old_helper_draw(self, context)

    HWUGX_PT_animation_helper_panel.draw = _hwugx_v670_animation_helper_draw

    # v6.6 removed this panel from the registration list. Add it back after the
    # removal block, so it registers normally when the add-on is enabled.
    if HWUGX_PT_animation_helper_panel not in classes:
        classes = tuple(classes) + (HWUGX_PT_animation_helper_panel,)
except Exception:
    pass


# -----------------------------------------------------------------------------
# v6.8.0 custom UAX import restoration
# -----------------------------------------------------------------------------
# Custom UAX files generated by the proven legacy DAE -> GR2 -> gr2ugx path can
# animate GrannyRootBone directly and often use scale-shear-only curves for
# structure/VFX motion. Earlier Blender 5 builds imported the file but then
# locked GrannyRootBone and discarded scale-shear, making those custom clips look
# static in a fresh .blend. v6.8 preserves GrannyRootBone by default and imports
# scale-shear into Blender pose scale curves.
