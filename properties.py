import bpy
import os
import json
import tempfile
from bpy.props import (StringProperty, IntProperty, BoolProperty, 
                       EnumProperty, CollectionProperty, PointerProperty)
from bpy.types import PropertyGroup

def _normalized_dir(path):
    if not path:
        return ""
    return os.path.normpath(os.path.abspath(bpy.path.abspath(path)))


def _runtime_cache_root():
    base = bpy.utils.user_resource('CONFIG') or tempfile.gettempdir()
    return os.path.join(base, "meshy_automodel_runtime")


def runtime_state_filepath():
    return os.path.join(_runtime_cache_root(), "session_state.json")


MAX_SOURCE_DIRECTORY_HISTORY = 12


def _coerce_source_directory_list(raw_paths, existing_only=False):
    paths = []
    seen = set()
    for raw_path in raw_paths or []:
        if isinstance(raw_path, dict):
            raw_path = raw_path.get("path", "")
        if not raw_path:
            continue
        try:
            path = _normalized_dir(raw_path)
        except Exception:
            continue
        if existing_only and not os.path.isdir(path):
            continue
        key = os.path.realpath(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths[:MAX_SOURCE_DIRECTORY_HISTORY]


def _source_directory_enum_items(self, context):
    paths = self.get_source_directories(existing_only=True)
    if not paths:
        return [('NONE', "无可切换输入源", "请先设置源目录")]

    items = []
    for idx, path in enumerate(paths):
        basename = os.path.basename(os.path.normpath(path)) or path
        label = f"当前: {basename}" if path == _normalized_dir(self.source_directory) else basename
        items.append((f"SRC_{idx}", label, path, 'FILE_FOLDER', idx))
    return items


_MESHY_MODEL_STATUS_IDS = frozenset({
    'UNMARKED', 'COMPLETED', 'NO_ACTION', 'QUESTIONABLE',
    'UNFIXABLE', 'HARD', 'PARTS', 'NOR_ERROR', 'COMBO_ASSET',
})


def _coerce_meshy_model_status(value, default='UNMARKED'):
    if isinstance(value, str) and value in _MESHY_MODEL_STATUS_IDS:
        return value
    return default


# 模型项目类
class ModelItem(PropertyGroup):
    name: StringProperty(name="名称")
    path: StringProperty(name="路径")
    preview_path: StringProperty(name="预览图路径")
    source_type: EnumProperty(
        name="来源类型",
        items=[
            ('SINGLE_FILE', "单体文件", "单个 GLB/USDZ 文件"),
            ('MULTI_PART_FOLDER', "多体目录", "包含多个部件文件的目录"),
        ],
        default='SINGLE_FILE',
    )
    part_count: IntProperty(name="部件数量", default=0, min=0)
    status: EnumProperty(
        name="状态",
        items=[
            ('UNMARKED', "未标记", "尚未进行处理"),
            ('COMPLETED', "可修复", "已修复并完成"),
            ('NO_ACTION', "good", "无需进行任何处理"),
            ('QUESTIONABLE', "存疑", "有问题，待确认"),
            ('UNFIXABLE', "bad", "无法修复的问题"),
            ('HARD', "hard", "hard 分类"),
            ('PARTS', "零件", "判定为零件"),
            ('NOR_ERROR', "nor-error", "nor-error 分类"),
            ('COMBO_ASSET', "组合资产", "组合资产分类"),
        ],
        default='UNMARKED'
    )
    
    # 导出历史记录
    export_history: StringProperty(name="导出历史")
    
    # 上次导出的状态
    last_exported_status: EnumProperty(
        name="上次导出状态",
        items=[
            ('UNMARKED', "未标记", ""),
            ('COMPLETED', "可修复", ""),
            ('NO_ACTION', "good", ""),
            ('QUESTIONABLE', "存疑", ""),
            ('UNFIXABLE', "bad", ""),
            ('HARD', "hard", ""),
            ('PARTS', "零件", ""),
            ('NOR_ERROR', "nor-error", ""),
            ('COMBO_ASSET', "组合资产", ""),
        ],
        default='UNMARKED'
    )

# 用户配置属性
class MeshyAutoModelSettings(PropertyGroup):
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
        description="包含GLB/USDZ模型的源目录",
        default="",
        subtype='DIR_PATH',
        options={'SKIP_SAVE'}  # 不保存此属性
    )

    source_directories_json: StringProperty(
        name="输入源历史",
        description="最近使用的输入源目录列表",
        default="[]",
        options={'SKIP_SAVE'}
    )

    source_directory_choice: EnumProperty(
        name="输入源",
        description="选择已记录的输入源目录",
        items=_source_directory_enum_items,
        options={'SKIP_SAVE'}
    )
    
    output_directory: StringProperty(
        name="输出目录",
        description="导出GLB/USDZ模型的目录",
        default="",
        subtype='DIR_PATH',
        options={'SKIP_SAVE'}  # 不保存此属性
    )

    output_format: EnumProperty(
        name="导出格式",
        description="导出模型文件的格式；默认沿用GLB版本",
        items=[
            ('GLB', "GLB", "按原GLB流程导出.glb"),
            ('USDZ', "USDZ", "导出.usdz，状态流转仍沿用GLB版本"),
        ],
        default='GLB',
        options={'SKIP_SAVE'}
    )

    export_animations: BoolProperty(
        name="导出动画",
        description="导出时包含骨骼动作动画；关闭则只保留骨骼绑定数据（skin）",
        default=False,
        options={'SKIP_SAVE'}
    )

    source_mode: EnumProperty(
        name="输入模式",
        description="控制源目录按单体 GLB/USDZ 文件还是多体子目录刷新任务",
        items=[
            ('SINGLE_FILE', "单 GLB/USDZ", "源目录下每个 GLB/USDZ 文件作为一个任务"),
            ('MULTI_PART_FOLDER', "多体目录", "源目录下每个包含部件文件的子文件夹作为一个任务"),
        ],
        default='SINGLE_FILE',
        options={'SKIP_SAVE'}
    )

    mark_mode: EnumProperty(
        name="打标模式",
        description="控制 good/bad 等状态按钮处理整模还是当前选择",
        items=[
            ('WHOLE_MODEL', "整体模型", "打标签时处理当前模型全部对象"),
            ('SELECTED_OBJECTS', "仅选中对象", "打标签时只处理当前选中的对象"),
        ],
        default='WHOLE_MODEL',
        options={'SKIP_SAVE'}
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
    
    # 进度自动保存
    auto_save_progress: BoolProperty(
        name="自动保存进度",
        description="自动保存处理进度",
        default=True,
        options={'SKIP_SAVE'}  # 不保存此属性
    )
    
    # 进度缓存目录
    progress_cache_dir: StringProperty(
        name="进度缓存目录",
        description="保存进度缓存的目录",
        default="",  # 将使用源目录作为进度缓存目录
        subtype='DIR_PATH',
        options={'SKIP_SAVE'}  # 不保存此属性
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
        # 如果没有设置源目录，则返回空
        if not self.source_directory:
            return ""
            
        # 使用源目录作为进度缓存目录
        progress_dir = os.path.join(self.source_directory, ".progress")
        
        # 构建进度文件名：progress.json - 每个源目录只有一个进度文件
        progress_filename = "progress.json"
        return os.path.join(progress_dir, progress_filename)

    def get_source_directories(self, include_current=True, existing_only=False):
        raw_paths = []
        if include_current and self.source_directory:
            raw_paths.append(self.source_directory)
        try:
            stored_paths = json.loads(self.source_directories_json or "[]")
            if isinstance(stored_paths, list):
                raw_paths.extend(stored_paths)
        except Exception:
            pass
        return _coerce_source_directory_list(raw_paths, existing_only=existing_only)

    def set_source_directories(self, paths):
        source_paths = _coerce_source_directory_list(paths)
        self.source_directories_json = json.dumps(
            source_paths,
            ensure_ascii=False,
            separators=(',', ':'),
        )
        self.source_directory_choice = "SRC_0" if source_paths else "NONE"

    def remember_source_directory(self, path=None):
        path = path or self.source_directory
        if not path:
            return
        source_paths = [path]
        source_paths.extend(self.get_source_directories(include_current=False))
        self.set_source_directories(source_paths)

    def resolve_source_directory_choice(self, choice=None):
        choice = choice or self.source_directory_choice
        if not choice or choice == "NONE":
            return ""
        if not choice.startswith("SRC_"):
            return ""
        try:
            idx = int(choice.split("_", 1)[1])
        except Exception:
            return ""
        paths = self.get_source_directories(existing_only=True)
        if idx < 0 or idx >= len(paths):
            return ""
        return paths[idx]

    def save_runtime_state(self):
        state_path = runtime_state_filepath()
        try:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            source_paths = self.get_source_directories()
            data = {
                "src": self.source_directory or "",
                "sources": source_paths,
                "out": self.output_directory or "",
                "op": self.operator_name or "",
                "idx": int(self.current_model_index),
                "lst": self.last_export_path or "",
                "fmt": self.output_format or "GLB",
                "mark": self.mark_mode or "WHOLE_MODEL",
                "src_mode": self.source_mode or "SINGLE_FILE",
                "anim": bool(self.export_animations),
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
            stored_sources = data.get("sources", [])
            if not isinstance(stored_sources, list):
                stored_sources = []
            source_candidates = [data.get("src", "") or ""]
            source_candidates.extend(stored_sources)
            source_paths = _coerce_source_directory_list(source_candidates)
            existing_source_paths = _coerce_source_directory_list(source_paths, existing_only=True)
            if not existing_source_paths:
                return False
            source_directory = existing_source_paths[0]
            self.source_directory = source_directory
            self.set_source_directories(source_paths)
            self.output_directory = data.get("out", "") or ""
            self.operator_name = data.get("op", "") or ""
            self.current_model_index = int(data.get("idx", 0) or 0)
            self.last_export_path = data.get("lst", "") or ""
            saved_format = data.get("fmt", "GLB")
            self.output_format = saved_format if saved_format in {'GLB', 'USDZ'} else 'GLB'
            saved_mark_mode = data.get("mark", "WHOLE_MODEL")
            self.mark_mode = saved_mark_mode if saved_mark_mode in {'WHOLE_MODEL', 'SELECTED_OBJECTS'} else 'WHOLE_MODEL'
            saved_source_mode = data.get("src_mode", "SINGLE_FILE")
            self.source_mode = saved_source_mode if saved_source_mode in {'SINGLE_FILE', 'MULTI_PART_FOLDER'} else 'SINGLE_FILE'
            self.export_animations = bool(data.get("anim", False))
            return bool(self.source_directory)
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
                "t": model.source_type,
                "pc": int(model.part_count),
                "pv": model.preview_path or "",
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
            
        if self.last_export_path:
            progress_data["lst"] = self.last_export_path

        if self.output_format:
            progress_data["fmt"] = self.output_format

        if self.mark_mode:
            progress_data["mark"] = self.mark_mode

        if self.source_mode:
            progress_data["src_mode"] = self.source_mode

        progress_data["anim"] = bool(self.export_animations)

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
            self.output_directory = progress_data.get("out", progress_data.get("output_directory", ""))
            self.current_model_index = progress_data.get("idx", progress_data.get("current_model_index", 0))
            self.last_export_path = progress_data.get("lst", progress_data.get("last_export_path", ""))
            self.last_save_time = progress_data.get("time", progress_data.get("last_save_time", ""))
            saved_format = progress_data.get("fmt", progress_data.get("output_format", "GLB"))
            self.output_format = saved_format if saved_format in {'GLB', 'USDZ'} else 'GLB'
            saved_mark_mode = progress_data.get("mark", progress_data.get("mark_mode", "WHOLE_MODEL"))
            self.mark_mode = saved_mark_mode if saved_mark_mode in {'WHOLE_MODEL', 'SELECTED_OBJECTS'} else 'WHOLE_MODEL'
            saved_source_mode = progress_data.get("src_mode", progress_data.get("source_mode", "SINGLE_FILE"))
            self.source_mode = saved_source_mode if saved_source_mode in {'SINGLE_FILE', 'MULTI_PART_FOLDER'} else 'SINGLE_FILE'
            self.export_animations = bool(progress_data.get("anim", False))
            
            # 恢复模型列表和状态
            models_data = progress_data.get("models", [])
            
            # 清空当前模型列表
            context.scene.meshy_models.clear()
            
            # 添加模型到列表
            for model_data in models_data:
                model = context.scene.meshy_models.add()
                model.name = model_data.get("n", model_data.get("name", ""))
                model.path = model_data.get("p", model_data.get("path", ""))
                source_type = model_data.get("t", model_data.get("source_type", "SINGLE_FILE"))
                model.source_type = source_type if source_type in {'SINGLE_FILE', 'MULTI_PART_FOLDER'} else 'SINGLE_FILE'
                try:
                    model.part_count = int(model_data.get("pc", model_data.get("part_count", 0)) or 0)
                except Exception:
                    model.part_count = 0
                model.preview_path = model_data.get("pv", model_data.get("preview_path", "")) or ""
                model.status = _coerce_meshy_model_status(
                    model_data.get("s", model_data.get("status", "UNMARKED"))
                )
                model.export_history = model_data.get("h", model_data.get("export_history", "")) or ""
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
                        op_suffix = self.operator_name or "unknown"
                        status_folders = {
                            'NO_ACTION': f"Good_{op_suffix}",
                            'QUESTIONABLE': f"Questionable_{op_suffix}",
                            'UNFIXABLE': f"Bad_{op_suffix}",
                            'HARD': f"Hard_{op_suffix}",
                            'PARTS': f"Parts_{op_suffix}",
                            'NOR_ERROR': f"NorError_{op_suffix}",
                            'COMBO_ASSET': f"ComboAsset_{op_suffix}",
                        }
                        legacy_folder_lists = {
                            'NO_ACTION': (f"NoAction_{op_suffix}", f"good_{op_suffix}"),
                            'UNFIXABLE': (f"Unfixable_{op_suffix}", f"bad_{op_suffix}"),
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
