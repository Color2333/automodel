import bpy
import os
import json
import hashlib
import tempfile
from bpy.props import (StringProperty, IntProperty, BoolProperty,
                       EnumProperty, CollectionProperty, PointerProperty)
from bpy.types import PropertyGroup

_MESHY_MODEL_STATUS_IDS = frozenset({
    'UNMARKED', 'COMPLETED', 'FIXED', 'NO_ACTION', 'QUESTIONABLE',
    'UNFIXABLE', 'HARD', 'PARTS',
})

HARDFIX_TAG_ITEMS = (
    ('SOURCE_ABNORMAL', "原始模型异常", "源 GLB 自身异常，无法进入修复流程"),
    ('PARTS', "零部件类", "模型属于零部件类，归档到 HardFix"),
    ('HARD_TO_FIX', "难以修复", "修复成本过高或当前无法修复"),
    ('OTHER', "其他", "其他原因，必须填写理由"),
)
HARDFIX_TAG_IDS = frozenset(item[0] for item in HARDFIX_TAG_ITEMS)


def _coerce_meshy_model_status(value, default='UNMARKED'):
    if isinstance(value, str) and value in _MESHY_MODEL_STATUS_IDS:
        return value
    return default


def _coerce_hardfix_tag(value, default='HARD_TO_FIX'):
    if isinstance(value, str) and value in HARDFIX_TAG_IDS:
        return value
    return default


def _normalized_dir(path):
    if not path:
        return ""
    try:
        path = bpy.path.abspath(path)
    except Exception:
        pass
    return os.path.normpath(path)


def ensure_directory_writable(path):
    path = _normalized_dir(path)
    if not path:
        return False, "empty path"

    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return False, str(e)

    probe_name = f".meshy_write_probe_{os.getpid()}.tmp"
    probe_path = os.path.join(path, probe_name)
    try:
        with open(probe_path, 'w', encoding='utf-8') as f:
            f.write("ok")
    except OSError as e:
        return False, str(e)
    finally:
        try:
            if os.path.exists(probe_path):
                os.remove(probe_path)
        except OSError:
            pass

    return True, ""


def _runtime_cache_root():
    base = bpy.utils.user_resource('CONFIG') or tempfile.gettempdir()
    return os.path.join(base, "meshy_autoglb_runtime")


def runtime_state_filepath():
    return os.path.join(_runtime_cache_root(), "session_state.json")


def _runtime_cache_dir(source_directory, purpose):
    source_key = _normalized_dir(source_directory) or "no_source"
    digest = hashlib.sha1(source_key.encode('utf-8')).hexdigest()[:10]
    source_name = os.path.basename(source_key.rstrip("\\/")) or "workspace"
    safe_name = "".join(ch if ch.isalnum() or ch in ('-', '_') else "_" for ch in source_name)
    return os.path.join(_runtime_cache_root(), purpose, f"{safe_name}_{digest}")


def _explicit_fallback_dir(settings, purpose):
    base = _normalized_dir(settings.local_fallback_directory)
    if not base:
        return ""
    return os.path.join(base, purpose)


def _store_runtime_resolution(settings, resolved_dir, warning):
    settings.resolved_output_directory = resolved_dir or ""
    settings.last_path_warning = warning or ""


def resolve_output_base_directory(settings):
    preferred = settings.output_directory
    if not preferred:
        if settings.source_directory:
            preferred = _normalized_dir(settings.source_directory)
            settings.output_directory = preferred
        else:
            _store_runtime_resolution(settings, "", "")
            return ""

    preferred = _normalized_dir(preferred)
    ok, err = ensure_directory_writable(preferred)
    if ok:
        _store_runtime_resolution(settings, preferred, "")
        return preferred

    explicit_fallback = _explicit_fallback_dir(settings, "exports")
    if explicit_fallback:
        explicit_ok, explicit_err = ensure_directory_writable(explicit_fallback)
        if explicit_ok:
            _store_runtime_resolution(
                settings,
                explicit_fallback,
                f"输出目录不可写，已回退到用户设置的本地目录: {preferred} ({err})",
            )
            return explicit_fallback
    else:
        explicit_err = "not configured"

    fallback = _runtime_cache_dir(settings.source_directory, "exports")
    fallback_ok, fallback_err = ensure_directory_writable(fallback)
    if fallback_ok:
        _store_runtime_resolution(
            settings,
            fallback,
            f"输出目录不可写，已回退到自动本地缓存: {preferred} ({err})",
        )
        return fallback

    _store_runtime_resolution(
        settings,
        "",
        (
            f"输出目录与回退目录均不可写: {preferred} ({err}); "
            f"{explicit_fallback or '未配置显式回退目录'} ({explicit_err}); "
            f"{fallback} ({fallback_err})"
        ),
    )
    return ""


def resolve_progress_filepath(settings):
    if not settings.source_directory:
        settings.resolved_progress_directory = ""
        return ""

    preferred_dir = os.path.join(_normalized_dir(settings.source_directory), ".progress")
    ok, err = ensure_directory_writable(preferred_dir)
    if ok:
        settings.resolved_progress_directory = preferred_dir
        return os.path.join(preferred_dir, "progress.json")

    explicit_fallback_dir = _explicit_fallback_dir(settings, "progress")
    if explicit_fallback_dir:
        explicit_ok, explicit_err = ensure_directory_writable(explicit_fallback_dir)
        if explicit_ok:
            settings.resolved_progress_directory = explicit_fallback_dir
            settings.last_path_warning = (
                f"进度目录不可写，已回退到用户设置的本地目录: {preferred_dir} ({err})"
            )
            return os.path.join(explicit_fallback_dir, "progress.json")
    else:
        explicit_err = "not configured"

    fallback_dir = _runtime_cache_dir(settings.source_directory, "progress")
    fallback_ok, fallback_err = ensure_directory_writable(fallback_dir)
    if fallback_ok:
        settings.resolved_progress_directory = fallback_dir
        settings.last_path_warning = (
            f"进度目录不可写，已回退到自动本地缓存: {preferred_dir} ({err})"
        )
        return os.path.join(fallback_dir, "progress.json")

    settings.resolved_progress_directory = ""
    settings.last_path_warning = (
        f"进度目录与回退目录均不可写: {preferred_dir} ({err}); "
        f"{explicit_fallback_dir or '未配置显式回退目录'} ({explicit_err}); "
        f"{fallback_dir} ({fallback_err})"
    )
    return ""


# 模型项目类
class ModelItem(PropertyGroup):
    name: StringProperty(name="名称")
    path: StringProperty(name="路径")
    preview_path: StringProperty(name="预览图路径")
    status: EnumProperty(
        name="状态",
        items=[
            ('UNMARKED', "未标记", "尚未进行处理"),
            ('COMPLETED', "可修复", "需要人工修复的中间状态"),
            ('FIXED', "已修复", "已修复并导出"),
            ('NO_ACTION', "无需修复", "无需进行任何处理"),
            ('QUESTIONABLE', "存疑", "有问题，待确认"),
            ('UNFIXABLE', "bad", "无法修复的问题"),
            ('HARD', "难以修复", "难以修复并需填写原因"),
            ('PARTS', "零件", "判定为零件"),
        ],
        default='UNMARKED'
    )
    
    # 导出历史记录
    export_history: StringProperty(name="导出历史")
    
    # 难以修复原因
    hardfix_reason: StringProperty(
        name="其他理由",
        description="归档分类为其他时必须填写的理由",
        default=""
    )

    hardfix_tag: EnumProperty(
        name="归档分类",
        description="难以修复并归档时写入报告的分类 Tag",
        items=HARDFIX_TAG_ITEMS,
        default='HARD_TO_FIX'
    )
    
    # 上次导出的状态
    last_exported_status: EnumProperty(
        name="上次导出状态",
        items=[
            ('UNMARKED', "未标记", ""),
            ('COMPLETED', "可修复", ""),
            ('FIXED', "已修复", ""),
            ('NO_ACTION', "无需修复", ""),
            ('QUESTIONABLE', "存疑", ""),
            ('UNFIXABLE', "bad", ""),
            ('HARD', "难以修复", ""),
            ('PARTS', "零件", ""),
        ],
        default='UNMARKED'
    )

# 用户配置属性
class MeshyAutoGLBSettings(PropertyGroup):
    operator_name: StringProperty(
        name="操作者姓名",
        description="当前操作者的姓名",
        default="",
        options={'SKIP_SAVE'}  # 不保存此属性
    )
    
    # 添加小功能工具面板折叠状态属性
    utils_expanded: BoolProperty(
        name="小功能工具展开",
        description="小功能工具面板是否展开",
        default=False,
        options={'SKIP_SAVE'}  # 不保存此属性
    )
    
    source_directory: StringProperty(
        name="源目录",
        description="包含GLB模型的源目录",
        default="",
        subtype='DIR_PATH',
        options={'SKIP_SAVE'}  # 不保存此属性
    )
    
    output_directory: StringProperty(
        name="输出目录",
        description="导出GLB模型的目录",
        default="",
        subtype='DIR_PATH',
        options={'SKIP_SAVE'}  # 不保存此属性
    )
    
    # 当前模型索引
    current_model_index: IntProperty(
        name="当前模型索引",
        description="当前选中的模型索引",
        default=0,
        options={'SKIP_SAVE'}  # 不保存此属性
    )
    
    # 上次导出路径
    last_export_path: StringProperty(
        name="上次导出路径",
        description="上次导出的路径",
        default="",
        options={'SKIP_SAVE'}  # 不保存此属性
    )

    last_audit_path: StringProperty(
        name="上次缺漏核对路径",
        description="最近一次导出缺漏核对CSV路径",
        default="",
        options={'SKIP_SAVE'}
    )

    # 进度自动保存
    auto_save_progress: BoolProperty(
        name="自动保存进度",
        description="自动保存处理进度",
        default=True,
        options={'SKIP_SAVE'}  # 不保存此属性
    )

    normal_export_mode: EnumProperty(
        name="法线导出",
        description="控制 GLB 导出时是否写入 normals；自动模式会跟随源 GLB 是否包含 normals",
        items=[
            ('AUTO', "自动", "源文件有 normals 则导出；源文件无 normals 则不导出，避免 manifold 文件被拆顶点"),
            ('OFF', "不导出", "始终不导出 normals，优先保持拓扑连通"),
            ('ON', "导出", "始终导出 normals，优先保持当前显示法线"),
        ],
        default='AUTO',
        options={'SKIP_SAVE'}
    )

    texcoord_export_mode: EnumProperty(
        name="UV导出",
        description="控制 GLB 导出时是否写入 UV/TEXCOORD；自动模式会跟随源 GLB 是否包含 UV",
        items=[
            ('AUTO', "自动", "源文件有 UV 则导出；源文件无 UV 则不导出，避免布尔辅助物体引入 UV"),
            ('OFF', "不导出", "始终不导出 UV/TEXCOORD"),
            ('ON', "导出", "始终导出 UV/TEXCOORD"),
        ],
        default='AUTO',
        options={'SKIP_SAVE'}
    )
    
    # 进度缓存目录
    progress_cache_dir: StringProperty(
        name="进度缓存目录",
        description="保存进度缓存的目录",
        default="",  # 将使用源目录作为进度缓存目录
        subtype='DIR_PATH',
        options={'SKIP_SAVE'}  # 不保存此属性
    )

    local_fallback_directory: StringProperty(
        name="本地回退目录",
        description="NAS 或共享目录不可写时，导出与进度优先回退到这里",
        default="",
        subtype='DIR_PATH',
        options={'SKIP_SAVE'}
    )

    resolved_output_directory: StringProperty(
        name="实际输出目录",
        description="当前实际使用的输出目录",
        default="",
        options={'SKIP_SAVE'}
    )

    resolved_progress_directory: StringProperty(
        name="实际进度目录",
        description="当前实际使用的进度目录",
        default="",
        options={'SKIP_SAVE'}
    )

    last_path_warning: StringProperty(
        name="路径告警",
        description="最近一次路径回退或权限告警",
        default="",
        options={'SKIP_SAVE'}
    )
    
    # 最后保存时间
    last_save_time: StringProperty(
        name="最后保存时间",
        description="最后一次保存进度的时间",
        default="",
        options={'SKIP_SAVE'}  # 不保存此属性
    )
    
    # 预览图属性
    preview_image: StringProperty(
        name="当前预览图",
        default="",
        options={'SKIP_SAVE'}  # 不保存此属性
    )
    
    # 状态标记颜色
    completed_color: StringProperty(
        name="完成标记颜色",
        default="0.2, 0.8, 0.2, 1.0",  # 绿色
        description="完成状态的指示颜色"
    )
    
    questionable_color: StringProperty(
        name="存疑标记颜色",
        default="0.9, 0.7, 0.0, 1.0",  # 黄色
        description="存疑状态的指示颜色"
    )
    
    unfixable_color: StringProperty(
        name="无法修复标记颜色",
        default="0.8, 0.2, 0.2, 1.0",  # 红色
        description="无法修复状态的指示颜色"
    )
    
    # 显示导出历史
    show_export_history: BoolProperty(
        name="显示导出历史",
        description="在界面中显示模型的导出历史记录",
        default=False
    )
    
    # 获取进度文件路径
    def get_progress_filepath(self):
        return resolve_progress_filepath(self)

    def save_runtime_state(self):
        state_path = runtime_state_filepath()
        state_dir = os.path.dirname(state_path)
        try:
            os.makedirs(state_dir, exist_ok=True)
            data = {
                "src": self.source_directory or "",
                "out": self.output_directory or "",
                "fallback": self.local_fallback_directory or "",
                "op": self.operator_name or "",
                "idx": int(self.current_model_index),
                "normal_mode": self.normal_export_mode,
                "texcoord_mode": self.texcoord_export_mode,
            }
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
            return True
        except Exception as e:
            print(f"保存运行态失败: {e}")
            return False

    def load_runtime_state(self):
        state_path = runtime_state_filepath()
        if not os.path.exists(state_path):
            return False
        try:
            with open(state_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.source_directory = data.get("src", "") or ""
            saved_output_directory = data.get("out", "") or ""
            source_dir = _normalized_dir(self.source_directory)
            saved_output_dir = _normalized_dir(saved_output_directory)
            legacy_auto_output_dir = os.path.dirname(source_dir) if source_dir else ""
            if source_dir and saved_output_dir == legacy_auto_output_dir:
                self.output_directory = source_dir
            else:
                self.output_directory = saved_output_directory
            self.local_fallback_directory = data.get("fallback", "") or ""
            self.operator_name = data.get("op", "") or ""
            self.current_model_index = int(data.get("idx", 0) or 0)
            normal_mode = data.get("normal_mode", "AUTO")
            self.normal_export_mode = normal_mode if normal_mode in {'AUTO', 'OFF', 'ON'} else 'AUTO'
            texcoord_mode = data.get("texcoord_mode", "AUTO")
            self.texcoord_export_mode = texcoord_mode if texcoord_mode in {'AUTO', 'OFF', 'ON'} else 'AUTO'
            return True
        except Exception as e:
            print(f"加载运行态失败: {e}")
            return False
    
    # 检测源目录中是否已有进度文件，并尝试获取用户名
    def detect_existing_progress(self):
        progress_filepath = self.get_progress_filepath()
        
        # 检查父目录是否存在
        progress_dir = os.path.dirname(progress_filepath)
        if not os.path.exists(progress_dir):
            return None
            
        # 检查进度文件是否存在
        if not os.path.exists(progress_filepath):
            return None
            
        # 从进度文件中读取用户名
        try:
            with open(progress_filepath, 'r', encoding='utf-8') as f:
                progress_data = json.load(f)
                # 适配新旧字段名：新格式用"op"，旧格式用"operator_name"
                return progress_data.get("op", progress_data.get("operator_name", None))
        except Exception as e:
            print(f"读取进度文件出错: {e}")
            return None
    
    # 保存进度信息（优化后的版本）
    def save_progress(self, context, force_full=False):
        import datetime
        
        # 如果没有启用自动保存，则不保存
        if not self.auto_save_progress and not force_full:
            return False
            
        # 如果没有设置操作者姓名或源目录，则不保存
        if not self.operator_name or not self.source_directory:
            return False
            
        # 获取进度文件路径
        progress_filepath = self.get_progress_filepath()
        if not progress_filepath:
            return False
            
        # 创建进度文件目录（如果不存在）
        progress_dir = os.path.dirname(progress_filepath)
        os.makedirs(progress_dir, exist_ok=True)
        
        # 获取当前时间
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.last_save_time = current_time
        
        # 模型：名称、路径、状态，以及导出历史 / 上次导出状态（写入 progress.json 以便重开文件后自动恢复）
        models_data = []
        for i, model in enumerate(context.scene.meshy_models):
            models_data.append({
                "n": model.name,
                "p": model.path,
                "s": model.status,
                "h": model.export_history or "",
                "lx": model.last_exported_status,
                "t": model.hardfix_tag,
                "r": model.hardfix_reason or "",
            })
        
        # 构建极简进度数据
        progress_data = {
            "op": self.operator_name,
            "idx": self.current_model_index,
            "time": current_time,
            "models": models_data
        }
        
        # 可选信息，如果已设置才包含
        if self.source_directory:
            progress_data["src"] = self.source_directory
            
        if self.output_directory:
            progress_data["out"] = self.output_directory

        if self.local_fallback_directory:
            progress_data["fallback"] = self.local_fallback_directory
            
        if self.last_export_path:
            progress_data["lst"] = self.last_export_path
        
        # 保存到JSON文件 - 极简格式
        try:
            with open(progress_filepath, 'w', encoding='utf-8') as f:
                json.dump(progress_data, f, ensure_ascii=False, separators=(',', ':'))
            self.save_runtime_state()
            return True
        except Exception as e:
            print(f"保存进度失败: {str(e)}")
            return False
    
    # 加载进度信息（匹配优化后的格式）
    def load_progress(self, context):
        # 如果没有设置源目录，则不加载
        if not self.source_directory:
            return False
            
        # 获取进度文件路径
        progress_filepath = self.get_progress_filepath()
        if not progress_filepath:
            return False
            
        # 检查进度文件是否存在
        if not os.path.exists(progress_filepath):
            return False
            
        # 从JSON文件加载数据
        try:
            with open(progress_filepath, 'r', encoding='utf-8') as f:
                progress_data = json.load(f)
                
            # 检查并更新操作者姓名（如果未设置）
            stored_operator = progress_data.get("op", progress_data.get("operator_name", ""))
            if stored_operator and not self.operator_name:
                self.operator_name = stored_operator
            
            # 恢复设置 - 适配新旧字段名
            saved_output_directory = progress_data.get("out", progress_data.get("output_directory", ""))
            source_dir = _normalized_dir(self.source_directory)
            saved_output_dir = _normalized_dir(saved_output_directory)
            legacy_auto_output_dir = os.path.dirname(source_dir) if source_dir else ""
            if source_dir and saved_output_dir == legacy_auto_output_dir:
                self.output_directory = source_dir
            else:
                self.output_directory = saved_output_directory
            self.local_fallback_directory = progress_data.get("fallback", progress_data.get("local_fallback_directory", ""))
            self.current_model_index = progress_data.get("idx", progress_data.get("current_model_index", 0))
            self.last_export_path = progress_data.get("lst", progress_data.get("last_export_path", ""))
            self.last_save_time = progress_data.get("time", progress_data.get("last_save_time", ""))
            
            # 恢复模型列表和状态
            models_data = progress_data.get("models", [])
            
            # 清空当前模型列表
            context.scene.meshy_models.clear()
            
            # 添加模型到列表
            for model_data in models_data:
                model = context.scene.meshy_models.add()
                model.name = model_data.get("n", model_data.get("name", ""))
                model.path = model_data.get("p", model_data.get("path", ""))
                model.status = _coerce_meshy_model_status(
                    model_data.get("s", model_data.get("status", "UNMARKED"))
                )
                model.export_history = model_data.get("h", model_data.get("export_history", "")) or ""
                model.hardfix_tag = _coerce_hardfix_tag(
                    model_data.get("t", model_data.get("hardfix_tag", "HARD_TO_FIX"))
                )
                model.hardfix_reason = model_data.get("r", model_data.get("hardfix_reason", "")) or ""
                model.last_exported_status = _coerce_meshy_model_status(
                    model_data.get("lx", model_data.get("last_exported_status", "UNMARKED")),
                    "UNMARKED",
                )

                # 无进度内历史时，从分类目录侧车 JSON 恢复（兼容旧版仅写侧车）
                try:
                    model_status = model.status
                    hist_empty = not (model.export_history or "").strip()
                    if (
                        hist_empty
                        and model_status not in ['UNMARKED', 'COMPLETED']
                    ):
                        op_suffix = (self.operator_name or "unknown").replace("/", "_").replace("\\", "_")
                        status_folders = {
                            'NO_ACTION': f"NoLogo_{op_suffix}",
                            'FIXED': f"Fixed_{op_suffix}",
                            'QUESTIONABLE': f"Questionable_{op_suffix}",
                            'UNFIXABLE': f"Bad_{op_suffix}",
                            'HARD': f"HardFix_{op_suffix}",
                            'PARTS': f"Parts_{op_suffix}",
                        }
                        legacy_folder_lists = {
                            'NO_ACTION': (f"Good_{op_suffix}", f"NoAction_{op_suffix}", f"good_{op_suffix}"),
                            'UNFIXABLE': (f"Unfixable_{op_suffix}", f"bad_{op_suffix}"),
                            'HARD': (f"Hard_{op_suffix}",),
                        }
                        
                        if self.output_directory and model_status in status_folders:
                            try_dirs = [status_folders[model_status]]
                            try_dirs.extend(legacy_folder_lists.get(model_status, ()))
                            for folder_key in try_dirs:
                                json_dir = os.path.join(self.output_directory, folder_key)
                                json_file = os.path.join(json_dir, f"{model.name}.json")
                                if os.path.exists(json_file):
                                    with open(json_file, 'r', encoding='utf-8') as jf:
                                        json_data = json.load(jf)
                                        if "export_history" in json_data:
                                            model.export_history = json_data["export_history"]
                                        if "status" in json_data:
                                            model.last_exported_status = _coerce_meshy_model_status(
                                                json_data["status"], "UNMARKED"
                                            )
                                        if "hardfix_tag" in json_data:
                                            model.hardfix_tag = _coerce_hardfix_tag(json_data["hardfix_tag"])
                                        if "hardfix_reason" in json_data:
                                            model.hardfix_reason = json_data["hardfix_reason"] or ""
                                    break
                except Exception as e:
                    print(f"无法恢复模型附加信息: {str(e)}")
                
            return True
        except Exception as e:
            print(f"加载进度失败: {str(e)}")
            return False
            
    # 清除进度信息
    def clear_progress(self, context):
        # 如果没有设置源目录，则不清除
        if not self.source_directory:
            return False
            
        # 获取进度文件路径
        progress_filepath = self.get_progress_filepath()
        if not progress_filepath:
            return False
            
        # 检查进度文件是否存在
        if not os.path.exists(progress_filepath):
            return False
            
        # 删除进度文件
        try:
            os.remove(progress_filepath)
            return True
        except Exception as e:
            print(f"清除进度失败: {str(e)}")
            return False
