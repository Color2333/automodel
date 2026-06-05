import bpy
import os
import glob
import datetime
import shutil
import unicodedata
import tempfile
import hashlib
from bpy.props import StringProperty, IntProperty, EnumProperty, BoolProperty, FloatProperty
from bpy.types import Operator
from mathutils import Vector

# 状态显示名（日志/UI 提示）；内部枚举 ID 不变
status_labels = {
    'UNMARKED': "未标记",
    'COMPLETED': "可修复",
    'NO_ACTION': "good",
    'QUESTIONABLE': "存疑",
    'UNFIXABLE': "bad",
    'HARD': "hard",
    'PARTS': "零件",
    'NOR_ERROR': "nor-error",
    'COMBO_ASSET': "组合资产",
}


def _meshy_operator_folder_suffix(settings):
    op = settings.operator_name or "unknown"
    return op


def status_subdir_for_export(status, operator_suffix):
    """输出根目录下的子文件夹名（与磁盘一致）。"""
    mapping = {
        'COMPLETED': f"Completed_{operator_suffix}",
        'NO_ACTION': f"Good_{operator_suffix}",
        'UNFIXABLE': f"Bad_{operator_suffix}",
        'QUESTIONABLE': f"Questionable_{operator_suffix}",
        'HARD': f"Hard_{operator_suffix}",
        'PARTS': f"Parts_{operator_suffix}",
        'NOR_ERROR': f"NorError_{operator_suffix}",
        'COMBO_ASSET': f"ComboAsset_{operator_suffix}",
    }
    return mapping.get(status)


def output_base_directory(settings):
    if not settings.output_directory:
        if settings.source_directory:
            settings.output_directory = os.path.dirname(settings.source_directory)
        else:
            return ""
    return settings.output_directory


def _nfc_filename(s):
    """与磁盘列表比对时使用 NFC，减少 macOS NFD / Windows 混用导致的「找不到文件」。"""
    if not s:
        return s
    return unicodedata.normalize("NFC", s)


# 终态：磁盘上有独立输出目录的状态（与 mark 自动导出一致）
TERMINAL_EXPORT_STATUSES = (
    'NO_ACTION', 'QUESTIONABLE', 'UNFIXABLE', 'HARD', 'PARTS',
    'NOR_ERROR', 'COMBO_ASSET',
)
SUPPORTED_SOURCE_EXTENSIONS = ('.glb', '.usdz')
OUTPUT_FILE_EXTENSIONS = ('.glb', '.usdz')
SCENE_CHECKPOINT_DIRNAME = "scene_checkpoints"
_MESHY_CHECKPOINT_SAVING = False
_MESHY_LAST_CHECKPOINT_SAVE = {}
CHECKPOINT_MIN_INTERVAL_SECONDS = 10.0


def current_output_extension(settings):
    return ".usdz" if getattr(settings, "output_format", "GLB") == 'USDZ' else ".glb"


def current_output_label(settings):
    return "USDZ" if current_output_extension(settings) == ".usdz" else "GLB"


def activate_source_directory(context, directory, report_fn=None):
    settings = context.scene.meshy_settings
    raw_directory = directory or ""
    if not raw_directory.strip():
        if report_fn:
            report_fn({'ERROR'}, "输入源目录无效")
        return {'CANCELLED'}

    directory = os.path.normpath(os.path.abspath(bpy.path.abspath(raw_directory)))

    if not directory or not os.path.isdir(directory):
        if report_fn:
            report_fn({'ERROR'}, "输入源目录无效")
        return {'CANCELLED'}

    if settings.auto_save_progress and settings.source_directory and settings.operator_name:
        settings.save_progress(context, True)
        try:
            _meshy_save_scene_checkpoint(context, report_fn, force=True)
        except NameError:
            pass

    context.scene.meshy_models.clear()
    settings.source_directory = directory
    settings.output_directory = os.path.dirname(directory)
    settings.current_model_index = 0
    settings.last_export_path = ""
    settings.last_save_time = ""
    settings.remember_source_directory(directory)
    settings.save_runtime_state()

    # 检测输入源目录中是否已有进度文件，并尝试获取用户名
    existing_operator = settings.detect_existing_progress()
    if existing_operator:
        settings.operator_name = existing_operator
        if report_fn:
            report_fn({'INFO'}, f"已自动设置用户名: {existing_operator}")

    loaded_progress = False
    if settings.auto_save_progress:
        loaded_progress = settings.load_progress(context)
        if loaded_progress:
            if context.scene.meshy_models:
                bpy.ops.meshy.import_model()
            if report_fn:
                report_fn(
                    {'INFO'},
                    f"已加载 {settings.operator_name} 在 {os.path.basename(directory)} 的处理进度",
                )

    if not loaded_progress:
        bpy.ops.meshy.refresh_model_list()

    settings.remember_source_directory(directory)
    settings.save_runtime_state()
    if report_fn:
        report_fn({'INFO'}, f"输入源已切换为: {directory}")
    return {'FINISHED'}


def _append_object_with_parents(objects, seen, obj):
    chain = []
    current = obj
    while current:
        chain.append(current)
        current = current.parent

    for item in reversed(chain):
        if item and item.name not in seen:
            objects.append(item)
            seen.add(item.name)


def expand_export_objects_with_armatures(objects):
    """Include armatures referenced by exported meshes so GLB skins are preserved."""
    expanded = []
    seen = set()

    for obj in objects:
        if obj and obj.name not in seen:
            expanded.append(obj)
            seen.add(obj.name)

    for obj in list(expanded):
        if not obj or obj.type != 'MESH':
            continue

        for modifier in obj.modifiers:
            if modifier.type != 'ARMATURE' or not modifier.object:
                continue
            _append_object_with_parents(expanded, seen, modifier.object)

    return expanded


def _matches_model_export_file(filename, model_name, extensions=OUTPUT_FILE_EXTENSIONS):
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in extensions:
        return False
    key = _nfc_filename(model_name)
    base = _nfc_filename(stem)
    if not key:
        return False
    if base == key:
        return True
    prefix = key + "_"
    return base.startswith(prefix) and base[len(prefix):].isdigit()


def list_model_exports_in_dir(directory, model_name, extensions=OUTPUT_FILE_EXTENSIONS):
    """列出目录内属于该模型的输出文件（单文件或 model_{纯数字索引}.ext）。"""
    if not directory or not os.path.isdir(directory):
        return []
    seen = set()
    for fn in os.listdir(directory):
        if _matches_model_export_file(fn, model_name, extensions):
            seen.add(os.path.join(directory, fn))
    return sorted(seen)


def _model_export_index(filename, model_name):
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in OUTPUT_FILE_EXTENSIONS:
        return None
    key = _nfc_filename(model_name)
    base = _nfc_filename(stem)
    if base == key:
        return 0
    prefix = key + "_"
    if base.startswith(prefix) and base[len(prefix):].isdigit():
        return int(base[len(prefix):])
    return None


def next_model_export_index(context, model):
    """返回跨所有输出分类目录的下一个 model_N 编号。"""
    settings = context.scene.meshy_settings
    base = output_base_directory(settings)
    if not base:
        return 1

    op_suffix = _meshy_operator_folder_suffix(settings)
    statuses = ('COMPLETED',) + TERMINAL_EXPORT_STATUSES
    max_index = 0
    for status in statuses:
        subdir = status_subdir_for_export(status, op_suffix)
        if not subdir:
            continue
        directory = os.path.join(base, subdir)
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            index = _model_export_index(filename, model.name)
            if index is not None:
                max_index = max(max_index, index)
    return max_index + 1


def purge_model_exports_from_directory(
    directory,
    model_name,
    report_fn=None,
    keep_paths=None,
    extensions=OUTPUT_FILE_EXTENSIONS,
):
    """删除目录内属于该模型的输出文件，返回删除数量。"""
    keep = {os.path.realpath(path) for path in (keep_paths or []) if path}
    removed = 0
    for path in list_model_exports_in_dir(directory, model_name, extensions=extensions):
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
    """从所有终态分类目录中移除该模型的旧 GLB/USDZ；keep_paths 内的新输出会保留。"""
    settings = context.scene.meshy_settings
    base = output_base_directory(settings)
    if not base:
        return 0
    op_suffix = _meshy_operator_folder_suffix(settings)
    total = 0
    for st in TERMINAL_EXPORT_STATUSES:
        sub = status_subdir_for_export(st, op_suffix)
        if not sub:
            continue
        d = os.path.join(base, sub)
        total += purge_model_exports_from_directory(d, model.name, report_fn, keep_paths=keep_paths)
    return total


def list_model_exports_for_status(context, model, status):
    settings = context.scene.meshy_settings
    base = output_base_directory(settings)
    if not base:
        return []
    op_suffix = _meshy_operator_folder_suffix(settings)
    subdir = status_subdir_for_export(status, op_suffix)
    if not subdir:
        return []
    return list_model_exports_in_dir(os.path.join(base, subdir), model.name)


def purge_completed_exports_for_model(context, model, report_fn=None):
    """从 Completed 目录移除该模型 GLB/USDZ（终态导出路径前清理陈旧副本，不用于「从 Completed 移动」的主路径）。"""
    settings = context.scene.meshy_settings
    base = output_base_directory(settings)
    if not base:
        return 0
    op_suffix = _meshy_operator_folder_suffix(settings)
    completed_dir = os.path.join(base, f"Completed_{op_suffix}")
    return purge_model_exports_from_directory(completed_dir, model.name, report_fn)


def move_completed_to_category(context, model, new_status, report_fn):
    """
    将 Completed_{op} 下本模型的 GLB/USDZ 移动到 new_status 对应分类目录。
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

    target_dir = os.path.join(base, subdir)
    sources = list_model_exports_in_dir(completed_dir, model.name)
    if not sources:
        # LEP 兜底：仅当 last_export_path 指向 Completed 内、且文件名严格匹配
        # ({model}.ext 或 {model}_{digits}.ext) 时，把该单文件作为源。不再按
        # 「以 {model}_ 开头」的宽松规则重扫目录，避免吞掉同前缀的其他模型文件。
        lep = settings.last_export_path
        if lep and os.path.isfile(lep):
            lep_dir = os.path.realpath(os.path.dirname(lep))
            if lep_dir == os.path.realpath(completed_dir):
                bn = os.path.basename(lep)
                if _matches_model_export_file(bn, model.name):
                    sources = [lep]
    if not sources:
        report_fn({'ERROR'}, f"在 Completed 目录中未找到模型「{model.name}」的 GLB/USDZ，请先导出到 Completed")
        return False, ""

    os.makedirs(target_dir, exist_ok=True)
    last_path = ""
    for src in sources:
        dest = os.path.join(target_dir, os.path.basename(src))
        backup_dest = ""
        try:
            if os.path.exists(dest):
                backup_dest = f"{dest}.meshy_backup_{os.getpid()}"
                suffix = 1
                while os.path.exists(backup_dest):
                    backup_dest = f"{dest}.meshy_backup_{os.getpid()}_{suffix}"
                    suffix += 1
                os.replace(dest, backup_dest)
            shutil.move(src, dest)
            if backup_dest and os.path.exists(backup_dest):
                os.remove(backup_dest)
            last_path = dest
        except OSError as e:
            if backup_dest and os.path.exists(backup_dest):
                try:
                    if os.path.exists(dest):
                        os.remove(dest)
                    os.replace(backup_dest, dest)
                except OSError:
                    pass
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
    report_fn({'INFO'}, f"已将 {len(sources)} 个文件移至 {target_dir}")
    return True, last_path


def navigation_blocked_while_completed(context):
    """可修复(COMPLETED) 期间禁止上一项/下一项，须先导出 Completed 并完成终态分类。"""
    models = getattr(context.scene, "meshy_models", None)
    settings = getattr(context.scene, "meshy_settings", None)
    if not models or not settings or len(models) == 0:
        return False
    idx = settings.current_model_index
    if idx < 0 or idx >= len(models):
        return False
    return models[idx].status == 'COMPLETED'


def _meshy_current_model(context):
    models = getattr(context.scene, "meshy_models", None)
    settings = getattr(context.scene, "meshy_settings", None)
    if not models or not settings or len(models) == 0:
        return None
    idx = settings.current_model_index
    if idx < 0 or idx >= len(models):
        return None
    return models[idx]


def _meshy_scene_checkpoint_path(settings, model):
    if not settings or not model or not settings.source_directory:
        return ""

    progress_filepath = settings.get_progress_filepath()
    if not progress_filepath:
        return ""

    checkpoint_dir = os.path.join(os.path.dirname(progress_filepath), SCENE_CHECKPOINT_DIRNAME)
    source_key = os.path.realpath(model.path or model.name)
    digest = hashlib.sha1(source_key.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in model.name).strip("._")
    safe_name = (safe_name or "model")[:80]
    return os.path.join(checkpoint_dir, f"{safe_name}_{digest}.blend")


def _meshy_save_scene_checkpoint(context, report_fn=None, force=False):
    global _MESHY_CHECKPOINT_SAVING
    settings = getattr(context.scene, "meshy_settings", None)
    if not settings or (not settings.auto_save_progress and not force):
        return False
    if _MESHY_CHECKPOINT_SAVING:
        return False
    if not context.scene.objects:
        return False

    model = _meshy_current_model(context)
    checkpoint_path = _meshy_scene_checkpoint_path(settings, model)
    if not checkpoint_path:
        return False

    if not force:
        try:
            now = datetime.datetime.now().timestamp()
            last_saved = _MESHY_LAST_CHECKPOINT_SAVE.get(checkpoint_path, 0)
            if now - last_saved < CHECKPOINT_MIN_INTERVAL_SECONDS:
                return False
        except Exception:
            pass

    try:
        _MESHY_CHECKPOINT_SAVING = True
        os.makedirs(os.path.dirname(checkpoint_path), exist_ok=True)
        datablocks = set(context.scene.objects)
        bpy.data.libraries.write(checkpoint_path, datablocks, fake_user=True)
        _MESHY_LAST_CHECKPOINT_SAVE[checkpoint_path] = datetime.datetime.now().timestamp()
        return True
    except TypeError:
        try:
            bpy.ops.wm.save_as_mainfile(
                filepath=checkpoint_path,
                check_existing=False,
                copy=True,
            )
            _MESHY_LAST_CHECKPOINT_SAVE[checkpoint_path] = datetime.datetime.now().timestamp()
            return True
        except Exception as e:
            if report_fn:
                report_fn({'WARNING'}, f"拆分现场保存失败: {e}")
            return False
    except Exception as e:
        if report_fn:
            report_fn({'WARNING'}, f"拆分现场保存失败: {e}")
        return False
    finally:
        _MESHY_CHECKPOINT_SAVING = False


def _meshy_autosave_scene_edit(context, report_fn=None):
    settings = getattr(context.scene, "meshy_settings", None)
    if not settings or not settings.auto_save_progress:
        return False
    settings.save_progress(context)
    return _meshy_save_scene_checkpoint(context, report_fn)


def save_active_scene_checkpoint_for_handlers():
    context = bpy.context
    settings = getattr(context.scene, "meshy_settings", None)
    if not settings or not settings.auto_save_progress:
        return False
    settings.save_progress(context)
    return _meshy_save_scene_checkpoint(context, force=True)


def _meshy_load_scene_checkpoint(context, model, report_fn=None):
    settings = getattr(context.scene, "meshy_settings", None)
    checkpoint_path = _meshy_scene_checkpoint_path(settings, model)
    if not checkpoint_path or not os.path.exists(checkpoint_path):
        return False

    try:
        _meshy_clear_scene_before_import(context)
        with bpy.data.libraries.load(checkpoint_path, link=False) as (data_from, data_to):
            data_to.objects = list(data_from.objects)

        loaded_objects = [obj for obj in data_to.objects if obj]
        for obj in loaded_objects:
            if not obj.users_collection:
                context.collection.objects.link(obj)

        bpy.context.view_layer.update()
        bpy.ops.view3d.view_all(center=False)
        if report_fn:
            report_fn({'INFO'}, f"已恢复拆分现场: {model.name}")
        return True
    except Exception as e:
        if report_fn:
            report_fn({'WARNING'}, f"恢复拆分现场失败，将重新导入源模型: {e}")
        return False


def _meshy_delete_scene_checkpoint(context, model=None):
    settings = getattr(context.scene, "meshy_settings", None)
    model = model or _meshy_current_model(context)
    checkpoint_path = _meshy_scene_checkpoint_path(settings, model)
    if not checkpoint_path or not os.path.exists(checkpoint_path):
        return False
    try:
        os.remove(checkpoint_path)
        return True
    except OSError:
        return False


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
    bl_description = "设置包含GLB/USDZ模型的源目录"
    
    directory: StringProperty(subtype='DIR_PATH')
    
    def execute(self, context):
        return activate_source_directory(context, self.directory, self.report)
    
    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class MESHY_OT_SwitchSourceDirectory(Operator):
    bl_idname = "meshy.switch_source_directory"
    bl_label = "切换输入源"
    bl_description = "切换到已记录的输入源目录，并加载该目录自己的进度"

    source_key: StringProperty(default="")

    def execute(self, context):
        settings = context.scene.meshy_settings
        directory = settings.resolve_source_directory_choice(self.source_key)
        if not directory:
            self.report({'ERROR'}, "请选择有效的输入源")
            return {'CANCELLED'}

        if settings.source_directory and os.path.realpath(directory) == os.path.realpath(settings.source_directory):
            settings.remember_source_directory(directory)
            settings.save_runtime_state()
            self.report({'INFO'}, "当前已是该输入源")
            return {'FINISHED'}

        return activate_source_directory(context, directory, self.report)

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
            }
            if model.path:
                cached_by_path[os.path.realpath(model.path)] = cached
            if model.name:
                cached_by_name[model.name] = cached
        
        # 清空当前模型列表
        context.scene.meshy_models.clear()
        
        # 查找所有GLB/USDZ文件；同名同时存在时以GLB作为源，避免同名模型重复进列表。
        model_files_by_name = {}
        for filename in os.listdir(settings.source_directory):
            model_path = os.path.join(settings.source_directory, filename)
            if not os.path.isfile(model_path):
                continue
            model_name, ext = os.path.splitext(filename)
            ext = ext.lower()
            if ext not in SUPPORTED_SOURCE_EXTENSIONS:
                continue
            current_path = model_files_by_name.get(model_name)
            if current_path is None or ext == ".glb":
                model_files_by_name[model_name] = model_path

        model_files = sorted(
            model_files_by_name.values(),
            key=lambda path: _nfc_filename(os.path.basename(path)).lower(),
        )

        if not model_files:
            self.report({'WARNING'}, "源目录中未找到GLB或USDZ模型")
            return {'CANCELLED'}
        
        # 添加模型到列表
        for model_path in model_files:
            filename = os.path.basename(model_path)
            model_name = os.path.splitext(filename)[0]
            
            # 创建新模型项
            model_item = context.scene.meshy_models.add()
            model_item.name = model_name
            model_item.path = model_path
            cached = cached_by_path.get(os.path.realpath(model_path)) or cached_by_name.get(model_name)
            if cached:
                model_item.status = cached["status"]
                model_item.export_history = cached["export_history"]
                model_item.last_exported_status = cached["last_exported_status"]
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


def _meshy_cleanup_usd_temp_dirs():
    try:
        tmp_root = tempfile.gettempdir()
        usd_tmp_dirs = glob.glob(os.path.join(tmp_root, "blender_*", "usd_textures_tmp"))
        for directory in usd_tmp_dirs:
            if os.path.isdir(directory):
                shutil.rmtree(directory, ignore_errors=True)
    except Exception:
        pass


def _meshy_clear_scene_before_import(context):
    """导入前清空场景：按数据块删除对象，不依赖全选（避免漏删不可选、glTF_not_exported 内等残留）。"""
    scene = context.scene
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
    _meshy_cleanup_usd_temp_dirs()


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

        if _meshy_load_scene_checkpoint(context, current_model, self.report):
            return {'FINISHED'}
        
        _meshy_clear_scene_before_import(context)
        
        # 导入模型
        try:
            # 根据路径判断导入函数
            ext = os.path.splitext(current_model.path)[1].lower()
            if ext == '.glb':
                bpy.ops.import_scene.gltf(
                    filepath=current_model.path,
                    import_pack_images=False,
                    guess_original_bind_pose=False,
                )
            elif ext == '.usdz':
                if hasattr(bpy.ops.wm, "usd_import"):
                    bpy.ops.wm.usd_import(filepath=current_model.path)
                else:
                    self.report({'ERROR'}, "当前Blender版本不支持USDZ导入")
                    return {'CANCELLED'}
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
    keys = (
        'COMPLETED', 'NO_ACTION', 'QUESTIONABLE', 'UNFIXABLE', 'HARD',
        'PARTS', 'NOR_ERROR', 'COMBO_ASSET',
    )
    status_dirs = {}
    for k in keys:
        sub = status_subdir_for_export(k, op_suffix)
        if sub:
            path = os.path.join(base_dir, sub)
            status_dirs[k] = path
            os.makedirs(path, exist_ok=True)
    return status_dirs

# 导出模型
class MESHY_OT_ExportModel(Operator):
    """
    导出模型
    
    在按顶级节点导出时，文件命名采用"模型名_索引号.扩展名"的方式，
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
            ('ALL_GROUPS', "导出所有", "将所有包含网格的组合并为一个模型文件导出"),
        ],
        default='TOP_LEVEL'
    )

    selected_group_index: IntProperty(
        name="选中对象编号",
        description="仅选中对象导出时使用的文件编号；0 表示不追加编号",
        default=0,
    )
    
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
            
        # 保存原始选择状态以便稍后恢复
        original_selection = context.selected_objects[:]
        original_active = context.view_layer.objects.active
        
        try:
            if self.export_mode == 'SELECTED':
                # 检查是否有选中的对象
                if not context.selected_objects:
                    self.report({'ERROR'}, "没有选中任何对象")
                    return {'CANCELLED'}
                
                objects = self.collect_selected_export_objects(context)
                group_index = self.selected_group_index if self.selected_group_index > 0 else None
                group_name = self.selected_group_name(context)

                # 导出选中的对象；选中父级时会带上子对象。
                if self.export_objects(
                    context,
                    current_model,
                    objects,
                    group_index=group_index,
                    group_name=group_name,
                ) == {'FINISHED'}:
                    return {'FINISHED'}
                else:
                    return {'CANCELLED'}
                    
            elif self.export_mode == 'TOP_LEVEL':
                # 获取所有顶级对象（没有父对象的对象）
                top_level_objects = [obj for obj in context.scene.objects if not obj.parent]
                
                if not top_level_objects:
                    self.report({'ERROR'}, "场景中没有顶级对象")
                    return {'CANCELLED'}
                
                # 按顶级节点进行组织导出
                successful_exports = 0
                # 添加连续计数器
                continuous_index = 1
                for i, top_obj in enumerate(top_level_objects):
                    # 收集这个顶级对象及其所有子对象
                    objects_list = [top_obj]
                    self.get_all_children(top_obj, objects_list)
                    
                    # 检查这组对象中是否至少有一个网格对象
                    has_mesh = False
                    for obj in objects_list:
                        if obj.type == 'MESH':
                            has_mesh = True
                            break
                    
                    if has_mesh:
                        # 使用连续索引代替原始索引
                        if self.export_objects(
                            context,
                            current_model,
                            objects_list,
                            group_index=continuous_index,
                            group_name=top_obj.name,
                            save_after=False,
                        ) == {'FINISHED'}:
                            successful_exports += 1
                            # 成功导出后递增连续索引
                            continuous_index += 1
                
                if successful_exports > 0:
                    if settings.auto_save_progress:
                        settings.save_progress(context)
                        _meshy_save_scene_checkpoint(context, self.report, force=True)
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
                
                for top_obj in top_level_objects:
                    # 收集这个顶级对象及其所有子对象
                    objects_list = [top_obj]
                    self.get_all_children(top_obj, objects_list)
                    
                    # 检查这组对象中是否至少有一个网格对象
                    has_mesh = False
                    for obj in objects_list:
                        if obj.type == 'MESH':
                            has_mesh = True
                            break
                    
                    if has_mesh:
                        valid_groups.append(top_obj.name)
                        valid_objects.extend(objects_list)
                
                if not valid_groups:
                    self.report({'WARNING'}, "没有找到包含网格的组")
                    return {'CANCELLED'}
                
                # 导出所有有效对象为一个文件
                if self.export_objects(context, current_model, valid_objects, group_name="All_Groups") == {'FINISHED'}:
                    self.report({'INFO'}, f"已将所有组 ({len(valid_groups)} 个) 导出为一个模型文件")
                    
                    return {'FINISHED'}
                else:
                    return {'CANCELLED'}
                
            return {'CANCELLED'}
            
        finally:
            # 恢复原始选择状态
            bpy.ops.object.select_all(action='DESELECT')
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
    
    def export_objects(self, context, model, objects, group_index=None, group_name=None, save_after=True):
        settings = context.scene.meshy_settings
        
        # 保存原始游标位置，以便稍后恢复
        original_cursor_location = context.scene.cursor.location.copy()
        
        if not settings.output_directory:
            settings.output_directory = os.path.dirname(settings.source_directory)
        
        status = model.status
        op_suffix = _meshy_operator_folder_suffix(settings)
        subdir = status_subdir_for_export(status, op_suffix)
        if not subdir:
            self.report({'WARNING'}, f"当前状态无法导出 {current_output_label(settings)}")
            context.scene.cursor.location = original_cursor_location
            return {'CANCELLED'}
        
        current_status_dir = os.path.join(settings.output_directory, subdir)
        os.makedirs(current_status_dir, exist_ok=True)
        original_locations = {}
        
        try:
            # 确定文件名和扩展名；命名规则沿用 GLB 版本，只替换扩展名。
            file_extension = current_output_extension(settings)
                
            # 仅「按组导出」传入 group_index；「导出所有」只传 group_name 时 index 为 None，不得生成 xxx_None.ext。
            if group_index is not None:
                export_filename = f"{model.name}_{group_index}{file_extension}"
            else:
                export_filename = f"{model.name}{file_extension}"
                
            export_path = os.path.join(current_status_dir, export_filename)
            
            objects = expand_export_objects_with_armatures(list(objects))

            # 保存原始位置
            original_locations = {obj: obj.location.copy() for obj in objects}
            
            # 计算选择集的中心位置（只考虑网格对象）
            mesh_objects = [obj for obj in objects if obj.type == 'MESH']
            if not mesh_objects:
                self.report({'ERROR'}, "选择中没有网格对象")
                return {'CANCELLED'}
            
            # 计算中心点
            center = Vector((0, 0, 0))
            if mesh_objects:
                center = sum([obj.location for obj in mesh_objects], Vector()) / len(mesh_objects)
            
            # 找出顶级对象（没有父级或者父级不在选择集中的对象）
            objects_set = set(objects)
            top_level_in_selection = [obj for obj in objects if not obj.parent or obj.parent not in objects_set]
            
            # 只移动顶级对象的位置，子对象会自动跟随
            for obj in top_level_in_selection:
                obj.location = obj.location - center
            
            # 更新场景
            bpy.context.view_layer.update()
            
            # 取消选择所有对象
            bpy.ops.object.select_all(action='DESELECT')
            
            # 选择要导出的对象
            for obj in objects:
                obj.select_set(True)
            
            if file_extension == ".glb":
                bpy.ops.export_scene.gltf(
                    filepath=export_path,
                    export_format='GLB',
                    use_selection=True,
                    export_extras=False,
                    export_skins=True,
                    export_animations=False,
                    export_image_format='AUTO',
                )
            elif file_extension == ".usdz":
                if not hasattr(bpy.ops.wm, "usd_export"):
                    raise RuntimeError("当前Blender版本不支持USDZ导出")
                if not self._usd_export(filepath=export_path):
                    raise RuntimeError("USDZ导出失败")
            
            # 恢复原始位置
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
            for obj in objects:
                try:
                    if obj and obj.name in context.view_layer.objects:
                        obj.select_set(False)
                except ReferenceError:
                    # 对象已被删除，忽略它
                    continue

            # 记录结果
            settings.last_export_path = export_path
            
            # 更新导出历史
            import datetime
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            history_entry = f"{timestamp}: {settings.operator_name} "
            
            if group_name:
                history_entry += f"以'{group_name}'组导出到 {os.path.basename(export_path)}\n"
            else:
                history_entry += f"导出 {len(objects)} 个对象到 {os.path.basename(export_path)}\n"
            
            if model.export_history:
                model.export_history = history_entry + model.export_history
            else:
                model.export_history = history_entry
            
            # 更新模型的last_exported_status
            model.last_exported_status = model.status
            
            # 保存进度
            if settings.auto_save_progress and save_after:
                settings.save_progress(context)
                _meshy_save_scene_checkpoint(context, self.report, force=True)
            
            # 输出日志
            if group_name:
                self.report({'INFO'}, f"已导出组 '{group_name}' 到 {export_path}")
            else:
                self.report({'INFO'}, f"已导出 {len(objects)} 个对象到 {export_path}")
            
            return {'FINISHED'}
        
        except Exception as e:
            self.report({'ERROR'}, f"导出失败: {str(e)}")
            return {'CANCELLED'}
        
        finally:
            # 导出完成或失败都恢复物体位置，避免 USD/GLB 导出异常后污染当前模型。
            for obj, loc in original_locations.items():
                try:
                    if obj and obj.name in context.view_layer.objects:
                        obj.location = loc
                except ReferenceError:
                    continue

            for obj in objects:
                try:
                    if obj and obj.name in context.view_layer.objects:
                        obj.select_set(False)
                except ReferenceError:
                    continue

            bpy.context.view_layer.update()
            context.scene.cursor.location = original_cursor_location
    
    def invoke(self, context, event):
        # 直接执行，不再弹出选择对话框
        return self.execute(context)

    def _usd_export(self, filepath):
        """
        兼容不同 Blender 版本的 USDZ 导出参数。
        只传当前 Blender 版本支持的参数；如果运行时仍遇到未识别 keyword，则移除后重试。
        """
        kwargs = {"filepath": filepath}
        optional_kwargs = {
            "selected_objects_only": True,
            "export_textures": True,
            "export_relative_paths": True,
        }

        supported = None
        try:
            supported = {
                prop.identifier
                for prop in bpy.ops.wm.usd_export.get_rna_type().properties
            }
        except Exception:
            pass

        for key, value in optional_kwargs.items():
            if supported is None or key in supported:
                kwargs[key] = value

        retry_order = ("export_relative_paths", "export_textures", "selected_objects_only")
        last_error = None
        while True:
            try:
                bpy.ops.wm.usd_export(**kwargs)
                return True
            except TypeError as e:
                last_error = e
                removed = False
                message = str(e)

                for key in retry_order:
                    if f'keyword "{key}" unrecognized' in message and key in kwargs:
                        del kwargs[key]
                        removed = True
                        break

                if not removed:
                    for key in retry_order:
                        if key in kwargs:
                            del kwargs[key]
                            removed = True
                            break

                if not removed:
                    break
            except Exception as e:
                self.report({'ERROR'}, f"USDZ导出失败: {str(e)}")
                return False

        self.report({'ERROR'}, f"USDZ导出失败: {str(last_error)}")
        return False
        
    def _export_glb(self, filepath, objects):
        """导出选定对象为GLB格式，避免创建额外骨骼数据"""
        objects = expand_export_objects_with_armatures(list(objects))

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
                export_skins=True,
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

    def collect_selected_export_objects(self, context):
        objects = []
        seen = set()
        for obj in context.selected_objects:
            if obj.name not in seen:
                objects.append(obj)
                seen.add(obj.name)
            descendants = []
            self.get_all_children(obj, descendants)
            for child in descendants:
                if child.name not in seen:
                    objects.append(child)
                    seen.add(child.name)
        return objects

    def selected_group_name(self, context):
        active = context.view_layer.objects.active
        if active and active.select_get():
            return active.name
        if len(context.selected_objects) == 1:
            return context.selected_objects[0].name
        return "Selected"

# 标记状态
class MESHY_OT_ToggleMarkMode(Operator):
    bl_idname = "meshy.toggle_mark_mode"
    bl_label = "切换打标模式"
    bl_description = "在整体模型和仅选中对象打标模式之间切换"

    def execute(self, context):
        settings = context.scene.meshy_settings
        if getattr(settings, "mark_mode", "WHOLE_MODEL") == 'SELECTED_OBJECTS':
            settings.mark_mode = 'WHOLE_MODEL'
        else:
            settings.mark_mode = 'SELECTED_OBJECTS'

        settings.save_runtime_state()
        label = "仅选中对象" if settings.mark_mode == 'SELECTED_OBJECTS' else "整体模型"
        self.report({'INFO'}, f"打标模式已切换为: {label}")
        return {'FINISHED'}


class MESHY_OT_MarkStatus(Operator):
    bl_idname = "meshy.mark_status"
    bl_label = "标记状态"
    bl_description = "标记当前模型的处理状态"
    
    status: EnumProperty(
        name="状态",
        items=[
            ('COMPLETED', "可修复", "需要修复，导出至 Completed 后再选分类"),
            ('NO_ACTION', "good", "归类为 good"),
            ('QUESTIONABLE', "存疑", "有问题，待确认"),
            ('UNFIXABLE', "bad", "归类为 bad"),
            ('HARD', "hard", "归类为 hard"),
            ('PARTS', "零件", "判定为零件，无需处理"),
            ('NOR_ERROR', "nor-error", "归类为 nor-error"),
            ('COMBO_ASSET', "组合资产", "归类为组合资产"),
        ]
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
        selected_mark_mode = getattr(settings, "mark_mode", "WHOLE_MODEL") == 'SELECTED_OBJECTS'
        
        # 标为可修复只进入修复态；旧终态输出等重新完成分类后再清理，避免恢复/继续时先删成果。
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
                _meshy_save_scene_checkpoint(context, self.report)
            return {'FINISHED'}
        
        # 终态：先完成导出/移动，再清理旧副本；失败路径不能删除已有成果。
        if self.status in TERMINAL_EXPORT_STATUSES:
            if selected_mark_mode and prev_status != 'COMPLETED':
                if not context.selected_objects:
                    self.report({'ERROR'}, "没有选中任何对象")
                    return {'CANCELLED'}
                current_model.status = self.status
                if not context.scene.objects:
                    self.report({'ERROR'}, "场景中没有对象，无法导出")
                    current_model.status = prev_status
                    return {'CANCELLED'}

                next_index = next_model_export_index(context, current_model)
                result = bpy.ops.meshy.export_model(
                    'EXEC_DEFAULT',
                    export_mode='SELECTED',
                    selected_group_index=next_index,
                )
                if result != {'FINISHED'}:
                    current_model.status = prev_status
                    self.report({'ERROR'}, "选中对象导出失败，状态已恢复")
                    return {'CANCELLED'}
            elif prev_status == 'COMPLETED':
                ok, _ = move_completed_to_category(
                    context, current_model, self.status, self.report
                )
                if not ok:
                    return {'CANCELLED'}
                current_model.status = self.status
                keep_paths = list_model_exports_for_status(context, current_model, self.status)
                purge_terminal_exports_for_model(
                    context,
                    current_model,
                    self.report,
                    keep_paths=keep_paths,
                )
            else:
                current_model.status = self.status
                if not context.scene.objects:
                    self.report({'ERROR'}, "场景中没有对象，无法导出")
                    current_model.status = prev_status
                    return {'CANCELLED'}
                result = bpy.ops.meshy.export_model('EXEC_DEFAULT', export_mode='TOP_LEVEL')
                if result != {'FINISHED'}:
                    current_model.status = prev_status
                    self.report({'ERROR'}, "自动导出失败，状态已恢复")
                    return {'CANCELLED'}
                keep_paths = list_model_exports_for_status(context, current_model, self.status)
                purge_terminal_exports_for_model(
                    context,
                    current_model,
                    self.report,
                    keep_paths=keep_paths,
                )
                # 非「从 Completed 移动」：成功导出终态后再清 Completed 中同名陈旧文件，避免失败时误删。
                purge_completed_exports_for_model(context, current_model, self.report)
            
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            history_entry = f"{timestamp}: {settings.operator_name} 将状态标记为 \"{status_labels[self.status]}\"\n"
            if current_model.export_history:
                current_model.export_history = history_entry + current_model.export_history
            else:
                current_model.export_history = history_entry
            self.report({'INFO'}, f"模型状态已标记为: {status_labels[self.status]}")
            if settings.auto_save_progress:
                settings.save_progress(context, True)
            if selected_mark_mode and prev_status != 'COMPLETED':
                return {'FINISHED'}

            _meshy_delete_scene_checkpoint(context, current_model)
            
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
                "当前为「可修复」：请先导出到 Completed，再点 good/bad 等完成分类后方可切换上一项",
            )
            return {'CANCELLED'}
            
        # 自动保存进度
        if settings.auto_save_progress:
            bpy.ops.meshy.save_progress()
        
        # 切换到上一个模型（未标记也可返回上一项）
        if current_index > 0:
            settings.current_model_index = current_index - 1
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

            _meshy_autosave_scene_edit(context, self.report)
            
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
            _meshy_save_scene_checkpoint(context, self.report, force=True)
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
            progress_filepath = settings.get_progress_filepath()
            if progress_filepath:
                checkpoint_dir = os.path.join(os.path.dirname(progress_filepath), SCENE_CHECKPOINT_DIRNAME)
                shutil.rmtree(checkpoint_dir, ignore_errors=True)
            self.report({'INFO'}, "进度已清除")
            return {'FINISHED'}
        else:
            self.report({'ERROR'}, "没有找到保存的进度")
            return {'CANCELLED'}

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
            _meshy_autosave_scene_edit(context, self.report)
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
            _meshy_autosave_scene_edit(context, self.report)
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
        _meshy_autosave_scene_edit(context, self.report)
        return {'FINISHED'}

# 导出类列表
classes = [
    MESHY_OT_SetOperatorName,
    MESHY_OT_SetSourceDirectory,
    MESHY_OT_RefreshModelList,
    MESHY_OT_ImportModel,
    MESHY_OT_ExportModel,
    MESHY_OT_ToggleMarkMode,
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
