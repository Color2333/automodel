# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTIBILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

bl_info = {
    "name": "Meshy-AutoModel",
    "author": "Meshy Tech",
    "version": (4, 6, 5),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > Meshy-AutoModel",
    "description": "GLB/USDZ模型管理、修复和状态追踪工具",
    "category": "Import-Export",
    "doc_url": "",
    "tracker_url": "",
}

import bpy
import os
import glob
from bpy.props import StringProperty, IntProperty, EnumProperty, CollectionProperty, BoolProperty
from bpy.types import Panel, Operator, PropertyGroup, WindowManager, Scene, AddonPreferences
from bpy.utils import register_class, unregister_class
from bpy.app.handlers import persistent

from . import ui
from . import operators
from . import properties

# 全局变量和类定义
classes = (
    properties.ModelItem,
    properties.MeshyAutoModelSettings,
    operators.MESHY_OT_SetOperatorName,
    operators.MESHY_OT_SetSourceDirectory,
    operators.MESHY_OT_SwitchSourceDirectory,
    operators.MESHY_OT_ImportModel,
    operators.MESHY_OT_ExportModel,
    operators.MESHY_OT_MarkStatus,
    operators.MESHY_OT_PreviousModel,
    operators.MESHY_OT_NextModel,
    operators.MESHY_OT_KeyHandler,
    operators.MESHY_OT_RefreshModelList,
    operators.MESHY_OT_CreateParentNode,
    operators.MESHY_OT_SaveProgress,
    operators.MESHY_OT_LoadProgress,
    operators.MESHY_OT_ClearProgress,
    operators.MESHY_OT_RemoveAlpha,
    operators.MESHY_OT_PurgeUnusedData,
    operators.MESHY_OT_SelectOutlineMeshes,
    operators.MESHY_OT_CleanEmptyGroups,
    operators.MESHY_OT_RotateModel,
    ui.MESHY_PT_MainPanel,
    ui.MESHY_PT_UtilsPanel,
    ui.MESHY_PT_ProgressPanel,
)

# 添加快捷键
addon_keymaps = []

def register_keymaps():
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        km = kc.keymaps.new(name='3D View', space_type='VIEW_3D')
        
        # 左右箭头快捷键
        kmi = km.keymap_items.new(operators.MESHY_OT_KeyHandler.bl_idname, 'LEFT_ARROW', 'PRESS')
        kmi.properties.key = 'LEFT'
        addon_keymaps.append((km, kmi))
        
        kmi = km.keymap_items.new(operators.MESHY_OT_KeyHandler.bl_idname, 'RIGHT_ARROW', 'PRESS')
        kmi.properties.key = 'RIGHT'
        addon_keymaps.append((km, kmi))
        
        # 数字键快捷键
        kmi = km.keymap_items.new(operators.MESHY_OT_KeyHandler.bl_idname, 'ONE', 'PRESS')
        kmi.properties.key = '1'
        addon_keymaps.append((km, kmi))
        
        kmi = km.keymap_items.new(operators.MESHY_OT_KeyHandler.bl_idname, 'TWO', 'PRESS')
        kmi.properties.key = '2'
        addon_keymaps.append((km, kmi))
        
        kmi = km.keymap_items.new(operators.MESHY_OT_KeyHandler.bl_idname, 'THREE', 'PRESS')
        kmi.properties.key = '3'
        addon_keymaps.append((km, kmi))
        
        # 导出快捷键
        kmi = km.keymap_items.new(operators.MESHY_OT_ExportModel.bl_idname, 'E', 'PRESS', ctrl=True)
        addon_keymaps.append((km, kmi))

def unregister_keymaps():
    for km, kmi in addon_keymaps:
        km.keymap_items.remove(kmi)
    addon_keymaps.clear()

def register():
    # 注册类
    for cls in classes:
        bpy.utils.register_class(cls)
    
    # 注册属性
    bpy.types.Scene.meshy_models = bpy.props.CollectionProperty(type=properties.ModelItem)
    bpy.types.Scene.meshy_settings = bpy.props.PointerProperty(type=properties.MeshyAutoModelSettings)
    
    # 注册快捷键
    keymap = bpy.context.window_manager.keyconfigs.addon.keymaps.new(name='3D View', space_type='VIEW_3D')
    
    # Ctrl+E 导出当前模型
    kmi = keymap.keymap_items.new('meshy.key_handler', 'E', 'PRESS', ctrl=True)
    kmi.properties.key = 'EXPORT'
    
    # Ctrl+Left 上一个模型
    kmi = keymap.keymap_items.new('meshy.key_handler', 'LEFT_ARROW', 'PRESS', ctrl=True)
    kmi.properties.key = 'PREVIOUS'
    
    # Ctrl+Right 下一个模型
    kmi = keymap.keymap_items.new('meshy.key_handler', 'RIGHT_ARROW', 'PRESS', ctrl=True)
    kmi.properties.key = 'NEXT'
    
    # 存储快捷键以便后续注销
    addon_keymaps.append(keymap)
    
    # 尝试加载之前的进度
    @persistent
    def load_progress_handler(dummy):
        # 检查context.scene是否可用
        if bpy.context.scene is None:
            print("Meshy-AutoModel: 场景尚未准备好，延迟设置重置")
            return
            
        try:
            settings = bpy.context.scene.meshy_settings
            
            # 只保留一些基本设置
            if not hasattr(settings, "auto_save_progress"):
                settings.auto_save_progress = True

            restored = settings.load_runtime_state()
            if restored and settings.source_directory and os.path.exists(settings.source_directory):
                if settings.auto_save_progress and settings.load_progress(bpy.context):
                    try:
                        bpy.ops.meshy.import_model()
                    except Exception as import_error:
                        print(f"Meshy-AutoModel: 自动恢复当前模型失败: {import_error}")
                    print("Meshy-AutoModel: 已自动恢复上次处理进度")
                else:
                    try:
                        bpy.ops.meshy.refresh_model_list()
                        print("Meshy-AutoModel: 已恢复源目录并刷新模型列表")
                    except Exception as refresh_error:
                        print(f"Meshy-AutoModel: 刷新模型列表失败: {refresh_error}")
            else:
                settings.source_directory = ""
                settings.operator_name = ""
                settings.current_model_index = 0
                settings.output_format = 'GLB'
                if hasattr(bpy.context.scene, "meshy_models"):
                    bpy.context.scene.meshy_models.clear()
                print("Meshy-AutoModel: 未找到可恢复的会话状态")
        except Exception as e:
            print(f"Meshy-AutoModel: 重置设置时出错: {str(e)}")

    @persistent
    def meshy_save_checkpoint_handler(dummy):
        try:
            operators.save_active_scene_checkpoint_for_handlers()
        except Exception as e:
            print(f"Meshy-AutoModel: Ctrl+S同步拆分现场失败: {e}")
    
    # 注册加载进度处理器
    bpy.app.handlers.load_post.append(load_progress_handler)
    bpy.app.handlers.save_pre.append(meshy_save_checkpoint_handler)
    
    print("Meshy AutoModel 插件已注册")

def unregister():
    # 移除加载进度处理器
    for handler in bpy.app.handlers.load_post:
        if handler.__name__ == 'load_progress_handler':
            bpy.app.handlers.load_post.remove(handler)

    for handler in bpy.app.handlers.save_pre:
        if handler.__name__ == 'meshy_save_checkpoint_handler':
            bpy.app.handlers.save_pre.remove(handler)
    
    # 注销快捷键
    for keymap in addon_keymaps:
        bpy.context.window_manager.keyconfigs.addon.keymaps.remove(keymap)
    addon_keymaps.clear()
    
    # 注销属性
    del bpy.types.Scene.meshy_models
    del bpy.types.Scene.meshy_settings
    
    # 注销类
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    print("Meshy AutoModel 插件已注销")

if __name__ == "__main__":
    register()
