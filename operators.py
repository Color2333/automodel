import bpy
import os
import glob
import csv
import datetime
import shutil
import unicodedata
import hashlib
import json
import struct
from bpy.props import StringProperty, IntProperty, EnumProperty, BoolProperty, FloatProperty
from bpy.types import Operator
from mathutils import Vector
from .properties import HARDFIX_TAG_ITEMS, resolve_output_base_directory

# 状态显示名（日志/UI 提示）；内部枚举 ID 不变
status_labels = {
    'UNMARKED': "未标记",
    'COMPLETED': "可修复",
    'FIXED': "已修复",
    'NO_ACTION': "无需修复",
    'QUESTIONABLE': "存疑",
    'UNFIXABLE': "bad",
    'HARD': "难以修复",
    'PARTS': "零件",
}

FINAL_EXPORT_STATUSES = ('NO_ACTION', 'FIXED', 'HARD')
EXPORT_FILE_PREFIXES = {
    'NO_ACTION': "nologo",
    'FIXED': "fixed",
    'HARD': "hardfix",
}
CSV_STATUS_VALUES = {
    'NO_ACTION': "nologo",
    'FIXED': "fixed",
    'HARD': "hardfix",
}
HARDFIX_TAG_LABELS = {item[0]: item[1] for item in HARDFIX_TAG_ITEMS}
HARDFIX_TAG_FOLDERS = dict(HARDFIX_TAG_LABELS)
_SOURCE_GLB_ATTRIBUTE_INFO_CACHE = {}
SCENE_SOURCE_PATH_PROP = "meshy_autoglb_current_source_path"
LOGOSLOT_BOOLEAN_MODIFIER_NAME = "LogoSlot_Boolean"
LOGOSLOT_BOOLEAN_OBJECT_PREFIX = "LogoSlot_Boolean"
LOGOSLOT_BOOLEAN_HELPER_PROP = "logoslot_boolean_helper"


def hardfix_tag_label(model):
    return HARDFIX_TAG_LABELS.get(getattr(model, "hardfix_tag", ""), "")


def hardfix_report_reason(model):
    tag = getattr(model, "hardfix_tag", "HARD_TO_FIX")
    if tag == 'OTHER':
        return (getattr(model, "hardfix_reason", "") or "").strip()
    return HARDFIX_TAG_LABELS.get(tag, "")


def hardfix_status_detail(model):
    label = hardfix_tag_label(model)
    if not label:
        return ""
    reason = (getattr(model, "hardfix_reason", "") or "").strip()
    if getattr(model, "hardfix_tag", "") == 'OTHER' and reason:
        return f"{label}: {reason}"
    return label


def hardfix_tag_folder(model):
    return HARDFIX_TAG_FOLDERS.get(getattr(model, "hardfix_tag", ""), HARDFIX_TAG_FOLDERS['HARD_TO_FIX'])


def _meshy_operator_folder_suffix(settings):
    op = settings.operator_name or "unknown"
    return op.replace("/", "_").replace("\\", "_")


def status_subdir_for_export(status, operator_suffix):
    """输出根目录下的子文件夹名（与磁盘一致）。"""
    mapping = {
        'NO_ACTION': f"NoLogo_{operator_suffix}",
        'FIXED': f"Fixed_{operator_suffix}",
        'HARD': f"HardFix_{operator_suffix}",
        # 旧状态保留目录映射，便于读取/清理旧项目，不在 UI 中继续暴露。
        'COMPLETED': f"Completed_{operator_suffix}",
        'UNFIXABLE': f"Bad_{operator_suffix}",
        'QUESTIONABLE': f"Questionable_{operator_suffix}",
        'PARTS': f"Parts_{operator_suffix}",
    }
    return mapping.get(status)


def status_export_directory(base, status, operator_suffix, model=None):
    subdir = status_subdir_for_export(status, operator_suffix)
    if not subdir:
        return ""
    path = os.path.join(base, subdir)
    if status == 'HARD' and model is not None:
        path = os.path.join(path, hardfix_tag_folder(model))
    return path


def iter_status_export_directories(base, status, operator_suffix):
    subdir = status_subdir_for_export(status, operator_suffix)
    if not subdir:
        return []
    root = os.path.join(base, subdir)
    if status != 'HARD':
        return [root]

    directories = [root]
    directories.extend(os.path.join(root, folder) for folder in HARDFIX_TAG_FOLDERS.values())
    if os.path.isdir(root):
        for name in os.listdir(root):
            path = os.path.join(root, name)
            if os.path.isdir(path) and path not in directories:
                directories.append(path)
    return directories


def export_subdir_for_path(base, path):
    try:
        return os.path.dirname(os.path.relpath(path, base))
    except ValueError:
        return os.path.basename(os.path.dirname(path))


def output_base_directory(settings):
    return resolve_output_base_directory(settings)


def deselect_all_objects(context):
    """Avoid bpy.ops.object.select_all poll failures in non-standard UI contexts."""
    for obj in context.scene.objects:
        try:
            obj.select_set(False)
        except ReferenceError:
            continue


def ensure_active_object(context, objects):
    for obj in objects:
        try:
            if obj and obj.name and obj.name in context.view_layer.objects:
                context.view_layer.objects.active = obj
                return obj
        except ReferenceError:
            continue
    return None


def _mark_scene_source(context, model):
    try:
        context.scene[SCENE_SOURCE_PATH_PROP] = os.path.realpath(model.path or "")
    except Exception:
        pass


def _clear_scene_source(context):
    try:
        if SCENE_SOURCE_PATH_PROP in context.scene:
            del context.scene[SCENE_SOURCE_PATH_PROP]
    except Exception:
        pass


def scene_matches_current_model(context, model):
    try:
        return context.scene.get(SCENE_SOURCE_PATH_PROP, "") == os.path.realpath(model.path or "")
    except Exception:
        return False


def find_view3d_override_context():
    window_manager = bpy.context.window_manager
    if not window_manager:
        return None
    for window in window_manager.windows:
        screen = window.screen
        if not screen:
            continue
        for area in screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type == 'WINDOW':
                    return {
                        "window": window,
                        "screen": screen,
                        "area": area,
                        "region": region,
                    }
    return None


def _gltf_export_supported_kwargs():
    try:
        return {prop.identifier for prop in bpy.ops.export_scene.gltf.get_rna_type().properties}
    except Exception:
        return None


def _filter_gltf_export_kwargs(kwargs):
    supported = _gltf_export_supported_kwargs()
    if supported is None:
        return kwargs
    return {key: value for key, value in kwargs.items() if key in supported}


def export_gltf_with_safe_context(filepath, active_object, export_normals, export_texcoords):
    kwargs = dict(
        filepath=filepath,
        export_format='GLB',
        use_selection=True,
        use_visible=True,
        export_apply=True,
        export_normals=export_normals,
        export_texcoords=export_texcoords,
        export_extras=False,
        export_animations=False,
        export_image_format='AUTO',
    )
    kwargs = _filter_gltf_export_kwargs(kwargs)
    override_ctx = find_view3d_override_context()
    if override_ctx:
        with bpy.context.temp_override(**override_ctx, active_object=active_object, object=active_object):
            return bpy.ops.export_scene.gltf('EXEC_DEFAULT', **kwargs)
    return bpy.ops.export_scene.gltf('EXEC_DEFAULT', **kwargs)


def validate_glb_file(filepath):
    if not filepath or not os.path.isfile(filepath):
        return False, "文件不存在"
    try:
        file_size = os.path.getsize(filepath)
        if file_size < 20:
            return False, f"文件过小: {file_size} bytes"
        with open(filepath, "rb") as f:
            header = f.read(12)
            if len(header) != 12:
                return False, "GLB header 不完整"
            magic, version, declared_length = struct.unpack("<4sII", header)
            if magic != b"glTF":
                return False, "GLB magic 不正确"
            if version != 2:
                return False, f"不支持的 GLB version: {version}"
            if declared_length != file_size:
                return False, f"GLB 长度不匹配: header={declared_length}, actual={file_size}"

            offset = 12
            has_json = False
            while offset < declared_length:
                chunk_header = f.read(8)
                if len(chunk_header) != 8:
                    return False, f"chunk header 截断: offset={offset}"
                chunk_length, chunk_type = struct.unpack("<I4s", chunk_header)
                offset += 8
                if chunk_length > declared_length - offset:
                    return False, f"chunk 长度越界: offset={offset}, chunk={chunk_length}"
                chunk = f.read(chunk_length)
                if len(chunk) != chunk_length:
                    return False, f"chunk 数据截断: offset={offset}, chunk={chunk_length}"
                if chunk_type == b"JSON":
                    try:
                        json.loads(chunk.decode("utf-8").rstrip(" \t\r\n\0"))
                    except Exception as e:
                        return False, f"JSON chunk 无法解析: {e}"
                    has_json = True
                offset += chunk_length
            if offset != declared_length:
                return False, f"GLB 读取位置异常: offset={offset}, length={declared_length}"
            if not has_json:
                return False, "缺少 JSON chunk"
            return True, ""
    except OSError as e:
        return False, f"读取失败: {e}"
    except Exception as e:
        return False, f"GLB 校验失败: {e}"


def is_object_in_view_layer(context, obj):
    try:
        return obj and obj.name and obj.name in context.view_layer.objects
    except ReferenceError:
        return False


def object_visible_for_export(context, obj):
    if not is_object_in_view_layer(context, obj):
        return False
    try:
        if obj.hide_get() or obj.hide_viewport:
            return False
    except ReferenceError:
        return False
    try:
        return obj.visible_get(view_layer=context.view_layer)
    except TypeError:
        try:
            return obj.visible_get()
        except Exception:
            return True
    except Exception:
        return True


def is_logoslot_boolean_helper_object(obj):
    if not obj or obj.type != 'MESH':
        return False
    try:
        if obj.get(LOGOSLOT_BOOLEAN_HELPER_PROP):
            return True
        if obj.name.startswith(LOGOSLOT_BOOLEAN_OBJECT_PREFIX):
            return True
    except ReferenceError:
        return False
    return False


def active_boolean_operand_objects(objects):
    operands = set()
    for obj in objects:
        if not obj or obj.type != 'MESH':
            continue
        try:
            modifiers = list(obj.modifiers)
        except ReferenceError:
            continue
        for modifier in modifiers:
            if modifier.type != 'BOOLEAN':
                continue
            if hasattr(modifier, "show_viewport") and not modifier.show_viewport:
                continue
            operand = getattr(modifier, "object", None)
            if operand and operand.type == 'MESH':
                operands.add(operand)
    return operands


def logoslot_boolean_helper_objects(context, objects):
    helpers = {obj for obj in objects if is_logoslot_boolean_helper_object(obj)}
    scan_objects = set(objects)
    scan_objects.update(active_boolean_operand_objects(objects))
    for obj in scan_objects:
        if not obj or obj.type != 'MESH':
            continue
        try:
            modifiers = list(obj.modifiers)
        except ReferenceError:
            continue
        for modifier in modifiers:
            if modifier.type != 'BOOLEAN':
                continue
            operand = getattr(modifier, "object", None)
            if (
                modifier.name == LOGOSLOT_BOOLEAN_MODIFIER_NAME
                and operand
                and operand.type == 'MESH'
            ):
                helpers.add(operand)
    for obj in context.scene.objects:
        if is_logoslot_boolean_helper_object(obj):
            helpers.add(obj)
    return helpers


def prepare_objects_for_export(context, objects):
    helpers = logoslot_boolean_helper_objects(context, objects)
    export_objects = []
    skipped = []
    seen = set()
    for obj in objects:
        if not is_object_in_view_layer(context, obj):
            continue
        if obj.type == 'MESH':
            if obj in helpers or is_logoslot_boolean_helper_object(obj):
                skipped.append(obj)
                continue
            if not object_visible_for_export(context, obj):
                skipped.append(obj)
                continue
        if obj not in seen:
            export_objects.append(obj)
            seen.add(obj)
    return export_objects, skipped


def _world_bbox_key(obj, precision=4):
    corners = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    mins = [min(corner[i] for corner in corners) for i in range(3)]
    maxs = [max(corner[i] for corner in corners) for i in range(3)]
    return tuple(round(value, precision) for value in (mins + maxs))


def _mesh_duplicate_signature(obj):
    if not obj or obj.type != 'MESH':
        return None
    try:
        mesh = obj.data
        return (
            len(mesh.vertices),
            len(mesh.polygons),
            len(mesh.edges),
            _world_bbox_key(obj),
        )
    except ReferenceError:
        return None


def overlapping_duplicate_mesh_groups(objects):
    groups = {}
    for obj in objects:
        if not obj or obj.type != 'MESH':
            continue
        signature = _mesh_duplicate_signature(obj)
        if signature is None:
            continue
        groups.setdefault(signature, []).append(obj)
    return [group for group in groups.values() if len(group) > 1]


def format_duplicate_mesh_groups(groups, limit=4):
    labels = []
    for group in groups[:limit]:
        labels.append("/".join(obj.name for obj in group[:4]))
    if len(groups) > limit:
        labels.append(f"...另 {len(groups) - limit} 组")
    return "; ".join(labels)


def transform_objects_for_boolean_export(context, export_objects):
    transform_objects = list(export_objects)
    seen = set(transform_objects)
    for operand in active_boolean_operand_objects(export_objects):
        if is_object_in_view_layer(context, operand) and operand not in seen:
            transform_objects.append(operand)
            seen.add(operand)
    return transform_objects


def commit_pending_mesh_edits(context, report_fn=None):
    """Commit Sculpt/Edit-mode changes before glTF export reads object mesh data."""
    active = context.view_layer.objects.active
    mode = getattr(active, "mode", "OBJECT") if active else "OBJECT"
    try:
        bpy.ops.ed.flush_edits()
    except Exception:
        pass
    if mode != 'OBJECT':
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
            if report_fn:
                report_fn({'INFO'}, f"导出前已从 {mode} 模式切回 Object 并提交编辑数据")
        except Exception as e:
            if report_fn:
                report_fn({'ERROR'}, f"导出前无法提交 {mode} 模式编辑数据: {e}")
            return False
    context.view_layer.update()
    return True


def source_glb_attribute_info(filepath):
    if not filepath or not filepath.lower().endswith(".glb") or not os.path.isfile(filepath):
        return None
    try:
        stat = os.stat(filepath)
        cache_key = (os.path.realpath(filepath), stat.st_mtime_ns, stat.st_size)
        if cache_key in _SOURCE_GLB_ATTRIBUTE_INFO_CACHE:
            return _SOURCE_GLB_ATTRIBUTE_INFO_CACHE[cache_key]
    except OSError:
        return None
    try:
        with open(filepath, "rb") as f:
            header = f.read(12)
            if len(header) != 12:
                return None
            magic, _version, length = struct.unpack("<4sII", header)
            if magic != b"glTF":
                return None
            while f.tell() < length:
                chunk_header = f.read(8)
                if len(chunk_header) != 8:
                    return None
                chunk_length, chunk_type = struct.unpack("<I4s", chunk_header)
                chunk = f.read(chunk_length)
                if chunk_type != b"JSON":
                    continue
                gltf = json.loads(chunk.decode("utf-8"))
                info = []
                for mesh in gltf.get("meshes", []):
                    for primitive in mesh.get("primitives", []):
                        attrs = primitive.get("attributes", {})
                        pos_count = 0
                        index_count = 0
                        if "POSITION" in attrs:
                            pos_count = gltf["accessors"][attrs["POSITION"]].get("count", 0)
                        if "indices" in primitive:
                            index_count = gltf["accessors"][primitive["indices"]].get("count", 0)
                        info.append({
                            "attrs": set(attrs.keys()),
                            "position_count": pos_count,
                            "index_count": index_count,
                        })
                _SOURCE_GLB_ATTRIBUTE_INFO_CACHE[cache_key] = info
                return info
    except Exception:
        return None
    return None


def source_glb_has_attribute(filepath, attribute_name=None, attribute_prefix=None):
    info = source_glb_attribute_info(filepath)
    if info is None:
        return True
    for primitive in info:
        attrs = primitive["attrs"]
        if attribute_name and attribute_name in attrs:
            return True
        if attribute_prefix and any(k.startswith(attribute_prefix) for k in attrs):
            return True
    return False


def source_glb_has_normals(filepath):
    return source_glb_has_attribute(filepath, attribute_name="NORMAL")


def source_glb_has_texcoords(filepath):
    return source_glb_has_attribute(filepath, attribute_prefix="TEXCOORD_")


def should_export_normals(settings, model):
    mode = getattr(settings, "normal_export_mode", "AUTO")
    if mode == 'OFF':
        return False
    if mode == 'ON':
        return True
    source_path = getattr(model, "path", "")
    info = source_glb_attribute_info(source_path)
    if info is None:
        return True
    has_normals = False
    for primitive in info:
        attrs = primitive["attrs"]
        if "NORMAL" not in attrs:
            continue
        has_normals = True
        has_texcoord = any(k.startswith("TEXCOORD_") for k in attrs)
        pos_count = primitive["position_count"]
        index_count = primitive["index_count"]
        # Blender-split manifold files have normals but no UV, and nearly one
        # POSITION record per index. Treat those normals as generated pollution.
        if (
            not has_texcoord
            and pos_count
            and index_count
            and pos_count / index_count > 0.75
        ):
            return False
    return has_normals


def should_export_texcoords(settings, model):
    mode = getattr(settings, "texcoord_export_mode", "AUTO")
    if mode == 'OFF':
        return False
    if mode == 'ON':
        return True
    return source_glb_has_texcoords(getattr(model, "path", ""))


def _nfc_filename(s):
    """与磁盘列表比对时使用 NFC，减少 macOS NFD / Windows 混用导致的「找不到文件」。"""
    if not s:
        return s
    return unicodedata.normalize("NFC", s)


def _short_model_hash(model):
    key = model.path or model.name
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]


def _is_hex6(s):
    return len(s) == 6 and all(c in "0123456789abcdef" for c in s.lower())


def _matches_export_stem(stem, model_name):
    """匹配新版分类前缀文件名，也兼容旧版 {model}_{数字}.glb。"""
    key = _nfc_filename(model_name)
    base = _nfc_filename(stem)
    if not key or not base:
        return False

    if base == key:
        return True
    old_prefix = key + "_"
    if base.startswith(old_prefix) and base[len(old_prefix):].isdigit():
        return True

    for prefix in EXPORT_FILE_PREFIXES.values():
        root = f"{prefix}__{key}"
        if base == root:
            return True
        delim = root + "__"
        if not base.startswith(delim):
            continue
        suffix = base[len(delim):]
        if _is_hex6(suffix):
            return True
        if suffix.startswith("part"):
            part = suffix[4:]
            if part.isdigit():
                return True
            if "__" in part:
                index, hash_suffix = part.split("__", 1)
                return index.isdigit() and _is_hex6(hash_suffix)
    return False


def list_model_glbs_in_dir(directory, model_name):
    """列出目录内属于该模型的 .glb，使用严格分隔符避免同前缀误删。"""
    if not directory or not os.path.isdir(directory):
        return []
    seen = set()
    for fn in os.listdir(directory):
        if not fn.lower().endswith(".glb"):
            continue
        path = os.path.join(directory, fn)
        if _matches_export_stem(fn[:-4], model_name):
            seen.add(path)
    return sorted(seen)


# 终态：磁盘上有独立输出目录的状态（与 mark 自动导出一致）
TERMINAL_EXPORT_STATUSES = FINAL_EXPORT_STATUSES


def purge_model_glbs_from_directory(directory, model_name, report_fn=None, keep_paths=None):
    """删除目录内属于该模型的所有 .glb，返回删除数量。"""
    keep = {os.path.realpath(p) for p in (keep_paths or []) if p}
    removed = 0
    for path in list_model_glbs_in_dir(directory, model_name):
        if os.path.realpath(path) in keep:
            continue
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            if report_fn:
                report_fn({'WARNING'}, f"无法删除: {path} ({e})")
    return removed


def purge_terminal_exports_for_model(context, model, report_fn=None, keep_paths=None):
    """从所有终态分类目录中移除该模型的旧 GLB；keep_paths 内的新输出会保留。"""
    settings = context.scene.meshy_settings
    base = output_base_directory(settings)
    if not base:
        return 0
    op_suffix = _meshy_operator_folder_suffix(settings)
    total = 0
    for st in TERMINAL_EXPORT_STATUSES:
        for d in iter_status_export_directories(base, st, op_suffix):
            total += purge_model_glbs_from_directory(d, model.name, report_fn, keep_paths=keep_paths)
    return total


def build_export_filename(status, model, group_index=None):
    prefix = EXPORT_FILE_PREFIXES.get(status)
    if not prefix:
        return ""
    if group_index is not None:
        return f"{prefix}__{model.name}__part{group_index:03d}.glb"
    return f"{prefix}__{model.name}.glb"


def unique_export_path(path, model):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    return f"{root}__{_short_model_hash(model)}{ext}"


def report_csv_path(context):
    settings = context.scene.meshy_settings
    base = output_base_directory(settings)
    if not base:
        return ""
    op_suffix = _meshy_operator_folder_suffix(settings)
    report_dir = os.path.join(base, f"Reports_{op_suffix}")
    os.makedirs(report_dir, exist_ok=True)
    safe_op = op_suffix.replace(os.sep, "_")
    return os.path.join(report_dir, f"logo_slot_report_{safe_op}.csv")


def audit_csv_path(context):
    settings = context.scene.meshy_settings
    base = output_base_directory(settings)
    if not base:
        return ""
    op_suffix = _meshy_operator_folder_suffix(settings)
    report_dir = os.path.join(base, f"Reports_{op_suffix}")
    os.makedirs(report_dir, exist_ok=True)
    safe_op = op_suffix.replace(os.sep, "_")
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(report_dir, f"logo_slot_missing_check_{safe_op}_{stamp}.csv")


def source_glb_files(source_directory):
    if not source_directory or not os.path.isdir(source_directory):
        return []
    result = []
    for filename in os.listdir(source_directory):
        if not filename.lower().endswith(".glb"):
            continue
        path = os.path.join(source_directory, filename)
        if os.path.isfile(path):
            result.append(path)
    return sorted(result, key=lambda p: _nfc_filename(os.path.basename(p)).lower())


def final_export_records(context):
    settings = context.scene.meshy_settings
    base = output_base_directory(settings)
    if not base:
        return []
    op_suffix = _meshy_operator_folder_suffix(settings)
    records = []
    for status in FINAL_EXPORT_STATUSES:
        for directory in iter_status_export_directories(base, status, op_suffix):
            if not os.path.isdir(directory):
                continue
            for filename in os.listdir(directory):
                if not filename.lower().endswith(".glb"):
                    continue
                path = os.path.join(directory, filename)
                if not os.path.isfile(path):
                    continue
                records.append({
                    "status": status,
                    "status_label": status_labels.get(status, status),
                    "subdir": export_subdir_for_path(base, path),
                    "filename": filename,
                    "path": path,
                    "stem": os.path.splitext(filename)[0],
                })
    return sorted(records, key=lambda r: (r["subdir"], _nfc_filename(r["filename"]).lower()))


def _find_model_item_for_source(context, source_path, model_name):
    source_real = os.path.realpath(source_path)
    for model in context.scene.meshy_models:
        try:
            if model.path and os.path.realpath(model.path) == source_real:
                return model
        except Exception:
            pass
    for model in context.scene.meshy_models:
        if model.name == model_name:
            return model
    return None


def _write_audit_text_block(summary_lines, issue_rows):
    text_name = "Logo Slot 缺漏核对"
    text = bpy.data.texts.get(text_name) or bpy.data.texts.new(text_name)
    text.clear()
    text.write("\n".join(summary_lines))
    text.write("\n\n")
    if issue_rows:
        text.write("问题列表:\n")
        for row in issue_rows[:200]:
            text.write(
                f"- {row['issue']}: {row['model_name']} | {row['note']} | {row['exported_files']}\n"
            )
        if len(issue_rows) > 200:
            text.write(f"... 仅显示前200条，完整列表见CSV，共 {len(issue_rows)} 条。\n")
    else:
        text.write("未发现缺漏、跨分类重复或无源导出文件。\n")


def upsert_report_csv(context, model, final_status, exported_paths):
    csv_status = CSV_STATUS_VALUES.get(final_status)
    if not csv_status:
        return
    path = report_csv_path(context)
    if not path:
        return

    fieldnames = [
        "operator",
        "model_name",
        "source_path",
        "final_status",
        "archive_tag",
        "reason",
        "exported_files",
        "updated_at",
    ]
    rows = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append({k: row.get(k, "") for k in fieldnames})
        except Exception:
            rows = []

    settings = context.scene.meshy_settings
    source_key = model.path or model.name
    exported_files = ";".join(os.path.basename(p) for p in exported_paths if p)
    updated_row = {
        "operator": settings.operator_name,
        "model_name": model.name,
        "source_path": source_key,
        "final_status": csv_status,
        "archive_tag": hardfix_tag_label(model) if final_status == 'HARD' else "",
        "reason": hardfix_report_reason(model) if final_status == 'HARD' else "",
        "exported_files": exported_files,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    replaced = False
    for i, row in enumerate(rows):
        if row.get("source_path") == source_key:
            rows[i] = updated_row
            replaced = True
            break
    if not replaced:
        rows.append(updated_row)

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def purge_completed_exports_for_model(context, model, report_fn=None):
    """从 Completed 目录移除该模型 GLB（终态导出路径前清理陈旧副本，不用于「从 Completed 移动」的主路径）。"""
    settings = context.scene.meshy_settings
    base = output_base_directory(settings)
    if not base:
        return 0
    op_suffix = _meshy_operator_folder_suffix(settings)
    completed_dir = os.path.join(base, f"Completed_{op_suffix}")
    return purge_model_glbs_from_directory(completed_dir, model.name, report_fn)


def move_completed_to_category(context, model, new_status, report_fn):
    """
    将 Completed_{op} 下本模型的 GLB 移动到 new_status 对应分类目录。
    report_fn: lambda level, msg -> None（如 self.report）
    返回 (success: bool, last_path: str)
    """
    settings = context.scene.meshy_settings
    op_suffix = _meshy_operator_folder_suffix(settings)
    base = output_base_directory(settings)
    if not base:
        report_fn({'ERROR'}, "未设置输出目录且无法从源目录推断")
        return False, ""

    completed_dir = os.path.join(base, f"Completed_{op_suffix}")
    subdir = status_subdir_for_export(new_status, op_suffix)
    if not subdir:
        report_fn({'ERROR'}, f"状态 {new_status} 无对应输出目录")
        return False, ""

    target_dir = status_export_directory(base, new_status, op_suffix, model)
    sources = list_model_glbs_in_dir(completed_dir, model.name)
    if not sources:
        # LEP 兜底：仅当 last_export_path 指向 Completed 内、且文件名严格匹配
        # ({model}.glb 或 {model}_{digits}.glb) 时，把该单文件作为源。不再按
        # 「以 {model}_ 开头」的宽松规则重扫目录，避免吞掉同前缀的其他模型文件。
        lep = settings.last_export_path
        key = _nfc_filename(model.name)
        if lep and key and os.path.isfile(lep):
            lep_dir = os.path.realpath(os.path.dirname(lep))
            if lep_dir == os.path.realpath(completed_dir):
                bn = os.path.basename(lep)
                if bn.lower().endswith(".glb"):
                    lep_base = _nfc_filename(bn[:-4])
                    prefix = key + "_"
                    if lep_base == key or (
                        lep_base.startswith(prefix)
                        and lep_base[len(prefix):].isdigit()
                    ):
                        sources = [lep]
    if not sources:
        report_fn({'ERROR'}, f"在 Completed 目录中未找到模型「{model.name}」的 GLB，请先导出到 Completed")
        return False, ""

    os.makedirs(target_dir, exist_ok=True)
    last_path = ""
    for src in sources:
        dest = os.path.join(target_dir, os.path.basename(src))
        try:
            if os.path.exists(dest):
                os.remove(dest)
            shutil.move(src, dest)
            last_path = dest
        except OSError as e:
            report_fn({'ERROR'}, f"移动文件失败: {src} -> {dest}: {e}")
            return False, ""

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    hist = f"{ts}: {settings.operator_name} 从 Completed 移至 {subdir}: {len(sources)} 个文件\n"
    if model.export_history:
        model.export_history = hist + model.export_history
    else:
        model.export_history = hist

    settings.last_export_path = last_path
    model.last_exported_status = new_status
    if settings.auto_save_progress:
        settings.save_progress(context)
    report_fn({'INFO'}, f"已将 {len(sources)} 个 GLB 移至 {target_dir}")
    return True, last_path


def navigation_blocked_while_completed(context):
    """可修复(COMPLETED) 期间禁止上一项，须先导出为 Fixed 再返回。"""
    models = getattr(context.scene, "meshy_models", None)
    settings = getattr(context.scene, "meshy_settings", None)
    if not models or not settings or len(models) == 0:
        return False
    idx = settings.current_model_index
    if idx < 0 or idx >= len(models):
        return False
    return models[idx].status == 'COMPLETED'


# 设置操作者姓名
class MESHY_OT_SetOperatorName(Operator):
    bl_idname = "meshy.set_operator_name"
    bl_label = "设置操作者姓名"
    bl_description = "设置当前操作者的姓名，用于标记处理的模型"
    
    operator_name: StringProperty(name="姓名")
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        settings.operator_name = self.operator_name
        settings.save_runtime_state()
        self.report({'INFO'}, f"操作者姓名已设置为: {self.operator_name}")
        
        # 尝试加载之前的进度
        if settings.auto_save_progress and settings.source_directory:
            if settings.load_progress(context):
                if context.scene.meshy_models:
                    bpy.ops.meshy.import_model()
                self.report({'INFO'}, f"已加载 {settings.operator_name} 的处理进度")
        
        return {'FINISHED'}
    
    def invoke(self, context, event):
        settings = context.scene.meshy_settings
        self.operator_name = settings.operator_name
        return context.window_manager.invoke_props_dialog(self)

# 设置源目录
class MESHY_OT_SetSourceDirectory(Operator):
    bl_idname = "meshy.set_source_directory"
    bl_label = "设置源目录"
    bl_description = "设置包含GLB模型的源目录"
    
    directory: StringProperty(subtype='DIR_PATH')
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        source_dir = self.directory
        settings.source_directory = source_dir
        settings.current_model_index = 0
        settings.last_export_path = ""
        settings.last_path_warning = ""
        settings.resolved_output_directory = ""
        settings.resolved_progress_directory = ""
        settings.output_directory = os.path.normpath(source_dir) if source_dir else ""
        context.scene.meshy_models.clear()
        
        # 检测源目录中是否已有进度文件，并尝试获取用户名
        existing_operator = settings.detect_existing_progress()
        loaded_progress = False
        
        # 如果存在进度文件且包含用户名，直接设置用户名
        if existing_operator:
            # 设置用户名（无论是否已有用户名都覆盖）
            settings.operator_name = existing_operator
            self.report({'INFO'}, f"已自动设置用户名: {existing_operator}")
            
            # 自动加载进度
            if settings.auto_save_progress and settings.load_progress(context):
                if context.scene.meshy_models:
                    bpy.ops.meshy.import_model()
                self.report({'INFO'}, f"已加载 {settings.operator_name} 在 {os.path.basename(self.directory)} 的处理进度")
                loaded_progress = True
        
        # 更新模型列表
        if not loaded_progress:
            bpy.ops.meshy.refresh_model_list()

        settings.save_runtime_state()
        
        self.report({'INFO'}, f"源目录已设置为: {self.directory}")
        return {'FINISHED'}
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

# 刷新模型列表
class MESHY_OT_RefreshModelList(Operator):
    bl_idname = "meshy.refresh_model_list"
    bl_label = "刷新模型列表"
    bl_description = "从源目录刷新可用的模型列表"
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        if not settings.source_directory or not os.path.exists(settings.source_directory):
            self.report({'ERROR'}, "源目录无效，请先设置有效的源目录")
            return {'CANCELLED'}

        cached_by_path = {}
        cached_by_name = {}
        for model in context.scene.meshy_models:
            cached = {
                "status": model.status,
                "export_history": model.export_history,
                "last_exported_status": model.last_exported_status,
                "hardfix_tag": model.hardfix_tag,
                "hardfix_reason": model.hardfix_reason,
            }
            if model.path:
                cached_by_path[os.path.realpath(model.path)] = cached
            if model.name:
                cached_by_name[model.name] = cached
        
        # 清空当前模型列表
        context.scene.meshy_models.clear()
        
        # 查找所有GLB文件
        glb_files = sorted(
            glob.glob(os.path.join(settings.source_directory, "*.glb")),
            key=lambda path: _nfc_filename(os.path.basename(path)).lower(),
        )
        
        if not glb_files:
            self.report({'WARNING'}, "源目录中未找到GLB模型")
            return {'CANCELLED'}
        
        # 添加模型到列表
        for glb_path in glb_files:
            filename = os.path.basename(glb_path)
            model_name = os.path.splitext(filename)[0]
            
            # 创建新模型项
            model_item = context.scene.meshy_models.add()
            model_item.name = model_name
            model_item.path = glb_path
            cached = cached_by_path.get(os.path.realpath(glb_path)) or cached_by_name.get(model_name)
            if cached:
                model_item.status = cached["status"]
                model_item.export_history = cached["export_history"]
                model_item.last_exported_status = cached["last_exported_status"]
                model_item.hardfix_tag = cached["hardfix_tag"]
                model_item.hardfix_reason = cached["hardfix_reason"]
            else:
                model_item.status = 'UNMARKED'
        
        # 确保当前索引在有效范围内
        model_count = len(context.scene.meshy_models)
        if model_count > 0:
            if settings.current_model_index >= model_count:
                settings.current_model_index = model_count - 1
                
            # 自动导入第一个模型
            bpy.ops.meshy.import_model()
        
        self.report({'INFO'}, f"已找到 {len(context.scene.meshy_models)} 个模型")
        return {'FINISHED'}


def _meshy_purge_orphans_safe():
    try:
        bpy.ops.outliner.orphans_purge(
            do_local_ids=True, do_linked_ids=True, do_recursive=True
        )
    except TypeError:
        try:
            bpy.ops.outliner.orphans_purge()
        except Exception:
            pass


def _meshy_clear_scene_before_import(context):
    """导入前清空场景：按数据块删除对象，不依赖全选（避免漏删不可选、glTF_not_exported 内等残留）。"""
    scene = context.scene
    _clear_scene_source(context)
    for obj in list(scene.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    def remove_empty_child_collections(parent):
        for child in list(parent.children):
            remove_empty_child_collections(child)
            if len(child.objects) == 0 and len(child.children) == 0:
                parent.children.unlink(child)
                bpy.data.collections.remove(child)

    remove_empty_child_collections(scene.collection)
    _meshy_purge_orphans_safe()


# 导入模型
class MESHY_OT_ImportModel(Operator):
    bl_idname = "meshy.import_model"
    bl_label = "导入模型"
    bl_description = "导入当前选中的模型到场景中"
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        models = context.scene.meshy_models
        
        if not models or len(models) == 0:
            self.report({'ERROR'}, "模型列表为空，请先设置源目录并刷新模型列表")
            return {'CANCELLED'}
        
        if settings.current_model_index >= len(models):
            self.report({'ERROR'}, "无效的模型索引")
            return {'CANCELLED'}
        
        # 获取当前模型
        current_model = models[settings.current_model_index]
        
        _meshy_clear_scene_before_import(context)
        
        # 导入GLB模型
        try:
            # 根据路径判断导入函数
            if current_model.path.lower().endswith('.glb'):
                bpy.ops.import_scene.gltf(
                    filepath=current_model.path,
                    import_pack_images=False,
                    guess_original_bind_pose=False,
                )
            else:
                self.report({'ERROR'}, f"不支持的文件格式: {current_model.path}")
                return {'CANCELLED'}
            
            # 导入后更新视图，确保模型可见
            bpy.ops.view3d.view_all(center=False)
            
            # 导入后清理可能的空骨骼
            armatures = [obj for obj in bpy.context.scene.objects if obj.type == 'ARMATURE']
            for armature in armatures:
                # 如果骨骼没有子对象或没有骨骼数据，则删除
                if len(armature.children) == 0 or (armature.data and len(armature.data.bones) == 0):
                    bpy.data.objects.remove(armature, do_unlink=True)
                    self.report({'INFO'}, f"已清理空骨骼: {armature.name}")

            _mark_scene_source(context, current_model)
            self.report({'INFO'}, f"已导入模型: {current_model.name}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"导入模型时出错: {str(e)}")
            return {'CANCELLED'}

# 确保状态目录存在
def ensure_status_directories(context, base_dir):
    """确保状态目录存在"""
    settings = context.scene.meshy_settings
    op_suffix = _meshy_operator_folder_suffix(settings)
    keys = ('NO_ACTION', 'FIXED', 'HARD')
    status_dirs = {}
    for k in keys:
        sub = status_subdir_for_export(k, op_suffix)
        if sub:
            path = os.path.join(base_dir, sub)
            status_dirs[k] = path
            os.makedirs(path, exist_ok=True)
            if k == 'HARD':
                for folder in HARDFIX_TAG_FOLDERS.values():
                    os.makedirs(os.path.join(path, folder), exist_ok=True)
    return status_dirs

# 导出模型
class MESHY_OT_ExportModel(Operator):
    """
    导出模型
    
    在按顶级节点导出时，文件命名采用"模型名_索引号.glb"的方式，
    如果有多个顶级节点，会依次使用"_1", "_2"等数字后缀，而不是使用节点名称
    """
    bl_idname = "meshy.export_model"
    bl_label = "导出模型"
    bl_description = "按父级节点组织导出模型"
    
    export_mode: EnumProperty(
        name="导出模式",
        description="选择如何组织导出对象",
        items=[
            ('SELECTED', "仅选中对象", "只导出当前选中的对象"),
            ('TOP_LEVEL', "按组导出", "将顶级对象及其子对象作为一组导出"),
            ('ALL_GROUPS', "导出所有", "将所有包含网格的组合并为一个GLB导出"),
        ],
        default='TOP_LEVEL'
    )

    def _begin_batch_side_effects(self):
        self._defer_export_side_effects = True
        self._batch_export_paths = []
        self._batch_export_status = None
        self._batch_base_output_dir = None

    def _finish_batch_side_effects(self, context, model):
        settings = context.scene.meshy_settings
        paths = getattr(self, "_batch_export_paths", [])
        status = getattr(self, "_batch_export_status", None)

        if not paths or not status:
            return

        model.last_exported_status = status
        purge_terminal_exports_for_model(context, model, self.report, keep_paths=paths)
        upsert_report_csv(context, model, status, paths)

        if settings.auto_save_progress:
            settings.save_progress(context)

    def _clear_batch_side_effects(self):
        self._defer_export_side_effects = False
        self._batch_export_paths = []
        self._batch_export_status = None
        self._batch_base_output_dir = None
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        models = context.scene.meshy_models
        
        if not models or len(models) == 0 or settings.current_model_index >= len(models):
            self.report({'ERROR'}, "无效的模型索引")
            return {'CANCELLED'}
        
        current_model = models[settings.current_model_index]
        
        if current_model.status == 'UNMARKED':
            self.report({'WARNING'}, "请先标记模型状态")
            return {'CANCELLED'}
        
        # 检查是否有对象
        if not context.scene.objects:
            self.report({'ERROR'}, "场景中没有对象")
            return {'CANCELLED'}
        if not scene_matches_current_model(context, current_model):
            self.report({'ERROR'}, "当前场景不是进度中选中的模型，请先重新导入当前模型后再导出")
            return {'CANCELLED'}

        if not commit_pending_mesh_edits(context, self.report):
            return {'CANCELLED'}
        
        # 保存原始选择状态以便稍后恢复
        original_selection = context.selected_objects[:]
        original_active = context.view_layer.objects.active
        
        try:
            if self.export_mode == 'SELECTED':
                # 检查是否有选中的对象
                if not context.selected_objects:
                    self.report({'ERROR'}, "没有选中任何对象")
                    return {'CANCELLED'}
                
                # 导出选中的对象
                if self.export_objects(context, current_model, context.selected_objects) == {'FINISHED'}:
                    return {'FINISHED'}
                else:
                    return {'CANCELLED'}
                    
            elif self.export_mode == 'TOP_LEVEL':
                # 获取所有顶级对象（没有父对象的对象）
                top_level_objects = [obj for obj in context.scene.objects if not obj.parent]
                
                if not top_level_objects:
                    self.report({'ERROR'}, "场景中没有顶级对象")
                    return {'CANCELLED'}

                if current_model.status in {'COMPLETED', 'FIXED'}:
                    all_exportable_meshes = []
                    seen_meshes = set()
                    for top_obj in top_level_objects:
                        objects_list = [top_obj]
                        self.get_all_children(top_obj, objects_list)
                        exportable_objects, _skipped = prepare_objects_for_export(context, objects_list)
                        for obj in exportable_objects:
                            if obj.type == 'MESH' and obj not in seen_meshes:
                                all_exportable_meshes.append(obj)
                                seen_meshes.add(obj)
                    duplicate_groups = overlapping_duplicate_mesh_groups(all_exportable_meshes)
                    if duplicate_groups:
                        detail = format_duplicate_mesh_groups(duplicate_groups)
                        self.report({'ERROR'}, f"检测到重叠重复 Mesh，已阻止按组 Fixed 导出: {detail}")
                        return {'CANCELLED'}
                
                # 按顶级节点进行组织导出
                successful_exports = 0
                failed_exports = 0
                # 添加连续计数器
                continuous_index = 1
                original_model_status = current_model.status
                original_last_exported_status = current_model.last_exported_status
                original_last_export_path = settings.last_export_path
                original_export_history = current_model.export_history
                self._begin_batch_side_effects()
                try:
                    for i, top_obj in enumerate(top_level_objects):
                        # 收集这个顶级对象及其所有子对象
                        objects_list = [top_obj]
                        self.get_all_children(top_obj, objects_list)
                        
                        # 检查这组对象中是否至少有一个最终会写入 GLB 的网格对象
                        exportable_objects, _skipped = prepare_objects_for_export(context, objects_list)
                        has_mesh = any(obj.type == 'MESH' for obj in exportable_objects)
                        
                        if has_mesh:
                            # 使用连续索引代替原始索引
                            if self.export_objects(context, current_model, objects_list, group_index=continuous_index, group_name=top_obj.name) == {'FINISHED'}:
                                successful_exports += 1
                                # 成功导出后递增连续索引
                                continuous_index += 1
                            else:
                                failed_exports += 1
                                break
                    
                    if successful_exports > 0 and failed_exports == 0:
                        self._finish_batch_side_effects(context, current_model)
                    elif failed_exports > 0:
                        for path in getattr(self, "_batch_export_paths", []):
                            try:
                                if path and os.path.exists(path):
                                    os.remove(path)
                            except OSError:
                                pass
                        current_model.status = original_model_status
                        current_model.last_exported_status = original_last_exported_status
                        settings.last_export_path = original_last_export_path
                        current_model.export_history = original_export_history
                finally:
                    self._clear_batch_side_effects()
                
                if failed_exports > 0:
                    self.report({'ERROR'}, f"按组导出失败: 已回滚本次 {successful_exports} 个成功组")
                    return {'CANCELLED'}
                if successful_exports > 0:
                    self.report({'INFO'}, f"已成功导出 {successful_exports} 个组")
                    
                    return {'FINISHED'}
                else:
                    self.report({'WARNING'}, "没有成功导出任何组")
                    return {'CANCELLED'}
            
            elif self.export_mode == 'ALL_GROUPS':
                # 获取所有顶级对象（没有父对象的对象）
                top_level_objects = [obj for obj in context.scene.objects if not obj.parent]
                
                if not top_level_objects:
                    self.report({'ERROR'}, "场景中没有顶级对象")
                    return {'CANCELLED'}
                
                # 收集所有包含网格的组
                valid_groups = []
                valid_objects = []
                seen_objects = set()
                
                for top_obj in top_level_objects:
                    # 收集这个顶级对象及其所有子对象
                    objects_list = [top_obj]
                    self.get_all_children(top_obj, objects_list)
                    
                    # 检查这组对象中是否至少有一个最终会写入 GLB 的网格对象
                    exportable_objects, _skipped = prepare_objects_for_export(context, objects_list)
                    has_mesh = any(obj.type == 'MESH' for obj in exportable_objects)
                    
                    if has_mesh:
                        valid_groups.append(top_obj.name)
                        for obj in objects_list:
                            if obj not in seen_objects:
                                valid_objects.append(obj)
                                seen_objects.add(obj)
                
                if not valid_groups:
                    self.report({'WARNING'}, "没有找到包含网格的组")
                    return {'CANCELLED'}
                
                # 导出所有有效对象为一个文件
                if self.export_objects(context, current_model, valid_objects, group_name="All_Groups") == {'FINISHED'}:
                    self.report({'INFO'}, f"已将所有组 ({len(valid_groups)} 个) 导出为一个GLB文件")
                    
                    return {'FINISHED'}
                else:
                    return {'CANCELLED'}
                
            return {'CANCELLED'}
            
        finally:
            # 恢复原始选择状态
            deselect_all_objects(context)
            for obj in original_selection:
                try:
                    # 只有当对象有效且在当前场景中时才选中它
                    if obj and obj.name and obj.name in context.view_layer.objects:
                        obj.select_set(True)
                except ReferenceError:
                    # 对象已被删除，忽略它
                    continue
            
            # 如果原活动对象有效，设置它为活动对象
            try:
                if original_active and original_active.name and original_active.name in context.view_layer.objects:
                    context.view_layer.objects.active = original_active
            except ReferenceError:
                # 原活动对象已被删除，忽略它
                pass
    
    def export_objects(self, context, model, objects, group_index=None, group_name=None):
        settings = context.scene.meshy_settings
        
        # 保存原始游标位置，以便稍后恢复
        original_cursor_location = context.scene.cursor.location.copy()
        
        status = 'FIXED' if model.status == 'COMPLETED' else model.status
        op_suffix = _meshy_operator_folder_suffix(settings)
        subdir = status_subdir_for_export(status, op_suffix)
        if not subdir:
            self.report({'WARNING'}, "当前状态无法导出 GLB")
            context.scene.cursor.location = original_cursor_location
            return {'CANCELLED'}

        if getattr(self, "_defer_export_side_effects", False) and getattr(self, "_batch_base_output_dir", None):
            base_output_dir = self._batch_base_output_dir
        else:
            base_output_dir = output_base_directory(settings)
            if getattr(self, "_defer_export_side_effects", False) and base_output_dir:
                self._batch_base_output_dir = base_output_dir
        if not base_output_dir:
            self.report({'ERROR'}, settings.last_path_warning or "输出目录不可写")
            context.scene.cursor.location = original_cursor_location
            return {'CANCELLED'}
        if settings.last_path_warning:
            self.report({'WARNING'}, settings.last_path_warning)

        current_status_dir = status_export_directory(base_output_dir, status, op_suffix, model)
        os.makedirs(current_status_dir, exist_ok=True)
        
        try:
            export_filename = build_export_filename(status, model, group_index)
            if not export_filename:
                self.report({'WARNING'}, "当前状态无法生成导出文件名")
                return {'CANCELLED'}
            final_export_path = os.path.join(current_status_dir, export_filename)
            if status in FINAL_EXPORT_STATUSES:
                root, ext = os.path.splitext(final_export_path)
                export_path = f"{root}__tmp_{os.getpid()}{ext}"
                temp_index = 1
                while os.path.exists(export_path):
                    export_path = f"{root}__tmp_{os.getpid()}_{temp_index}{ext}"
                    temp_index += 1
            else:
                export_path = unique_export_path(final_export_path, model)

            export_objects, skipped_objects = prepare_objects_for_export(context, objects)
            if skipped_objects:
                self.report(
                    {'INFO'},
                    f"导出前已跳过 {len(skipped_objects)} 个隐藏或 LogoSlot 布尔辅助物体",
                )
            
            # 计算选择集的中心位置（只考虑网格对象）
            mesh_objects = [obj for obj in export_objects if obj.type == 'MESH']
            if not mesh_objects:
                self.report({'ERROR'}, "选择中没有网格对象")
                return {'CANCELLED'}
            if status == 'FIXED':
                duplicate_groups = overlapping_duplicate_mesh_groups(mesh_objects)
                if duplicate_groups:
                    detail = format_duplicate_mesh_groups(duplicate_groups)
                    self.report({'ERROR'}, f"检测到重叠重复 Mesh，已阻止 Fixed 导出: {detail}")
                    return {'CANCELLED'}

            transform_objects = transform_objects_for_boolean_export(context, export_objects)
            # 保存原始位置；布尔 operand 也要一起恢复，避免导出居中影响修复关系。
            original_locations = {obj: obj.location.copy() for obj in transform_objects}

            export_normals = should_export_normals(settings, model)
            if not export_normals:
                self.report({'INFO'}, "导出 normals: 关闭（保持 manifold 拓扑连通）")
            export_texcoords = should_export_texcoords(settings, model)
            if not export_texcoords:
                self.report({'INFO'}, "导出 UV: 关闭（源文件无 UV，避免布尔辅助物体污染）")
            
            # 计算中心点
            center = Vector((0, 0, 0))
            if mesh_objects:
                center = sum([obj.location for obj in mesh_objects], Vector()) / len(mesh_objects)
            
            # 找出顶级对象（没有父级或者父级不在选择集中的对象）
            objects_set = set(transform_objects)
            top_level_in_selection = [
                obj for obj in transform_objects
                if not obj.parent or obj.parent not in objects_set
            ]
            
            # 只移动顶级对象的位置，子对象会自动跟随
            for obj in top_level_in_selection:
                obj.location = obj.location - center
            
            # 更新场景
            bpy.context.view_layer.update()
            
            # 取消选择所有对象
            deselect_all_objects(context)
            
            # 选择要导出的对象
            for obj in export_objects:
                obj.select_set(True)
            active_object = ensure_active_object(context, export_objects)
            if not active_object:
                self.report({'ERROR'}, "没有可用的活动对象，无法导出")
                return {'CANCELLED'}
            
            try:
                # 导出为GLB
                result = export_gltf_with_safe_context(export_path, active_object, export_normals, export_texcoords)
                if result != {'FINISHED'}:
                    if export_path != final_export_path and os.path.exists(export_path):
                        os.remove(export_path)
                    self.report({'ERROR'}, f"导出器返回失败状态: {result}")
                    return {'CANCELLED'}
                valid_glb, invalid_reason = validate_glb_file(export_path)
                if not valid_glb:
                    if os.path.exists(export_path):
                        os.remove(export_path)
                    self.report({'ERROR'}, f"导出 GLB 不完整，已丢弃临时文件: {invalid_reason}")
                    return {'CANCELLED'}
            finally:
                # 无论导出成功、失败或抛异常，都必须恢复场景对象位置。
                for obj, loc in original_locations.items():
                    try:
                        if obj and obj.name in context.view_layer.objects:
                            obj.location = loc
                    except ReferenceError:
                        # 对象已被删除，忽略它
                        continue
                    
                # 更新场景
                bpy.context.view_layer.update()
                
                # 取消选择
                for obj in export_objects:
                    try:
                        if obj and obj.name in context.view_layer.objects:
                            obj.select_set(False)
                    except ReferenceError:
                        # 对象已被删除，忽略它
                        continue

            if export_path != final_export_path:
                os.replace(export_path, final_export_path)
                export_path = final_export_path
                valid_glb, invalid_reason = validate_glb_file(export_path)
                if not valid_glb:
                    try:
                        os.remove(export_path)
                    except OSError:
                        pass
                    self.report({'ERROR'}, f"导出 GLB 落盘后校验失败: {invalid_reason}")
                    return {'CANCELLED'}
                if not getattr(self, "_defer_export_side_effects", False):
                    purge_terminal_exports_for_model(
                        context,
                        model,
                        self.report,
                        keep_paths=[export_path],
                    )

            # 记录结果
            settings.last_export_path = export_path
            model.status = status
            
            # 更新导出历史
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            history_entry = f"{timestamp}: {settings.operator_name} "
            
            if group_name:
                history_entry += f"以'{group_name}'组导出到 {os.path.basename(export_path)}\n"
            else:
                history_entry += f"导出 {len(export_objects)} 个对象到 {os.path.basename(export_path)}\n"
            
            if model.export_history:
                model.export_history = history_entry + model.export_history
            else:
                model.export_history = history_entry
            
            # 更新模型的last_exported_status
            model.last_exported_status = status
            if getattr(self, "_defer_export_side_effects", False):
                self._batch_export_paths.append(export_path)
                self._batch_export_status = status
            else:
                upsert_report_csv(
                    context,
                    model,
                    status,
                    list_model_glbs_in_dir(current_status_dir, model.name),
                )
            
            # 保存进度
            if settings.auto_save_progress and not getattr(self, "_defer_export_side_effects", False):
                settings.save_progress(context)
            
            # 输出日志
            if group_name:
                self.report({'INFO'}, f"已导出组 '{group_name}' 到 {export_path}")
            else:
                self.report({'INFO'}, f"已导出 {len(export_objects)} 个对象到 {export_path}")
            
            return {'FINISHED'}
        
        except Exception as e:
            if (
                'export_path' in locals()
                and 'final_export_path' in locals()
                and export_path != final_export_path
                and os.path.exists(export_path)
            ):
                try:
                    os.remove(export_path)
                except OSError:
                    pass
            self.report({'ERROR'}, f"导出失败: {str(e)}")
            return {'CANCELLED'}
        
        finally:
            # 导出完成后恢复原始游标位置
            context.scene.cursor.location = original_cursor_location
    
    def invoke(self, context, event):
        # 直接执行，不再弹出选择对话框
        return self.execute(context)
        
    def _export_glb(self, filepath, objects):
        """导出选定对象为GLB格式，避免创建额外骨骼数据"""
        # 取消选择所有对象
        bpy.ops.object.select_all(action='DESELECT')
        
        # 选择要导出的对象
        for obj in objects:
            obj.select_set(True)
        
        # 保存原始位置
        original_locations = {}
        for obj in objects:
            original_locations[obj.name] = obj.location.copy()
        
        try:
            # 计算中心点
            from mathutils import Vector
            center = Vector((0, 0, 0))
            if objects:
                center = sum([obj.location for obj in objects], Vector()) / len(objects)
            
            # 调整位置到原点(不改变层级关系)
            for obj in objects:
                obj.location = obj.location - center
            
            # 更新场景
            bpy.context.view_layer.update()
            
            # 导出为GLB（不使用export_extras以减少额外数据）
            bpy.ops.export_scene.gltf(
                filepath=filepath,
                export_format='GLB',
                use_selection=True,
                export_extras=False,
                export_animations=False  # 避免导出动画数据
            )
        
        finally:
            # 恢复原始位置
            for obj in objects:
                if obj.name in original_locations:
                    obj.location = original_locations[obj.name]
            
            # 更新场景
            bpy.context.view_layer.update()
            
            # 取消选择
            for obj in objects:
                obj.select_set(False)

    def get_all_children(self, obj, result_list):
        """递归获取所有子对象"""
        for child in obj.children:
            result_list.append(child)
            self.get_all_children(child, result_list)

# 标记状态
class MESHY_OT_MarkStatus(Operator):
    bl_idname = "meshy.mark_status"
    bl_label = "标记状态"
    bl_description = "标记当前模型的处理状态"
    
    status: EnumProperty(
        name="状态",
        items=[
            ('COMPLETED', "可修复", "需要人工修复，修完后导出为已修复"),
            ('FIXED', "已修复", "修复完成并导出"),
            ('NO_ACTION', "无需修复", "归类为 nologo"),
            ('HARD', "难以修复", "必须填写原因并归档"),
        ]
    )

    hardfix_tag: EnumProperty(
        name="归档分类",
        items=HARDFIX_TAG_ITEMS,
        default='HARD_TO_FIX'
    )
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        models = context.scene.meshy_models
        
        if not models or len(models) == 0 or settings.current_model_index >= len(models):
            self.report({'ERROR'}, "无效的模型索引")
            return {'CANCELLED'}
        
        current_model = models[settings.current_model_index]
        current_index = settings.current_model_index
        prev_status = current_model.status
        
        # 标为可修复只进入修复态；旧终态输出等重新成功导出后再清理，避免恢复/继续时先删成果。
        if self.status == 'COMPLETED':
            current_model.status = self.status
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            history_entry = f"{timestamp}: {settings.operator_name} 将状态标记为 \"{status_labels[self.status]}\"\n"
            if current_model.export_history:
                current_model.export_history = history_entry + current_model.export_history
            else:
                current_model.export_history = history_entry
            self.report({'INFO'}, f"模型状态已标记为: {status_labels[self.status]}")
            if settings.auto_save_progress:
                settings.save_progress(context, True)
            return {'FINISHED'}
        
        if self.status == 'HARD':
            current_model.hardfix_tag = self.hardfix_tag
            if current_model.hardfix_tag == 'OTHER' and not current_model.hardfix_reason.strip():
                self.report({'ERROR'}, "归档分类为「其他」时，请先填写理由")
                return {'CANCELLED'}

        # 终态：导出成功后再替换旧 GLB，避免导出失败时丢失已有结果。
        if self.status in TERMINAL_EXPORT_STATUSES:
            current_model.status = self.status
            if not context.scene.objects:
                self.report({'ERROR'}, "场景中没有对象，无法导出")
                current_model.status = prev_status
                return {'CANCELLED'}
            export_mode = 'TOP_LEVEL' if self.status == 'FIXED' else 'ALL_GROUPS'
            result = bpy.ops.meshy.export_model('EXEC_DEFAULT', export_mode=export_mode)
            if result != {'FINISHED'}:
                current_model.status = prev_status
                self.report({'ERROR'}, "自动导出失败，状态已恢复")
                return {'CANCELLED'}
            
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            status_detail = status_labels[self.status]
            if self.status == 'HARD':
                detail = hardfix_status_detail(current_model)
                if detail:
                    status_detail = f"{status_detail} / {detail}"
            history_entry = f"{timestamp}: {settings.operator_name} 将状态标记为 \"{status_detail}\"\n"
            if current_model.export_history:
                current_model.export_history = history_entry + current_model.export_history
            else:
                current_model.export_history = history_entry
            self.report({'INFO'}, f"模型状态已标记为: {status_labels[self.status]}")
            if settings.auto_save_progress:
                settings.save_progress(context, True)
            
            if current_index < len(models) - 1:
                settings.current_model_index = current_index + 1
                self.report({'INFO'}, "已自动切换到下一个模型")
                bpy.ops.meshy.import_model()
                bpy.ops.ed.flush_edits()
            return {'FINISHED'}
        
        self.report({'WARNING'}, "未处理的状态")
        return {'CANCELLED'}

# 导航到上一个模型
class MESHY_OT_PreviousModel(Operator):
    """切换到前一个模型"""
    bl_idname = "meshy.previous_model"
    bl_label = "上一个模型"
    bl_description = "切换到前一个模型"
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        models = context.scene.meshy_models
        
        if not models or len(models) == 0:
            self.report({'ERROR'}, "没有可用的模型")
            return {'CANCELLED'}
        
        current_index = settings.current_model_index

        if navigation_blocked_while_completed(context):
            self.report(
                {'WARNING'},
                "当前为「可修复」：请先完成修复并导出为「已修复」后再切换上一项",
            )
            return {'CANCELLED'}
            
        # 自动保存进度
        if settings.auto_save_progress:
            bpy.ops.meshy.save_progress()
        
        # 切换到上一个模型（未标记也可返回上一项）
        if current_index > 0:
            settings.current_model_index = current_index - 1
            settings.save_runtime_state()
            try:
                bpy.ops.meshy.import_model()
                # 清除撤销历史，防止用户撤销到上一个模型
                bpy.ops.ed.flush_edits()

                self.report({'INFO'}, f"当前模型: {models[settings.current_model_index].name}")
            except Exception as e:
                self.report({'ERROR'}, f"无法加载模型: {str(e)}")
                return {'CANCELLED'}
        else:
            self.report({'INFO'}, "已是第一个模型")
            
        return {'FINISHED'}

# 导航到下一个模型
class MESHY_OT_NextModel(Operator):
    bl_idname = "meshy.next_model"
    bl_label = "下一个模型"
    bl_description = "切换到下一个模型"
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        models = context.scene.meshy_models
        
        if not models or len(models) == 0:
            self.report({'ERROR'}, "没有可用的模型")
            return {'CANCELLED'}
        
        current_index = settings.current_model_index
        
        if current_index < len(models) and models[current_index].status == 'UNMARKED':
            self.report({'WARNING'}, "未标记时不能进入下一项，请先打标签或返回上一项")
            return {'CANCELLED'}

        # 自动保存进度
        if settings.auto_save_progress:
            bpy.ops.meshy.save_progress()
        
        # 切换到下一个模型
        if current_index < len(models) - 1:
            settings.current_model_index = current_index + 1
            settings.save_runtime_state()
            try:
                bpy.ops.meshy.import_model()
                # 清除撤销历史，防止用户撤销到上一个模型
                bpy.ops.ed.flush_edits()

                self.report({'INFO'}, f"当前模型: {models[settings.current_model_index].name}")
            except Exception as e:
                self.report({'ERROR'}, f"无法加载模型: {str(e)}")
                return {'CANCELLED'}
        else:
            self.report({'INFO'}, "已是最后一个模型")
            
        return {'FINISHED'}

# 快捷键处理器
class MESHY_OT_KeyHandler(Operator):
    bl_idname = "meshy.key_handler"
    bl_label = "快捷键处理器"
    bl_description = "处理插件的快捷键"
    
    key: StringProperty()
    
    def execute(self, context):
        # 处理不同的快捷键
        if self.key == 'NEXT':
            # 调用下一个模型操作符
            bpy.ops.meshy.next_model()
        elif self.key == 'PREVIOUS':
            # 调用上一个模型操作符
            bpy.ops.meshy.previous_model()
        elif self.key == 'EXPORT':
            # 调用导出模型操作符
            bpy.ops.meshy.export_model('INVOKE_DEFAULT')
        
        return {'FINISHED'}

# 创建父级节点
class MESHY_OT_CreateParentNode(Operator):
    bl_idname = "meshy.create_parent_node"
    bl_label = "创建父级节点"
    bl_description = "将选中的对象放入一个新的父级节点下"
    
    parent_name: StringProperty(
        name="组名",
        description="新父级节点的名称",
        default="Group"
    )
    
    def execute(self, context):
        # 获取选中的对象
        selected_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_objects:
            self.report({'ERROR'}, "请先选择至少一个网格对象")
            return {'CANCELLED'}
        
        # 计算选中对象的中心点
        from mathutils import Vector
        center = Vector((0, 0, 0))
        for obj in selected_objects:
            center += obj.location
        center /= len(selected_objects)
        
        # 创建一个空对象作为父级节点
        empty = bpy.data.objects.new(self.parent_name, None)
        empty.empty_display_type = 'PLAIN_AXES'
        empty.empty_display_size = 0.5
        empty.location = center
        
        # 将空对象添加到场景
        context.collection.objects.link(empty)
        
        # 保存原始选择和活动对象
        original_active = context.view_layer.objects.active
        
        try:
            # 将选中的对象设置为空对象的子级
            for obj in selected_objects:
                # 保存原始世界坐标
                original_matrix_world = obj.matrix_world.copy()
                
                # 设置父级关系
                obj.parent = empty
                
                # 恢复原始世界坐标
                obj.matrix_world = original_matrix_world
            
            # 取消所有选择
            bpy.ops.object.select_all(action='DESELECT')
            
            # 选择新创建的父级节点
            empty.select_set(True)
            context.view_layer.objects.active = empty
            
            self.report({'INFO'}, f"已创建父级节点'{self.parent_name}'，包含 {len(selected_objects)} 个子对象")
            return {'FINISHED'}
            
        except Exception as e:
            self.report({'ERROR'}, f"创建父级节点失败: {str(e)}")
            return {'CANCELLED'}

# 保存进度操作符
class MESHY_OT_SaveProgress(Operator):
    bl_idname = "meshy.save_progress"
    bl_label = "保存进度"
    bl_description = "保存当前处理进度"
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        
        if not settings.source_directory:
            self.report({'ERROR'}, "请先设置源目录")
            return {'CANCELLED'}
        
        if not settings.operator_name:
            self.report({'ERROR'}, "请先设置操作者姓名")
            return {'CANCELLED'}
        
        if settings.save_progress(context):
            self.report({'INFO'}, f"进度已保存 ({settings.last_save_time})")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "保存进度失败")
            return {'CANCELLED'}

# 加载进度操作符
class MESHY_OT_LoadProgress(Operator):
    bl_idname = "meshy.load_progress"
    bl_label = "加载进度"
    bl_description = "加载之前保存的处理进度"
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        
        if not settings.source_directory:
            self.report({'ERROR'}, "请先设置源目录")
            return {'CANCELLED'}
        
        if settings.load_progress(context):
            if context.scene.meshy_models:
                bpy.ops.meshy.import_model()
            self.report({'INFO'}, f"进度已加载 (保存于 {settings.last_save_time})")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "没有找到保存的进度")
            return {'CANCELLED'}

# 清除进度操作符
class MESHY_OT_ClearProgress(Operator):
    bl_idname = "meshy.clear_progress"
    bl_label = "清除进度"
    bl_description = "清除保存的处理进度"
    
    def execute(self, context):
        settings = context.scene.meshy_settings
        
        if not settings.source_directory:
            self.report({'ERROR'}, "请先设置源目录")
            return {'CANCELLED'}
        
        if settings.clear_progress(context):
            self.report({'INFO'}, "进度已清除")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "没有找到保存的进度")
            return {'CANCELLED'}


class MESHY_OT_CheckExportCompleteness(Operator):
    bl_idname = "meshy.check_export_completeness"
    bl_label = "核对导出缺漏"
    bl_description = "对比源目录和最终导出目录，生成缺漏、重复和无源导出的CSV列表"

    def execute(self, context):
        settings = context.scene.meshy_settings

        if not settings.source_directory or not os.path.isdir(settings.source_directory):
            self.report({'ERROR'}, "请先设置有效的源目录")
            return {'CANCELLED'}
        if not settings.operator_name:
            self.report({'ERROR'}, "请先设置操作者姓名")
            return {'CANCELLED'}

        base = output_base_directory(settings)
        if not base:
            self.report({'ERROR'}, settings.last_path_warning or "输出目录不可写，无法生成核对CSV")
            return {'CANCELLED'}

        sources = source_glb_files(settings.source_directory)
        if not sources:
            self.report({'ERROR'}, "源目录中未找到GLB模型")
            return {'CANCELLED'}

        exports = final_export_records(context)
        matched_export_paths = set()
        issue_rows = []
        covered_count = 0
        duplicate_count = 0
        corrupt_count = 0
        corrupt_export_paths = set()

        for source_path in sources:
            model_name = os.path.splitext(os.path.basename(source_path))[0]
            model_item = _find_model_item_for_source(context, source_path, model_name)
            progress_status = ""
            hardfix_tag = ""
            hardfix_reason = ""
            if model_item:
                progress_status = status_labels.get(model_item.status, model_item.status)
                if model_item.status == 'HARD':
                    hardfix_tag = hardfix_tag_label(model_item)
                    hardfix_reason = hardfix_report_reason(model_item)

            matched = [
                record for record in exports
                if _matches_export_stem(record["stem"], model_name)
            ]
            for record in matched:
                real_export_path = os.path.realpath(record["path"])
                matched_export_paths.add(real_export_path)
                valid_glb, invalid_reason = validate_glb_file(record["path"])
                if not valid_glb and real_export_path not in corrupt_export_paths:
                    corrupt_export_paths.add(real_export_path)
                    corrupt_count += 1
                    issue_rows.append({
                        "issue": "CORRUPT_EXPORT",
                        "model_name": model_name,
                        "source_path": source_path,
                        "progress_status": progress_status,
                        "hardfix_tag": hardfix_tag,
                        "hardfix_reason": hardfix_reason,
                        "export_statuses": record["subdir"],
                        "exported_files": f"{record['subdir']}/{record['filename']}",
                        "note": f"导出 GLB 文件不完整或无法解析: {invalid_reason}",
                    })

            status_keys = sorted({record["status"] for record in matched})
            status_dirs = sorted({record["subdir"] for record in matched})
            exported_files = ";".join(
                f"{record['subdir']}/{record['filename']}" for record in matched
            )

            if not matched:
                issue_rows.append({
                    "issue": "MISSING_EXPORT",
                    "model_name": model_name,
                    "source_path": source_path,
                    "progress_status": progress_status,
                    "hardfix_tag": hardfix_tag,
                    "hardfix_reason": hardfix_reason,
                    "export_statuses": "",
                    "exported_files": "",
                    "note": "源目录有模型，但 NoLogo/Fixed/HardFix 最终目录未找到对应GLB",
                })
                continue

            covered_count += 1
            if len(status_keys) > 1:
                duplicate_count += 1
                issue_rows.append({
                    "issue": "DUPLICATE_STATUS",
                    "model_name": model_name,
                    "source_path": source_path,
                    "progress_status": progress_status,
                    "hardfix_tag": hardfix_tag,
                    "hardfix_reason": hardfix_reason,
                    "export_statuses": ";".join(status_dirs),
                    "exported_files": exported_files,
                    "note": "同一模型出现在多个最终分类目录，请人工确认保留哪一类",
                })

        orphan_count = 0
        for record in exports:
            if os.path.realpath(record["path"]) in matched_export_paths:
                continue
            orphan_count += 1
            issue_rows.append({
                "issue": "ORPHAN_EXPORT",
                "model_name": "",
                "source_path": "",
                "progress_status": "",
                "hardfix_tag": "",
                "hardfix_reason": "",
                "export_statuses": record["subdir"],
                "exported_files": f"{record['subdir']}/{record['filename']}",
                "note": "导出目录中存在GLB，但源目录未找到同名模型；可能是旧输出或源文件已改名/移走",
            })
            real_export_path = os.path.realpath(record["path"])
            valid_glb, invalid_reason = validate_glb_file(record["path"])
            if not valid_glb and real_export_path not in corrupt_export_paths:
                corrupt_export_paths.add(real_export_path)
                corrupt_count += 1
                issue_rows.append({
                    "issue": "CORRUPT_EXPORT",
                    "model_name": "",
                    "source_path": "",
                    "progress_status": "",
                    "hardfix_tag": "",
                    "hardfix_reason": "",
                    "export_statuses": record["subdir"],
                    "exported_files": f"{record['subdir']}/{record['filename']}",
                    "note": f"导出 GLB 文件不完整或无法解析: {invalid_reason}",
                })

        missing_count = sum(1 for row in issue_rows if row["issue"] == "MISSING_EXPORT")

        path = audit_csv_path(context)
        if not path:
            self.report({'ERROR'}, "无法创建核对CSV路径")
            return {'CANCELLED'}

        fieldnames = [
            "issue",
            "model_name",
            "source_path",
            "progress_status",
            "hardfix_tag",
            "hardfix_reason",
            "export_statuses",
            "exported_files",
            "note",
        ]
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                if issue_rows:
                    writer.writerows(issue_rows)
                else:
                    writer.writerow({
                        "issue": "OK",
                        "note": "源目录和最终导出目录核对通过，未发现缺漏、跨分类重复或无源导出文件",
                    })
        except OSError as e:
            self.report({'ERROR'}, f"写入核对CSV失败: {e}")
            return {'CANCELLED'}

        settings.last_audit_path = path
        summary_lines = [
            "Logo / Slot 导出缺漏核对",
            f"源目录: {settings.source_directory}",
            f"输出根目录: {base}",
            f"源模型数: {len(sources)}",
            f"已覆盖源模型数: {covered_count}",
            f"缺漏模型数: {missing_count}",
            f"跨分类重复模型数: {duplicate_count}",
            f"无源导出文件数: {orphan_count}",
            f"损坏/截断导出文件数: {corrupt_count}",
            f"CSV: {path}",
        ]
        _write_audit_text_block(summary_lines, issue_rows)

        self.report(
            {'INFO'},
            f"核对完成: 源{len(sources)} 已覆盖{covered_count} 缺漏{missing_count} 重复{duplicate_count} 无源导出{orphan_count} 损坏{corrupt_count}",
        )
        return {'FINISHED'}


# 去除Alpha连接操作符
class MESHY_OT_RemoveAlpha(Operator):
    bl_idname = "meshy.remove_alpha"
    bl_label = "去除Alpha"
    bl_description = "断开所有材质中连接到Alpha的节点，使模型完全不透明"
    
    def execute(self, context):
        # 用于记录操作信息
        mesh_count = 0
        material_count = 0
        connection_count = 0
        
        # 遍历所有网格对象
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
                
            mesh_count += 1
            
            # 遍历对象的所有材质槽
            for mat_slot in obj.material_slots:
                if not mat_slot.material:
                    continue
                    
                material = mat_slot.material
                material_count += 1
                
                # 检查材质是否使用节点
                if not material.use_nodes or not material.node_tree:
                    continue
                
                # 1. 检查原理化BSDF节点 (最常见的透明度设置位置)
                principled_nodes = [n for n in material.node_tree.nodes 
                                 if n.type == 'BSDF_PRINCIPLED']
                
                for principled_node in principled_nodes:
                    if 'Alpha' in principled_node.inputs:
                        alpha_input = principled_node.inputs['Alpha']
                        
                        # 检查Alpha输入是否有连接
                        if alpha_input.is_linked:
                            # 断开连接
                            for link in alpha_input.links:
                                connection_count += 1
                                
                                # 断开连接
                                material.node_tree.links.remove(link)
                                
                                # 设置Alpha值为1.0（完全不透明）
                                alpha_input.default_value = 1.0
                
                # 2. 检查输出节点的Alpha (部分自定义着色器会直接连接到输出)
                output_nodes = [n for n in material.node_tree.nodes 
                              if n.type == 'OUTPUT_MATERIAL']
                
                for output_node in output_nodes:
                    if 'Alpha' in output_node.inputs and output_node.inputs['Alpha'].is_linked:
                        alpha_input = output_node.inputs['Alpha']
                        
                        for link in alpha_input.links:
                            connection_count += 1
                            
                            # 断开连接
                            material.node_tree.links.remove(link)
                
                # 3. 检查混合着色器(Mix Shader)的Fac输入
                # 这通常用于透明度混合
                mix_nodes = [n for n in material.node_tree.nodes 
                           if n.type == 'MIX_SHADER']
                
                for mix_node in mix_nodes:
                    if 'Fac' in mix_node.inputs and mix_node.inputs['Fac'].is_linked:
                        fac_input = mix_node.inputs['Fac']
                        
                        for link in fac_input.links:
                            connection_count += 1
                            
                            # 断开连接
                            material.node_tree.links.remove(link)
                            
                            # 设置混合因子为0.0（使用第一个着色器）
                            fac_input.default_value = 0.0
        
        # 强制更新所有材质
        for mat in bpy.data.materials:
            if mat.use_nodes and mat.node_tree:
                mat.node_tree.update_tag()
        
        # 显示结果消息
        if connection_count > 0:
            self.report({'INFO'}, f"已断开 {connection_count} 个Alpha连接，处理了 {mesh_count} 个物体的 {material_count} 个材质")
        else:
            self.report({'INFO'}, "未找到需要断开的Alpha连接")
            
        return {'FINISHED'}

# 清理未使用数据块操作符
class MESHY_OT_PurgeUnusedData(Operator):
    bl_idname = "meshy.purge_unused_data"
    bl_label = "清理数据块"
    bl_description = "清理场景中所有未使用的数据块，释放内存"
    
    def execute(self, context):
        # 执行清理操作前的数据统计
        mesh_count_before = len(bpy.data.meshes)
        material_count_before = len(bpy.data.materials)
        texture_count_before = len(bpy.data.textures)
        image_count_before = len(bpy.data.images)
        
        # 强制清理所有未使用的数据块(orphans)
        result = bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        
        # 执行清理操作后的数据统计
        mesh_count_after = len(bpy.data.meshes)
        material_count_after = len(bpy.data.materials)
        texture_count_after = len(bpy.data.textures)
        image_count_after = len(bpy.data.images)
        
        # 计算清理的数据块数量
        mesh_removed = mesh_count_before - mesh_count_after
        material_removed = material_count_before - material_count_after
        texture_removed = texture_count_before - texture_count_after
        image_removed = image_count_before - image_count_after
        total_removed = mesh_removed + material_removed + texture_removed + image_removed
        
        # 显示清理结果
        if total_removed > 0:
            self.report({'INFO'}, f"成功清理 {total_removed} 个未使用的数据块 (网格:{mesh_removed}, 材质:{material_removed}, 纹理:{texture_removed}, 图像:{image_removed})")
        else:
            self.report({'INFO'}, "没有找到可清理的未使用数据块")
            
        return {'FINISHED'}

# 选中outline材质对象操作符
class MESHY_OT_SelectOutlineMeshes(Operator):
    bl_idname = "meshy.select_outline_meshes"
    bl_label = "选中描边物体"
    bl_description = "选中所有使用包含'outline'字样材质的网格对象"
    
    def execute(self, context):
        # 先取消选择所有对象
        bpy.ops.object.select_all(action='DESELECT')
        
        # 计数器
        selected_count = 0
        material_count = 0
        outline_materials = []
        
        # 遍历场景中所有网格对象
        for obj in context.scene.objects:
            if obj.type != 'MESH':
                continue
                
            has_outline_material = False
            obj_material_names = []
            
            # 检查对象的所有材质槽
            for mat_slot in obj.material_slots:
                if not mat_slot.material:
                    continue
                    
                material = mat_slot.material
                material_count += 1
                obj_material_names.append(material.name)
                
                # 检查材质名称是否包含"outline"（不区分大小写）
                if "outline" in material.name.lower():
                    has_outline_material = True
                    if material.name not in outline_materials:
                        outline_materials.append(material.name)
            
            # 如果对象有描边材质，选中它
            if has_outline_material:
                obj.select_set(True)
                selected_count += 1
        
        # 设置活动对象（如果有选中的对象）
        if selected_count > 0 and context.selected_objects:
            context.view_layer.objects.active = context.selected_objects[0]
        
        # 报告结果
        if selected_count > 0:
            outline_mat_str = ", ".join(outline_materials)
            self.report({'INFO'}, f"已选中 {selected_count} 个带描边材质的对象 (找到 {len(outline_materials)} 个描边材质: {outline_mat_str})")
        else:
            self.report({'INFO'}, "未找到使用描边材质的对象")
            
        return {'FINISHED'}

class MESHY_OT_CleanEmptyGroups(Operator):
    bl_idname = "meshy.clean_empty_groups"
    bl_label = "清除空组"
    bl_description = "递归清除所有不包含网格对象的空组"
    
    def execute(self, context):
        # 记录清除前的空组数量
        empty_groups_before = 0
        for obj in context.scene.objects:
            if obj.type == 'EMPTY' and not self._has_mesh_children(obj):
                empty_groups_before += 1
        
        # 保存需要删除的空组列表
        groups_to_delete = []
        for obj in context.scene.objects:
            if obj.type == 'EMPTY':
                if not self._has_mesh_children(obj):
                    groups_to_delete.append(obj)
        
        # 删除空组
        deleted_count = 0
        for obj in groups_to_delete:
            bpy.data.objects.remove(obj, do_unlink=True)
            deleted_count += 1
            
        # 报告结果
        if deleted_count > 0:
            self.report({'INFO'}, f"已清除 {deleted_count} 个空组")
        else:
            self.report({'INFO'}, "未找到需要清除的空组")
            
        return {'FINISHED'}
    
    def _has_mesh_children(self, obj, checked_objects=None):
        """递归检查对象是否包含网格子对象"""
        if checked_objects is None:
            checked_objects = set()
            
        # 防止循环引用导致的无限递归
        if obj in checked_objects:
            return False
        checked_objects.add(obj)
        
        # 检查直接子对象
        for child in obj.children:
            # 如果子对象是网格，返回True
            if child.type == 'MESH':
                return True
            # 如果子对象是组，递归检查
            elif child.type == 'EMPTY':
                if self._has_mesh_children(child, checked_objects):
                    return True
        
        # 没有找到网格子对象
        return False

class MESHY_OT_RotateModel(Operator):
    bl_idname = "meshy.rotate_model"
    bl_label = "旋转模型"
    bl_description = "按指定轴和角度旋转选中的模型"
    
    axis: EnumProperty(
        name="轴向",
        description="旋转轴",
        items=[
            ('X', "X轴", "沿X轴旋转"),
            ('Y', "Y轴", "沿Y轴旋转"),
            ('Z', "Z轴", "沿Z轴旋转"),
        ],
        default='Z'
    )
    
    angle: FloatProperty(
        name="角度",
        description="旋转角度（度）",
        default=90.0
    )
    
    def execute(self, context):
        import math
        from mathutils import Quaternion, Vector, Matrix
        
        # 检查是否有选中的物体
        if not context.selected_objects:
            self.report({'WARNING'}, "未选中任何物体")
            return {'CANCELLED'}
        
        # 将角度转换为弧度
        angle_rad = math.radians(self.angle)
        
        # 创建四元数旋转
        if self.axis == 'X':
            # 创建绕X轴旋转的四元数
            rotation_quat = Quaternion((1.0, 0.0, 0.0), angle_rad)
            # 创建世界坐标系下的旋转矩阵
            rotation_matrix = Matrix.Rotation(angle_rad, 4, 'X')
        elif self.axis == 'Y':
            # 创建绕Y轴旋转的四元数
            rotation_quat = Quaternion((0.0, 1.0, 0.0), angle_rad)
            # 创建世界坐标系下的旋转矩阵
            rotation_matrix = Matrix.Rotation(angle_rad, 4, 'Y')
        else:  # Z轴
            # 创建绕Z轴旋转的四元数
            rotation_quat = Quaternion((0.0, 0.0, 1.0), angle_rad)
            # 创建世界坐标系下的旋转矩阵
            rotation_matrix = Matrix.Rotation(angle_rad, 4, 'Z')
        
        # 应用旋转到所有选中的物体（使用世界坐标系旋转）
        for obj in context.selected_objects:
            # 保存物体原始位置
            original_location = obj.matrix_world.to_translation()
            
            # 应用世界坐标系下的旋转（先平移到原点，旋转，再平移回原位置）
            # 1. 创建平移到原点的矩阵
            trans_to_origin = Matrix.Translation(-original_location)
            # 2. 创建平移回原位置的矩阵
            trans_back = Matrix.Translation(original_location)
            # 3. 应用变换：平移到原点 -> 旋转 -> 平移回原位置
            obj.matrix_world = trans_back @ rotation_matrix @ trans_to_origin @ obj.matrix_world
        
        # 报告结果
        self.report({'INFO'}, f"已将选中物体沿世界坐标系{self.axis}轴旋转 {self.angle}°")
        return {'FINISHED'}

# 导出类列表
classes = [
    MESHY_OT_SetOperatorName,
    MESHY_OT_SetSourceDirectory,
    MESHY_OT_RefreshModelList,
    MESHY_OT_ImportModel,
    MESHY_OT_ExportModel,
    MESHY_OT_MarkStatus,
    MESHY_OT_PreviousModel,
    MESHY_OT_NextModel,
    MESHY_OT_KeyHandler,
    MESHY_OT_CreateParentNode,
    MESHY_OT_SaveProgress,
    MESHY_OT_LoadProgress,
    MESHY_OT_ClearProgress,
    MESHY_OT_RemoveAlpha,
    MESHY_OT_PurgeUnusedData,
    MESHY_OT_SelectOutlineMeshes,
    MESHY_OT_CleanEmptyGroups,
    MESHY_OT_RotateModel
] 
