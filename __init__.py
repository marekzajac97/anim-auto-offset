import bpy
from bpy.props import BoolProperty # type: ignore
from bpy.app.handlers import persistent
from mathutils import Vector

def _get_obj_fcurves(obj):
    if obj.animation_data is None:
        return
    fcurves = obj.animation_data.action.fcurves
    for fcu in fcurves:
        yield fcu

def transform_keyframe_points(fcurve, delta):
    for kp in fcurve.keyframe_points:
        new_kp_co = kp.co[1] + delta[fcurve.array_index]
        kp.handle_right[1] += new_kp_co - kp.co[1]
        kp.handle_left[1] += new_kp_co - kp.co[1]
        kp.co[1] = new_kp_co

def get_fcurves_deltas(obj):
    deltas = list()
    pre_update = obj.get('fcurves_pre_update')
    if pre_update is None:
        return deltas
    for fcurve in _get_obj_fcurves(obj):
        val = Vector(pre_update[fcurve.data_path])
        data = eval('obj' + '.' + fcurve.data_path)
        delta = Vector(data) - val
        deltas.append((fcurve, delta))
    del obj['fcurves_pre_update']
    return deltas

def save_fcurves_data(obj, depsgraph):
    eval_obj = obj.evaluated_get(depsgraph)
    pre_update = dict()
    for fcurve in _get_obj_fcurves(obj):
        # NOTE: can't use getattr or similar because of paths like pose.bone['Bone']
        data = eval('eval_obj' + '.' + fcurve.data_path)
        pre_update[fcurve.data_path] = Vector(data)
    obj['fcurves_pre_update'] = pre_update

g_is_undo_redo_in_progress = False

@persistent
def post_depsgraph_update(scene):
    if not scene.use_anim_offset_mode:
        return
    if g_is_undo_redo_in_progress:
        return
    if scene.tool_settings.use_keyframe_insert_auto:
        return

    depsgraph = bpy.context.view_layer.depsgraph
    for obj in depsgraph.objects:
        obj = bpy.data.objects.get(obj.name)
        if not obj:
            continue
        for fcurve, delta in get_fcurves_deltas(obj):
            transform_keyframe_points(fcurve, delta)

@persistent
def pre_depsgraph_update(scene):
    if not scene.use_anim_offset_mode:
        return
    if g_is_undo_redo_in_progress:
        return
    depsgraph = bpy.context.view_layer.depsgraph
    for obj in depsgraph.objects:
        obj = bpy.data.objects.get(obj.name)
        if not obj:
            continue
        save_fcurves_data(obj, depsgraph)

@persistent
def post_redo_undo(scene):
    global g_is_undo_redo_in_progress
    g_is_undo_redo_in_progress = True
    bpy.context.evaluated_depsgraph_get() # trigger depsgraph update now
    g_is_undo_redo_in_progress = False

class DOPESHEET_OT_anim_offset_mode_activate(bpy.types.Operator):
    bl_idname = "anim_offset_mode.activate"
    bl_label = "Relative Editing"
    bl_description = "Update all keyframe points relatively when the property value changes"

    def execute(self, context):
        context.scene.use_anim_offset_mode = not context.scene.use_anim_offset_mode # switch
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        cls.poll_message_set("Cannot be used when Auto Keying is enabled")
        return not context.scene.tool_settings.use_keyframe_insert_auto

def menu_header(self, context):
    self.layout.operator(DOPESHEET_OT_anim_offset_mode_activate.bl_idname, icon='CON_TRANSLIKE',
                         emboss=True, depress=context.scene.use_anim_offset_mode, text='')

def register():
    bpy.types.Scene.use_anim_offset_mode = BoolProperty (
            name="Relative Editing",
            description="Update all keyframe points relatively when the property value changes",
            default=True
        )
    bpy.utils.register_class(DOPESHEET_OT_anim_offset_mode_activate)
    bpy.types.GRAPH_HT_header.append(menu_header)
    bpy.types.DOPESHEET_HT_header.append(menu_header)
    bpy.app.handlers.depsgraph_update_post.append(post_depsgraph_update)
    bpy.app.handlers.depsgraph_update_pre.append(pre_depsgraph_update)
    bpy.app.handlers.undo_post.append(post_redo_undo)
    bpy.app.handlers.redo_post.append(post_redo_undo)

def unregister():
    bpy.types.DOPESHEET_HT_header.remove(menu_header)
    bpy.types.GRAPH_HT_header.remove(menu_header)
    bpy.utils.unregister_class(DOPESHEET_OT_anim_offset_mode_activate)
    del bpy.types.Scene.use_anim_offset_mode

    bpy.app.handlers.depsgraph_update_post.remove(post_depsgraph_update)
    bpy.app.handlers.depsgraph_update_pre.remove(pre_depsgraph_update)
    bpy.app.handlers.undo_post.remove(post_redo_undo)
    bpy.app.handlers.redo_post.remove(post_redo_undo)
