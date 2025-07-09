import bpy # type: ignore
from bpy.props import BoolProperty # type: ignore
from bpy.app.handlers import persistent # type: ignore
from mathutils import Vector # type: ignore

def _get_obj_fcurves(obj, data_path=None):
    action = obj.animation_data.action
    action_slot = obj.animation_data.action_slot
    if not action or not action_slot:
        return
    # TODO: update to support layers in 5.0
    channelbag = action.layers[0].strips[0].channelbag(action_slot)
    if channelbag is None:
        return
    for fcurve in channelbag.fcurves:
        if data_path and fcurve.data_path != data_path:
            continue
        yield fcurve

def transform_keyframe_points(fcurve, delta, only_selected=False):
    for kp in fcurve.keyframe_points:
        if not only_selected or only_selected and kp.select_left_handle:
            kp.handle_left[1] += delta
        if not only_selected or only_selected and kp.select_right_handle:
            kp.handle_right[1] += delta
        if not only_selected or only_selected and kp.select_control_point:
            kp.co[1] += delta

def is_iterable(o):
    return hasattr(o, "__len__")

def vectorized(data):
    return Vector(data) if is_iterable(data) else data

def has_anim_attr_changed(obj, pre_update, attr, sub_attr):
    attr_val = getattr(obj.animation_data, attr)
    if attr_val:
        if pre_update[attr] != getattr(attr_val, sub_attr):
            return True
    elif pre_update[attr]:
        return True
    return False

def save_anim_attr(obj, pre_update, attr, sub_attr):
    attr_val = getattr(obj.animation_data, attr)
    pre_update[attr] = getattr(attr_val, sub_attr) if attr_val else ''

def get_fcurves_deltas(obj):
    if obj.animation_data is None:
        return

    pre_update = obj.get('pre_update_data')
    if pre_update is None:
        return

    # skip on action or action_slot change
    if has_anim_attr_changed(obj, pre_update, 'action', 'name'):
        return
    if has_anim_attr_changed(obj, pre_update, 'action_slot', 'identifier'):
        return

    fcurves_pre_update = pre_update['fcurves']
    for data_path, val in fcurves_pre_update.items():
        data = eval('obj.' + data_path)
        delta = vectorized(data) - vectorized(val)
        for fcurve in _get_obj_fcurves(obj, data_path):
            fcurve_delta = delta[fcurve.array_index] if is_iterable(delta) else delta
            if fcurve_delta == 0.0: # no change
                continue
            yield (fcurve, fcurve_delta)
    del obj['pre_update_data']

def save_fcurves_data(obj, depsgraph):
    eval_obj = obj.evaluated_get(depsgraph)
    if eval_obj.animation_data is None:
        return

    fcurves_pre_update = dict()
    for fcurve in _get_obj_fcurves(obj):
        if fcurve.data_path in fcurves_pre_update:
            continue
        # NOTE: can't use getattr or similar because of paths like pose.bone['Bone']
        data = eval('eval_obj.' + fcurve.data_path)
        try:
            fcurves_pre_update[fcurve.data_path] = vectorized(data)
        except TypeError:
            pass # skip

    pre_update = dict()
    pre_update['fcurves'] = fcurves_pre_update
    save_anim_attr(eval_obj, pre_update, 'action', 'name')
    save_anim_attr(eval_obj, pre_update, 'action_slot', 'identifier')
    obj['pre_update_data'] = pre_update

g_is_undo_redo_in_progress = False

@persistent
def post_depsgraph_update(scene):
    if not scene.use_anim_offset_mode:
        return
    if g_is_undo_redo_in_progress:
        return
    if scene.tool_settings.use_keyframe_insert_auto:
        return

    only_selected = scene.anim_offset_mode_only_selected

    depsgraph = bpy.context.view_layer.depsgraph
    for update in depsgraph.updates:
        if (update.id.id_type != 'OBJECT' or not
            (update.is_updated_geometry or # this fires for e.g. pose bone transform update
             update.is_updated_transform)):
            continue
        obj = bpy.data.objects.get(update.id.name)
        if not obj:
            continue
        for fcurve, delta in get_fcurves_deltas(obj):
            transform_keyframe_points(fcurve, delta, only_selected=only_selected)

@persistent
def pre_depsgraph_update(scene):
    if not scene.use_anim_offset_mode:
        return
    if g_is_undo_redo_in_progress:
        return
    depsgraph = bpy.context.view_layer.depsgraph
    for update in depsgraph.updates:
        if (update.id.id_type != 'OBJECT' or not
            (update.is_updated_geometry or
             update.is_updated_transform)):
            continue
        obj = bpy.data.objects.get(update.id.name)
        if not obj:
            continue
        save_fcurves_data(obj, depsgraph)

@persistent
def pre_redo_undo(scene):
    global g_is_undo_redo_in_progress
    g_is_undo_redo_in_progress = True

@persistent
def post_redo_undo(scene):
    global g_is_undo_redo_in_progress
    # trigger depsgraph update now (it would be done after post_redo/undo callback)
    # but this time skip fcurves update (as it has already been done in depsgraph update before post_redo/undo callback)
    bpy.context.evaluated_depsgraph_get()
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


class DOPESHEET_PT_anim_offset_mode(bpy.types.Panel):
    bl_idname = "DOPESHEET_PT_anim_offset_mode"
    bl_label = "Relative Editing"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'HEADER'

    def draw(self, context):
        self.layout.prop(context.scene, 'anim_offset_mode_only_selected')


def draw_header(self, context):
    st = context.space_data
    if st.mode == 'TIMELINE': # don't show in timeline
        return

    row = self.layout.row(align=True)
    row.operator(DOPESHEET_OT_anim_offset_mode_activate.bl_idname, icon='CON_TRANSLIKE',
                 emboss=True, depress=context.scene.use_anim_offset_mode, text='')
    sub = row.row(align=True)
    sub.popover(
        panel=DOPESHEET_PT_anim_offset_mode.bl_idname,
        text="",
    )

def register():
    bpy.types.Scene.use_anim_offset_mode = BoolProperty (
            name="Relative Editing",
            description="Update keyframe points relatively when the property value changes",
            default=False
        )
    bpy.types.Scene.anim_offset_mode_only_selected = BoolProperty (
            name="Affect only selected keyframes",
            description="Affect only selected keyframe points when Relative Editing is enabled",
            default=False
        )

    bpy.utils.register_class(DOPESHEET_OT_anim_offset_mode_activate)
    bpy.utils.register_class(DOPESHEET_PT_anim_offset_mode)
    bpy.types.GRAPH_HT_header.append(draw_header)
    bpy.types.DOPESHEET_HT_header.append(draw_header)
    bpy.app.handlers.depsgraph_update_post.append(post_depsgraph_update)
    bpy.app.handlers.depsgraph_update_pre.append(pre_depsgraph_update)
    bpy.app.handlers.undo_post.append(post_redo_undo)
    bpy.app.handlers.redo_post.append(post_redo_undo)
    bpy.app.handlers.undo_pre.append(pre_redo_undo)
    bpy.app.handlers.redo_pre.append(pre_redo_undo)

def unregister():
    bpy.app.handlers.redo_pre.remove(pre_redo_undo)
    bpy.app.handlers.undo_pre.remove(pre_redo_undo)
    bpy.app.handlers.redo_post.remove(post_redo_undo)
    bpy.app.handlers.undo_post.remove(post_redo_undo)
    bpy.app.handlers.depsgraph_update_pre.remove(pre_depsgraph_update)
    bpy.app.handlers.depsgraph_update_post.remove(post_depsgraph_update)

    bpy.types.DOPESHEET_HT_header.remove(draw_header)
    bpy.types.GRAPH_HT_header.remove(draw_header)

    bpy.utils.unregister_class(DOPESHEET_PT_anim_offset_mode)
    bpy.utils.unregister_class(DOPESHEET_OT_anim_offset_mode_activate)

    del bpy.types.Scene.anim_offset_mode_only_selected
    del bpy.types.Scene.use_anim_offset_mode
