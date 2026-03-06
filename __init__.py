import bpy # type: ignore
from bpy.props import BoolProperty # type: ignore
from bpy.app.handlers import persistent # type: ignore
from mathutils import Vector # type: ignore
import numpy as np # type: ignore

from . import __package__

class AnimAutoOffsetPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    auto_key_override : BoolProperty (
            name="Auto keying override",
            description="When enabled 'Relative editing' mode will switch off 'Auto keying' and vice versa",
            default=False
        ) # # type: ignore

    def draw(self, context):
        self.layout.prop(self, 'auto_key_override')

def get_is_auto_key_override(context):
    return context.preferences.addons[__package__].preferences.auto_key_override

def _get_obj_fcurves(obj):
    action = obj.animation_data.action
    action_slot = obj.animation_data.action_slot
    if not action or not action_slot:
        return None
    # TODO: update to support layers in 5.0
    channelbag = action.layers[0].strips[0].channelbag(action_slot)
    if channelbag is None:
        return None
    return channelbag.fcurves

KP_ATTRS = {
    'co': 'select_control_point',
    'handle_left': 'select_left_handle',
    'handle_right': 'select_right_handle',
}

def transform_keyframe_points(fcurve, delta, only_selected=False):
    for attr, select_attr in KP_ATTRS.items():
        size = len(fcurve.keyframe_points)
        attr_vals = np.empty(size * 2)
        fcurve.keyframe_points.foreach_get(attr, attr_vals)
        if only_selected:
            selected = np.empty(size)
            fcurve.keyframe_points.foreach_get(select_attr, selected)
            where = np.zeros(size * 2, dtype=bool)
            where[1::2] = selected
        else:
            where = np.tile(np.array([False, True]), size)
        np.add(attr_vals, delta, out=attr_vals, where=where)
        fcurve.keyframe_points.foreach_set(attr, attr_vals)

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

def get_value_from_data_path(obj, data_path):
    # NOTE: can't use getattr or similar because of paths like pose.bone['Bone']
    if data_path.startswith('['): # dict-like syntax
        return eval('obj' + data_path)
    else:
        return eval('obj.' + data_path)

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

    obj_fcurves = _get_obj_fcurves(obj)
    if obj_fcurves is None:
        return

    fcurve_map = dict()
    for fcurve in obj_fcurves:
        fcurve_map.setdefault(fcurve.data_path, dict())[fcurve.array_index] = fcurve

    fcurves_pre_update = pre_update['fcurves']
    for data_path, val in fcurves_pre_update.items():
        fcurves = fcurve_map.get(data_path)
        if not fcurves:
            continue
        data = get_value_from_data_path(obj, data_path)
        deltas = vectorized(data) - vectorized(val)
        for i, delta in enumerate(deltas if is_iterable(deltas) else [deltas]):
            fcurve = fcurves.get(i)
            if not fcurve: # this index is not animated
                continue
            if delta == 0.0: # no change
                continue
            yield (fcurve, delta)

    del obj['pre_update_data']

def save_fcurves_data(obj, depsgraph):
    eval_obj = obj.evaluated_get(depsgraph)
    if eval_obj.animation_data is None:
        return

    obj_fcurves = _get_obj_fcurves(eval_obj)
    if obj_fcurves is None:
        return

    fcurves_pre_update = dict()
    for fcurve in obj_fcurves:
        if fcurve.data_path in fcurves_pre_update:
            continue
        data = get_value_from_data_path(eval_obj, fcurve.data_path)
        try:
            fcurves_pre_update[fcurve.data_path] = vectorized(data)
        except TypeError:
            pass # must be non-numeric data like a string/enum, skip

    pre_update = dict()
    pre_update['fcurves'] = fcurves_pre_update
    save_anim_attr(eval_obj, pre_update, 'action', 'name')
    save_anim_attr(eval_obj, pre_update, 'action_slot', 'identifier')
    obj['pre_update_data'] = pre_update

g_is_undo_redo_in_progress = False

def is_valid_update(update):
    return (update.id.id_type == 'OBJECT' and 
            (update.is_updated_geometry or # this fires for e.g. pose bone transform update
             update.is_updated_transform))

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
        if not is_valid_update(update):
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
        if not is_valid_update(update):
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

_msgbus_owner = object()

def _register_message_bus() -> None:
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.ToolSettings, "use_keyframe_insert_auto"),
        owner=_msgbus_owner,
        args=(),
        notify=_on_auto_key_change,
        options={"PERSISTENT"},
    )

def _unregister_message_bus() -> None:
    bpy.msgbus.clear_by_owner(_msgbus_owner)

@persistent
def post_load(none, other_none) -> None:
    _register_message_bus()

class DOPESHEET_PT_anim_offset_mode(bpy.types.Panel):
    bl_idname = "DOPESHEET_PT_anim_offset_mode"
    bl_label = "Relative Editing"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'HEADER'

    def draw(self, context):
        self.layout.prop(context.scene, 'anim_offset_mode_only_selected')

g_auto_key_change_active = False
g_anim_offset_mode_change_active = False

def _on_anim_offset_mode_change(scene, context):
    if not get_is_auto_key_override(context):
        return
    if g_auto_key_change_active:
        return

    # XXX: not cleared because message_bus will trigger the _on_auto_key_change() callback AFTER this one
    global g_anim_offset_mode_change_active
    g_anim_offset_mode_change_active = True

    tool_settings = scene.tool_settings
    if scene.use_anim_offset_mode:
        scene.use_keyframe_insert_auto_old = tool_settings.use_keyframe_insert_auto # save
        tool_settings.use_keyframe_insert_auto = False
    else:
        tool_settings.use_keyframe_insert_auto = scene.use_keyframe_insert_auto_old # restore

def _on_auto_key_change():
    if not get_is_auto_key_override(bpy.context):
        return

    global g_anim_offset_mode_change_active
    if g_anim_offset_mode_change_active:
        g_anim_offset_mode_change_active = False
        return

    global g_auto_key_change_active
    g_auto_key_change_active = True

    tool_settings = bpy.context.tool_settings
    scene = bpy.context.scene
    if tool_settings.use_keyframe_insert_auto:
        scene.use_anim_offset_mode = False

    g_auto_key_change_active = False

def draw_header(self, context):
    # st = context.space_data
    # if st.mode == 'TIMELINE': # don't show in timeline
    #     return

    row = self.layout.row(align=True)
    row.prop(context.scene, "use_anim_offset_mode", icon='CON_TRANSLIKE', emboss=True, text='')
    row.active = not context.scene.tool_settings.use_keyframe_insert_auto
    sub = row.row(align=True)
    sub.popover(
        panel=DOPESHEET_PT_anim_offset_mode.bl_idname,
        text="",
    )

def register():
    bpy.types.Scene.use_keyframe_insert_auto_old = BoolProperty () # internal

    bpy.types.Scene.use_anim_offset_mode = BoolProperty (
            name="Relative editing",
            description="Update all keyframe points relatively when the property value changes",
            default=False,
            update=_on_anim_offset_mode_change
        )
    bpy.types.Scene.anim_offset_mode_only_selected = BoolProperty (
            name="Affect only selected keyframes",
            description="Affect only selected keyframe points when Relative editing is enabled",
            default=False
        )
    
    bpy.utils.register_class(AnimAutoOffsetPreferences)
    bpy.utils.register_class(DOPESHEET_PT_anim_offset_mode)
    _register_message_bus()
    bpy.types.GRAPH_HT_header.append(draw_header)
    bpy.types.DOPESHEET_HT_header.append(draw_header)
    bpy.app.handlers.depsgraph_update_post.append(post_depsgraph_update)
    bpy.app.handlers.depsgraph_update_pre.append(pre_depsgraph_update)
    bpy.app.handlers.undo_post.append(post_redo_undo)
    bpy.app.handlers.redo_post.append(post_redo_undo)
    bpy.app.handlers.undo_pre.append(pre_redo_undo)
    bpy.app.handlers.redo_pre.append(pre_redo_undo)
    bpy.app.handlers.load_post.append(post_load)

def unregister():
    bpy.app.handlers.load_post.remove(post_load)
    bpy.app.handlers.redo_pre.remove(pre_redo_undo)
    bpy.app.handlers.undo_pre.remove(pre_redo_undo)
    bpy.app.handlers.redo_post.remove(post_redo_undo)
    bpy.app.handlers.undo_post.remove(post_redo_undo)
    bpy.app.handlers.depsgraph_update_pre.remove(pre_depsgraph_update)
    bpy.app.handlers.depsgraph_update_post.remove(post_depsgraph_update)
    bpy.types.DOPESHEET_HT_header.remove(draw_header)
    bpy.types.GRAPH_HT_header.remove(draw_header)
    _unregister_message_bus()
    bpy.utils.unregister_class(DOPESHEET_PT_anim_offset_mode)
    bpy.utils.unregister_class(AnimAutoOffsetPreferences)

    del bpy.types.Scene.anim_offset_mode_only_selected
    del bpy.types.Scene.use_anim_offset_mode
    del bpy.types.Scene.use_keyframe_insert_auto_old
