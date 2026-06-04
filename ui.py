import bpy
from bpy.types import Panel
import os
from . import bl_info
import re


def _draw_source_controls(layout, settings):
    box = layout.box()
    header = box.row()
    header.label(text="输入源", icon='FILE_FOLDER')

    if settings.source_directory:
        row = box.row()
        row.label(text=settings.source_directory)

    row = box.row(align=True)
    source_paths = settings.get_source_directories(existing_only=True)
    if len(source_paths) > 1:
        row.prop(settings, "source_directory_choice", text="")
        row.operator("meshy.switch_source_directory", text="", icon='FILE_REFRESH')
    elif source_paths:
        row.label(text=os.path.basename(os.path.normpath(source_paths[0])) or source_paths[0])
    row.operator("meshy.set_source_directory", text="", icon='FILEBROWSER')


# 主面板
class MESHY_PT_MainPanel(Panel):
    bl_label = "Meshy-AutoModel"
    bl_idname = "MESHY_PT_main_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Meshy-AutoModel'
    
    def draw(self, context):
        layout = self.layout
        settings = context.scene.meshy_settings
        
        # 直接写死版本号，确保显示正确
        version = "4.6.6"
        
        row = layout.row()
        row.label(text=f"版本: {version}", icon='PLUGIN')

        _draw_source_controls(layout, settings)
        
        # 检查是否已设置必要信息 - 先检查源目录
        if not settings.source_directory:
            box = layout.box()
            box.label(text="欢迎使用 Meshy-AutoModel", icon='INFO')
            box.label(text="请先设置源目录")
            box.operator("meshy.set_source_directory")
            return
        
        # 再检查用户名
        if not settings.operator_name:
            box = layout.box()
            box.label(text="请设置操作者姓名")
            box.operator("meshy.set_operator_name")
            return
        
        # 顶部区域 - 操作者信息和设置
        box = layout.box()
        row = box.row()
        row.label(text=f"操作者: {settings.operator_name}")
        row.operator("meshy.set_operator_name", text="", icon='PREFERENCES')
        
        # 顶部区域 - 模型信息
        models = context.scene.meshy_models
        if not models or len(models) == 0:
            box = layout.box()
            box.label(text="未找到模型，请设置或刷新源目录")
            row = box.row()
            row.operator("meshy.set_source_directory", text="设置源目录", icon='FILEBROWSER')
            row.operator("meshy.switch_source_directory", text="切换输入源", icon='FILE_REFRESH')
            row.operator("meshy.refresh_model_list", text="刷新", icon='FILE_REFRESH')
            return
        
        # 确保当前索引有效
        model_count = len(models)
        if settings.current_model_index >= model_count:
            settings.current_model_index = model_count - 1
        
        # 获取当前模型
        current_model = models[settings.current_model_index]
        
        # 模型信息区域 - 显示完整路径而不是名称
        box = layout.box()
        row = box.row()
        row.label(text="当前模型路径:")
        # 使用路径而不是名称
        path_text = current_model.path
        row = box.row()
        row.label(text=path_text)
        
        ui_status_labels = {
            'UNMARKED': "未标记",
            'COMPLETED': "可修复",
            'NO_ACTION': "good",
            'QUESTIONABLE': "存疑",
            'UNFIXABLE': "bad",
            'HARD': "hard",
            'PARTS': "零件",
        }
        
        status_icons = {
            'UNMARKED': 'QUESTION',
            'COMPLETED': 'CHECKMARK',
            'NO_ACTION': 'FILE_TICK',
            'QUESTIONABLE': 'ERROR',
            'UNFIXABLE': 'CANCEL',
            'HARD': 'MOD_PHYSICS',
            'PARTS': 'MESH_CUBE',
        }
        status_colors = {
            'UNMARKED': (0.5, 0.5, 0.5),
            'COMPLETED': (0.2, 0.8, 0.2),
            'NO_ACTION': (0.0, 0.7, 0.9),
            'QUESTIONABLE': (0.9, 0.7, 0.0),
            'UNFIXABLE': (0.8, 0.2, 0.2),
            'HARD': (0.7, 0.3, 0.9),
            'PARTS': (0.6, 0.6, 0.6),
        }
        
        # 创建一个状态指示器
        status_box = layout.box()
        row = status_box.row()
        row.label(text="当前状态:")
        # 使用图标和颜色指示状态，使用UI专用的标签
        st = current_model.status
        row.label(
            text=ui_status_labels.get(st, st),
            icon=status_icons.get(st, 'DOT')
        )
        
        # 显示上次导出状态（如果有）
        if current_model.last_exported_status:
            row = status_box.row()
            les = current_model.last_exported_status
            row.label(text=f"上次导出状态: {ui_status_labels.get(les, les)}")

        mark_mode_labels = {
            'WHOLE_MODEL': "整体模型",
            'SELECTED_OBJECTS': "仅选中对象",
        }
        row = status_box.row()
        row.label(text=f"打标模式: {mark_mode_labels.get(settings.mark_mode, settings.mark_mode)}")
        
        # 调整UI顺序，先显示状态标记按钮
        
        # 状态标记按钮
        status_box = layout.box()
        status_box.label(text="状态标记:", icon='CHECKMARK')
        row = status_box.row(align=True)
        row.prop(settings, "mark_mode", text="", expand=True)
        
        row = status_box.row(align=True)
        op = row.operator("meshy.mark_status", text="good", icon='FILE_TICK')
        op.status = 'NO_ACTION'
        op = row.operator("meshy.mark_status", text="存疑", icon='ERROR')
        op.status = 'QUESTIONABLE'
        op = row.operator("meshy.mark_status", text="bad", icon='CANCEL')
        op.status = 'UNFIXABLE'
        op = row.operator("meshy.mark_status", text="hard", icon='MOD_PHYSICS')
        op.status = 'HARD'
        
        row = status_box.row(align=True)
        row.scale_y = 1.5
        row.enabled = current_model.status != 'COMPLETED'
        op = row.operator("meshy.mark_status", text="可修复", icon='CHECKMARK')
        op.status = 'COMPLETED'
        
        # 模型组织与导出部分
        box = layout.box()
        box.label(text="模型组织与导出", icon='EXPORT')
        row = box.row(align=True)
        row.prop(settings, "output_format", expand=True)
        hint = box.row()
        hint.scale_y = 0.85
        hint.label(text="可修复：先「按组/导出所有」到 Completed，再点分类移动文件", icon='INFO')
        
        # 创建父节点按钮
        row = box.row()
        row.scale_y = 1.2
        row.operator("meshy.create_parent_node", text="快速创建组", icon='OBJECT_DATA')
        
        # 导出按钮
        row = box.row(align=True)
        row.scale_y = 1.5  # 使按钮更大
        
        # 按组导出操作
        export_op = row.operator("meshy.export_model", text="按组导出", icon='GROUP')
        export_op.export_mode = 'TOP_LEVEL'
        
        # 导出所有操作
        export_all_op = row.operator("meshy.export_model", text="导出所有", icon='EXPORT')
        export_all_op.export_mode = 'ALL_GROUPS'
        
        # 显示最后一次导出路径
        if settings.last_export_path:
            box.label(text=f"上次导出: {os.path.basename(settings.last_export_path)}", icon='FILE_TICK')
        
        if not context.scene.objects:
            row = box.row()
            row.label(text="提示: 场景中无对象，请先导入模型", icon='INFO')
        
        # 底部区域 - 模型导航，箭头更大
        nav_box = layout.box()
        nav_box.label(text="导航控制:", icon='TRACKING')
        
        # 导航信息行 - 显示当前位置
        info_row = nav_box.row()
        info_row.alignment = 'CENTER'  # 居中显示
        info_row.scale_y = 1.2  # 稍微增大
        info_row.label(text=f"当前位置: {settings.current_model_index + 1}/{model_count}", icon='VIEWZOOM')
        
        # 导航按钮行：未标记禁下一项；可修复时禁上一项但允许下一项
        btn_row = nav_box.row(align=True)
        btn_row.scale_y = 2.5
        st_nav = current_model.status

        prev_btn = btn_row.row(align=True)
        prev_btn.scale_x = 3.0
        prev_btn.enabled = st_nav != 'COMPLETED'
        prev_btn.operator("meshy.previous_model", text="", icon='TRIA_LEFT')

        spacer = btn_row.row()
        spacer.scale_x = 0.5
        spacer.label(text="")

        next_btn = btn_row.row(align=True)
        next_btn.scale_x = 3.0
        next_btn.enabled = st_nav != 'UNMARKED'
        next_btn.operator("meshy.next_model", text="", icon='TRIA_RIGHT')

        if st_nav == 'COMPLETED':
            hint_nav = nav_box.row()
            hint_nav.scale_y = 0.9
            hint_nav.label(text="可修复中：可切换下一项继续标记；完成修复后请导出并点分类", icon='INFO')
        elif st_nav == 'UNMARKED':
            hint_nav = nav_box.row()
            hint_nav.scale_y = 0.9
            hint_nav.label(text="未标记：可返回上一项；请先打标签后再进下一项", icon='INFO')
        
        # 底部区域 - 刷新按钮
        row = layout.row()
        row.operator("meshy.refresh_model_list", text="重新导入当前模型", icon='FILE_REFRESH')
        
        # 获取设置
        settings = context.scene.meshy_settings
        
        # 小功能工具折叠面板
        utils_box = layout.box()
        header_row = utils_box.row()
        header_row.alignment = 'LEFT'
        
        # 折叠箭头按钮
        expand_icon = 'TRIA_DOWN' if settings.utils_expanded else 'TRIA_RIGHT'
        header_row.prop(settings, "utils_expanded", text="小功能工具", 
                        icon=expand_icon, icon_only=False, emboss=False)
        
        # 根据折叠状态显示工具内容
        if settings.utils_expanded:
            # 直接从MESHY_PT_UtilsPanel中复制功能
            # 添加去除Alpha按钮
            row = utils_box.row()
            row.scale_y = 1.2  # 稍微增大按钮
            row.operator("meshy.remove_alpha", text="去除Alpha", icon='MATERIAL')
            
            # 添加清理数据块按钮
            row = utils_box.row()
            row.scale_y = 1.2  # 稍微增大按钮
            row.operator("meshy.purge_unused_data", text="清理数据块", icon='TRASH')
            
            # 添加选中描边物体按钮
            row = utils_box.row()
            row.scale_y = 1.2  # 稍微增大按钮
            row.operator("meshy.select_outline_meshes", text="选中描边物体", icon='OUTLINER_OB_MESH')
            
            # 添加清除空组按钮
            row = utils_box.row()
            row.scale_y = 1.2  # 稍微增大按钮
            row.operator("meshy.clean_empty_groups", text="清除空组", icon='GROUP')
        
        # 添加模型旋转控制区域 - 直接在主界面显示
        box = layout.box()
        box.label(text="模型旋转控制", icon='DRIVER_ROTATIONAL_DIFFERENCE')
        
        # 简化为两列三行布局
        # X轴旋转控制
        row = box.row(align=True)
        
        # X轴90°按钮
        op = row.operator("meshy.rotate_model", text="X轴 90°")
        op.axis = 'X'
        op.angle = 90.0
        
        # X轴180°按钮
        op = row.operator("meshy.rotate_model", text="X轴 180°")
        op.axis = 'X'
        op.angle = 180.0
        
        # Y轴旋转控制
        row = box.row(align=True)
        
        # Y轴90°按钮
        op = row.operator("meshy.rotate_model", text="Y轴 90°")
        op.axis = 'Y'
        op.angle = 90.0
        
        # Y轴180°按钮
        op = row.operator("meshy.rotate_model", text="Y轴 180°")
        op.axis = 'Y'
        op.angle = 180.0
        
        # Z轴旋转控制
        row = box.row(align=True)
        
        # Z轴90°按钮
        op = row.operator("meshy.rotate_model", text="Z轴 90°")
        op.axis = 'Z'
        op.angle = 90.0
        
        # Z轴180°按钮
        op = row.operator("meshy.rotate_model", text="Z轴 180°")
        op.axis = 'Z'
        op.angle = 180.0
        
        # 最后添加进度管理下拉菜单，确保它在界面最底部
        layout.popover("MESHY_PT_progress_panel", text="进度管理", icon='TIME')

# 小功能工具子面板
class MESHY_PT_UtilsPanel(Panel):
    bl_label = "小功能工具"
    bl_idname = "MESHY_PT_utils_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Meshy-AutoModel'
    bl_options = {'INSTANCED'}
    
    def draw(self, context):
        layout = self.layout
        
        # 添加去除Alpha按钮
        row = layout.row()
        row.scale_y = 1.2  # 稍微增大按钮
        row.operator("meshy.remove_alpha", text="去除Alpha", icon='MATERIAL')
        
        # 添加清理数据块按钮
        row = layout.row()
        row.scale_y = 1.2  # 稍微增大按钮
        row.operator("meshy.purge_unused_data", text="清理数据块", icon='TRASH')
        
        # 添加选中描边物体按钮
        row = layout.row()
        row.scale_y = 1.2  # 稍微增大按钮
        row.operator("meshy.select_outline_meshes", text="选中描边物体", icon='OUTLINER_OB_MESH')
        
        # 添加清除空组按钮
        row = layout.row()
        row.scale_y = 1.2  # 稍微增大按钮
        row.operator("meshy.clean_empty_groups", text="清除空组", icon='GROUP')

# 进度管理子面板
class MESHY_PT_ProgressPanel(Panel):
    bl_label = "进度管理"
    bl_idname = "MESHY_PT_progress_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Meshy-AutoModel'
    bl_options = {'INSTANCED'}
    
    def draw(self, context):
        layout = self.layout
        settings = context.scene.meshy_settings
        
        # 自动保存选项
        row = layout.row()
        row.prop(settings, "auto_save_progress", text="自动保存进度")
        
        row = layout.row(align=True)
        row.operator("meshy.save_progress", text="保存进度", icon='FILE_TICK')
        row.operator("meshy.load_progress", text="加载进度", icon='LOOP_BACK')
        
        # 显示最后保存时间
        if settings.last_save_time:
            row = layout.row()
            row.label(text=f"上次保存: {settings.last_save_time}", icon='TIME') 
