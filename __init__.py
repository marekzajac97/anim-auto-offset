import bpy # type: ignore
from bpy.props import BoolProperty, IntVectorProperty, EnumProperty, IntProperty # type: ignore
from bpy.app.handlers import persistent # type: ignore
from mathutils import Vector # type: ignore
import numpy as np # type: ignore
import math
import time
import gpu
from gpu_extras.batch import batch_for_shader

from . import __package__

class AnimAutoOffsetPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    auto_key_override : BoolProperty (
            name="Auto keying override",
            description="When enabled 'Offset editing' mode will switch off 'Auto keying' and vice versa",
            default=False
        ) # # type: ignore

    easing_overlay_gradient : BoolProperty (
            name="Gradient easing overlay",
            description="Show the easing window overlay opacity as a gradient matching the easing curve",
            default=False
        ) # # type: ignore

    transform_wheel_easing_window : BoolProperty (
            name="Wheel adjusts easing window while transforming",
            description="While moving, rotating or scaling (G/R/S) in the 3D Viewport the mouse wheel\n"
                        "adjusts the easing window instead of the proportional editing size",
            default=True
        ) # # type: ignore

    easing_window_wheel_in_editors : BoolProperty (
            name="Wheel modifiers in the editors",
            description="In the Dope Sheet and the Graph Editor, hold a number key and scroll the\n"
                        "mouse wheel: 3 adjusts the easing window, 4 picks the curve, 5 picks the\n"
                        "easing mode. A plain scroll keeps zooming the view",
            default=True
        ) # # type: ignore

    def draw(self, context):
        self.layout.prop(self, 'auto_key_override')
        self.layout.prop(self, 'easing_overlay_gradient')
        self.layout.prop(self, 'transform_wheel_easing_window')
        self.layout.prop(self, 'easing_window_wheel_in_editors')

def _get_prefs(context=None):
    """AddonPreferences, or None when the add-on is not enabled as an add-on.

    Accessing preferences.addons[__package__] directly raises KeyError when the
    module was imported/registered by hand (a re-register during a reload, a test
    harness, or a stale module left in RAM), which used to make the scene
    callbacks throw on every depsgraph update.
    """
    prefs = getattr(context or bpy.context, 'preferences', None)
    addons = getattr(prefs, 'addons', None)
    if addons is None:
        return None
    entry = addons.get(__package__) if hasattr(addons, 'get') else None
    if entry is None:
        return None
    return getattr(entry, 'preferences', None)


def _pref(name, default=None):
    prefs = _get_prefs()
    return getattr(prefs, name, default) if prefs is not None else default

def get_is_auto_key_override(context=None):
    return bool(_pref('auto_key_override', False))

def get_is_easing_overlay_gradient(context=None):
    return bool(_pref('easing_overlay_gradient', False))

def get_is_transform_wheel_easing_window(context=None):
    return bool(_pref('transform_wheel_easing_window', True))

def get_is_easing_window_wheel_in_editors(context=None):
    return bool(_pref('easing_window_wheel_in_editors', True))

def _get_obj_fcurves(obj):
    """F-curves of *obj*, or None when it has nothing to animate.

    obj may be None or have no animation_data (the panel predicates run on the
    active object, which can be unanimated or not exist at all).
    """
    anim = getattr(obj, 'animation_data', None) if obj is not None else None
    if anim is None:
        return None
    action = anim.action
    action_slot = anim.action_slot
    if not action or not action_slot:
        return None
    layers = action.layers
    if len(layers) == 0:
        return None
    strips = layers[0].strips
    if len(strips) == 0:
        return None
    channelbag = strips[0].channelbag(action_slot)
    if channelbag is None:
        return None
    return channelbag.fcurves

# --- Auto select keyframes by current frame ---------------------------------

g_auto_select_active = False

# state of the "1" / "2" keys, used as modifiers for the mouse wheel
g_auto_select_hold = {'left': False, 'right': False}

def _get_auto_select_objects(context):
    objects = []
    for obj in context.selected_objects:
        if obj.animation_data and obj.animation_data.action:
            objects.append(obj)
    if not objects:
        obj = context.active_object
        if obj and obj.animation_data and obj.animation_data.action:
            objects.append(obj)
    return objects

def _find_anchor_index(frames, frame):
    """Index of the keyframe closest to *frame*."""
    size = len(frames)
    idx = int(np.searchsorted(frames, frame))
    if idx <= 0:
        return 0
    if idx >= size:
        return size - 1
    # ties go to the earlier keyframe
    if (frame - frames[idx - 1]) <= (frames[idx] - frame):
        return idx - 1
    return idx

def _auto_select_fcurve(fcurve, frame, left, right):
    size = len(fcurve.keyframe_points)
    if size == 0:
        return False

    co = np.empty(size * 2)
    fcurve.keyframe_points.foreach_get('co', co)
    frames = co[0::2]

    anchor = _find_anchor_index(frames, frame)
    start = max(0, anchor - left)
    end = min(size - 1, anchor + right)

    selection = np.zeros(size, dtype=bool)
    selection[start:end + 1] = True

    current = np.empty(size, dtype=bool)
    fcurve.keyframe_points.foreach_get('select_control_point', current)
    if np.array_equal(current, selection):
        return False

    fcurve.keyframe_points.foreach_set('select_control_point', selection)
    fcurve.select = True
    return True

def _tag_anim_editors_redraw(context):
    screen = getattr(context, 'screen', None)
    if not screen:
        return
    for area in screen.areas:
        if area.type in _ANIM_EDITOR_TYPES:
            area.tag_redraw()

def apply_auto_select(context=None):
    """Select the keyframes around the current frame of every animated object.

    Hold 1 and scroll the wheel to extend the selection to the left, hold 2 to
    extend it to the right and hold both keys to extend both sides.
    """
    global g_auto_select_active
    if g_auto_select_active:
        return
    if context is None:
        context = bpy.context
    scene = getattr(context, 'scene', None)
    if scene is None:
        return
    if not (scene.use_anim_offset_mode and scene.anim_offset_auto_select_by_frame):
        return

    frame = scene.frame_current
    left, right = scene.anim_offset_auto_select_counts

    changed = False
    g_auto_select_active = True
    try:
        for obj in _get_auto_select_objects(context):
            fcurves = _get_obj_fcurves(obj)
            if not fcurves:
                continue
            for fcurve in fcurves:
                if _auto_select_fcurve(fcurve, frame, int(left), int(right)):
                    changed = True
    finally:
        g_auto_select_active = False

    if changed:
        _tag_anim_editors_redraw(context)

def adjust_auto_select(scene, left_delta, right_delta):
    """Extend/shrink the automatic selection by a number of keyframes per side."""
    counts = scene.anim_offset_auto_select_counts
    left = max(0, min(9999, int(counts[0]) + left_delta))
    right = max(0, min(9999, int(counts[1]) + right_delta))
    if (left, right) == (int(counts[0]), int(counts[1])):
        return False
    scene.anim_offset_auto_select_counts = (left, right)
    return True

def adjust_easing_window(scene, index, step):
    """Change one side of the easing window by *step* keyframes."""
    sizes = scene.anim_offset_easing_window_sizes
    index = 0 if index == 0 else 1
    new_value = max(0, int(sizes[index]) + step)
    if new_value == sizes[index]:
        return False
    sizes[index] = new_value
    return True

KP_ATTRS = {
    'co': 'select_control_point',
    'handle_left': 'select_left_handle',
    'handle_right': 'select_right_handle',
}

EASINGS = {
    'linear': {
        'in':     lambda t: t,
        'out':    lambda t: t,
        'in_out': lambda t: t,
    },
    'sine': {
        # ease-in starts slow, so it is the 1 - cos() branch; sin() is the
        # fast-starting ease-out. These two were the wrong way round, which
        # made "Sine + In" behave like an ease-out and vice versa.
        'in':     lambda t: 1 - math.cos(t * math.pi / 2),
        'out':    lambda t: math.sin(t * math.pi / 2),
        'in_out': lambda t: -(math.cos(t * math.pi) - 1) / 2,
    },
    'quadratic': {
        'in':     lambda t: t * t,
        'out':    lambda t: 1 - (1 - t) * (1 - t),
        'in_out': lambda t: 2 * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 2 / 2,
    },
    'cubic': {
        'in':     lambda t: t * t * t,
        'out':    lambda t: 1 - (1 - t) ** 3,
        'in_out': lambda t: 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2,
    },
    'quartic': {
        'in':     lambda t: t * t * t * t,
        'out':    lambda t: 1 - (1 - t) ** 4,
        'in_out': lambda t: 8 * t * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 4 / 2,
    },
    'quintic': {
        'in':     lambda t: t * t * t * t * t,
        'out':    lambda t: 1 - (1 - t) ** 5,
        'in_out': lambda t: 16 * t * t * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 5 / 2,
    },
    'exponential': {
        'in':     lambda t: 0.0 if t == 0 else math.pow(2, 10 * (t - 1)),
        'out':    lambda t: 1.0 if t == 1 else 1 - math.pow(2, -10 * t),
        'in_out': lambda t: 0.0 if t == 0 else 1.0 if t == 1 else math.pow(2, 10 * (2 * t - 1)) / 2 if t < 0.5 else 1 - math.pow(2, -10 * (2 * t - 1)) / 2,
    },
    'circular': {
        'in':     lambda t: 1 - math.sqrt(1 - t * t),
        'out':    lambda t: math.sqrt(1 - (1 - t) * (1 - t)),
        'in_out': lambda t: (1 - math.sqrt(1 - (2 * t) ** 2)) / 2 if t < 0.5 else (math.sqrt(1 - (-2 * t + 2) ** 2) + 1) / 2,
    },
}

EASING_FUNCTIONS = tuple(EASINGS)

def _get_easing_weights(fcurve, easing_window, easing_function, easing_mode):
    easing_window_B, easing_window_A = easing_window
    if easing_window_B <= 0 and easing_window_A <= 0:
        return None

    size = len(fcurve.keyframe_points)
    selected = np.empty(size, dtype=bool)
    fcurve.keyframe_points.foreach_get('select_control_point', selected)
    selected_indices = np.where(selected)[0]

    if len(selected_indices) == 0:
        return None

    if not (len(selected_indices) == 1 or np.all(np.diff(selected_indices) == 1)):
        return None

    first_sel = selected_indices[0]
    last_sel = selected_indices[-1]

    weights = np.zeros(size)
    weights[first_sel:last_sel + 1] = 1.0

    easing_fun = EASINGS[easing_function][easing_mode]

    def _ease(t):
        if t <= 0:
            return 0.0
        if t >= 1:
            return 1.0
        return easing_fun(t)

    # BEFORE selected range: weight fades from 0 (far edge) to ~1 (at selected boundary),
    # with t computed from actual frame positions so uneven keyframe spacing is respected.
    win_start = max(0, first_sel - easing_window_B)
    win_end = first_sel
    if win_start < win_end:
        far_frame = fcurve.keyframe_points[win_start].co[0]
        near_frame = fcurve.keyframe_points[first_sel].co[0]
        span = near_frame - far_frame
        if span > 0:
            for i in range(win_start, win_end):
                t = (fcurve.keyframe_points[i].co[0] - far_frame) / span
                weights[i] = _ease(t)

    # AFTER selected range: weight fades from ~1 (at selected boundary) to 0 (far edge).
    win_start = last_sel + 1
    win_end = min(size, last_sel + easing_window_A + 1)
    if win_start < win_end:
        near_frame = fcurve.keyframe_points[last_sel].co[0]
        far_frame = fcurve.keyframe_points[win_end - 1].co[0]
        span = far_frame - near_frame
        if span > 0:
            for i in range(win_start, win_end):
                t = (far_frame - fcurve.keyframe_points[i].co[0]) / span
                weights[i] = _ease(t)

    return weights

def transform_keyframe_points(fcurve, delta, only_selected=False,
                              easing_window=(0, 0), easing_function='linear',
                              easing_mode='in_out'):
    size = len(fcurve.keyframe_points)
    if size == 0:
        return
    easing_weights = None
    if only_selected:
        easing_weights = _get_easing_weights(fcurve, easing_window, easing_function, easing_mode)

    if easing_weights is not None:
        for attr, _select_attr in KP_ATTRS.items():
            attr_vals = np.empty(size * 2)
            fcurve.keyframe_points.foreach_get(attr, attr_vals)
            expanded_delta = np.zeros(size * 2)
            expanded_delta[1::2] = delta * easing_weights
            np.add(attr_vals, expanded_delta, out=attr_vals)
            fcurve.keyframe_points.foreach_set(attr, attr_vals)
        fcurve.update()
    else:
        for attr, select_attr in KP_ATTRS.items():
            attr_vals = np.empty(size * 2)
            fcurve.keyframe_points.foreach_get(attr, attr_vals)
            if only_selected:
                selected = np.empty(size, dtype=bool)
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

_pre_update_registry: dict[int, dict] = {}

def get_fcurves_deltas(obj):
    if obj.animation_data is None:
        return

    pre_update = _pre_update_registry.pop(obj.as_pointer(), None)
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
        try:
            data = get_value_from_data_path(obj, data_path)
        except (KeyError, AttributeError, IndexError):
            continue # stale F-Curve: its property no longer exists on the object
        deltas = vectorized(data) - vectorized(val)
        for i, delta in enumerate(deltas if is_iterable(deltas) else [deltas]):
            fcurve = fcurves.get(i)
            if not fcurve: # this index is not animated
                continue
            if delta == 0.0: # no change
                continue
            yield (fcurve, delta)

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
        try:
            data = get_value_from_data_path(eval_obj, fcurve.data_path)
        except (KeyError, AttributeError, IndexError):
            continue # stale F-Curve: its property no longer exists on the object
        try:
            fcurves_pre_update[fcurve.data_path] = vectorized(data)
        except TypeError:
            pass # must be non-numeric data like a string/enum, skip

    pre_update = dict()
    pre_update['fcurves'] = fcurves_pre_update
    save_anim_attr(eval_obj, pre_update, 'action', 'name')
    save_anim_attr(eval_obj, pre_update, 'action_slot', 'identifier')
    _pre_update_registry[obj.as_pointer()] = pre_update

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

    depsgraph = bpy.context.view_layer.depsgraph
    easing_window = tuple(scene.anim_offset_easing_window_sizes)
    for update in depsgraph.updates:
        if not is_valid_update(update):
            continue
        obj = bpy.data.objects.get(update.id.name)
        if not obj:
            continue

        for fcurve, delta in get_fcurves_deltas(obj):
            transform_keyframe_points(fcurve, delta,
                                      only_selected=scene.anim_offset_mode_only_selected,
                                      easing_window=easing_window,
                                      easing_function=scene.anim_offset_easing_function,
                                      easing_mode=scene.anim_offset_easing_mode)

    _pre_update_registry.clear()

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

def post_load(none, other_none) -> None:
    _register_message_bus()
    # a reload can drop the key up event that clears the wheel modifier
    _clear_hold_keys()
    _stop_hold3_overlay()


@persistent
def auto_select_frame_change(scene, depsgraph=None):
    if not (scene.use_anim_offset_mode and scene.anim_offset_auto_select_by_frame):
        return
    apply_auto_select(bpy.context)

def _on_auto_select_change(scene, context):
    """Toggle of the 'auto select' option: forget held keys and reselect."""
    if context is None:
        context = bpy.context
    g_auto_select_hold['left'] = False
    g_auto_select_hold['right'] = False
    apply_auto_select(context)

def _on_auto_select_counts_change(scene, context):
    """Only the numbers changed (e.g. by the wheel), keep the held keys."""
    if context is None:
        context = bpy.context
    apply_auto_select(context)

class DOPESHEET_PT_anim_offset_mode(bpy.types.Panel):
    bl_idname = "DOPESHEET_PT_anim_offset_mode"
    bl_label = "Offset Editing"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'HEADER'

    @staticmethod
    def _selected_keyframes_are_sequential(obj):
        fcurves = _get_obj_fcurves(obj)
        if not fcurves:
            return False

        is_seq = False
        for fcurve in fcurves:
            size = len(fcurve.keyframe_points)
            if size == 0:
                continue
            if not fcurve.select:
                continue

            selected = np.empty(size, dtype=bool)
            fcurve.keyframe_points.foreach_get('select_control_point', selected)
            indices = np.where(selected)[0]

            if len(indices) == 0:
                continue

            if not (len(indices) == 1 or np.all(np.diff(indices) == 1)):
                return False

            is_seq = True

        return is_seq

    @staticmethod
    def _selected_keyframes_are_non_consecutive(obj):
        fcurves = _get_obj_fcurves(obj)
        if not fcurves:
            return False

        for fcurve in fcurves:
            size = len(fcurve.keyframe_points)
            if size == 0:
                continue
            if not fcurve.select:
                continue

            selected = np.empty(size, dtype=bool)
            fcurve.keyframe_points.foreach_get('select_control_point', selected)
            indices = np.where(selected)[0]

            if len(indices) > 1 and not np.all(np.diff(indices) == 1):
                return True

        return False

    def draw(self, context):
        draw_offset_panel(self.layout, context)


def draw_offset_panel(layout, context):
    """Panel body shared by the header popover and the editor sidebars."""
    scene = context.scene
    row = layout.row(align=True)
    row.prop(scene, 'anim_offset_mode_only_selected')
    row.active = scene.use_anim_offset_mode

    row = layout.row(align=True)
    row.prop(scene, 'anim_offset_auto_select_by_frame')
    row.active = scene.use_anim_offset_mode

    if scene.anim_offset_auto_select_by_frame:
        row = layout.row(align=True)
        row.active = scene.use_anim_offset_mode
        row.prop(scene, 'anim_offset_auto_select_counts', index=0, text='Left')
        row.prop(scene, 'anim_offset_auto_select_counts', index=1, text='Right')
        hint = layout.row()
        hint.active = scene.use_anim_offset_mode
        hint.label(text='Hold 1 / 2 + Wheel', icon='INFO')

    if (scene.use_anim_offset_mode and scene.anim_offset_mode_only_selected
            and get_is_easing_window_wheel_in_editors(context)):
        hint = layout.row()
        hint.label(text='3: window   4: curve   5: mode', icon='INFO')

    only_sel = scene.anim_offset_mode_only_selected
    is_sequential = True
    obj = context.active_object
    obj_is_valid = obj and obj.animation_data

    if only_sel and obj_is_valid:
        is_sequential = DOPESHEET_PT_anim_offset_mode._selected_keyframes_are_sequential(obj)

    window = scene.anim_offset_easing_window_sizes
    has_easing_window = window[0] > 0 or window[1] > 0

    col = layout.column(align=True)
    col.active = only_sel and is_sequential
    col.label(text='Easing Settings:')
    row = col.row(align=True)
    if scene.anim_offset_use_asymmetric_window is True:
        row.prop(scene, 'anim_offset_easing_window_sizes', index=0, text='Before')
        _col = row.column()
        _col.prop(scene, 'anim_offset_easing_window_sizes', index=1, text='After')
    else:
        row.prop(scene, 'anim_offset_easing_window_sizes', index=0, text='Window')
    _col = row.column()
    _col.prop(scene, 'anim_offset_use_asymmetric_window', text='', toggle=True, icon='AREA_SWAP')
    _col.enabled = only_sel and is_sequential
    _col = row.column()
    _col.operator('anim.adjust_easing_window_drag', text='', icon='DRIVER_DISTANCE')
    _col.enabled = only_sel and is_sequential

    if (only_sel and not is_sequential and has_easing_window
            and obj_is_valid
            and DOPESHEET_PT_anim_offset_mode._selected_keyframes_are_non_consecutive(obj)):
        warn = col.column(align=True)
        warn.alert = True
        warn.label(text='Selected keyframes are not', icon='ERROR')
        warn.label(text='in consecutive order', icon='BLANK1')

    row = col.row(align=False)
    row.active = only_sel and is_sequential and has_easing_window
    _col = row.column()
    _col.label(text='Curve:')
    _col.prop_enum(scene, 'anim_offset_easing_function', 'linear', icon='IPO_LINEAR')
    _col.prop_enum(scene, 'anim_offset_easing_function', 'sine', icon='IPO_SINE')
    _col.prop_enum(scene, 'anim_offset_easing_function', 'quadratic', icon='IPO_QUAD')
    _col.prop_enum(scene, 'anim_offset_easing_function', 'cubic', icon='IPO_CUBIC')
    _col.prop_enum(scene, 'anim_offset_easing_function', 'quartic', icon='IPO_QUART')
    _col.prop_enum(scene, 'anim_offset_easing_function', 'quintic', icon='IPO_QUINT')
    _col.prop_enum(scene, 'anim_offset_easing_function', 'exponential', icon='IPO_EXPO')
    _col.prop_enum(scene, 'anim_offset_easing_function', 'circular', icon='IPO_CIRC')
    _col = row.column()
    _col.label(text='Mode:')
    _col.prop_enum(scene, 'anim_offset_easing_mode', 'in', icon='IPO_EASE_IN')
    _col.prop_enum(scene, 'anim_offset_easing_mode', 'out', icon='IPO_EASE_OUT')
    _col.prop_enum(scene, 'anim_offset_easing_mode', 'in_out', icon='IPO_EASE_IN_OUT')


# --- Sidebar panels --------------------------------------------------------
# The Timeline is a DOPESHEET_EDITOR area with mode == 'TIMELINE', so one panel
# with bl_space_type='DOPESHEET_EDITOR' shows up in both the Dope Sheet and the
# Timeline. The Graph Editor needs its own class because bl_space_type must match.
class ANIM_PT_offset_editing_dope(bpy.types.Panel):
    bl_idname = "ANIM_PT_offset_editing_dope"
    bl_label = "Offset Editing"
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Offset"

    def draw(self, context):
        draw_offset_panel(self.layout, context)


class ANIM_PT_offset_editing_graph(bpy.types.Panel):
    bl_idname = "ANIM_PT_offset_editing_graph"
    bl_label = "Offset Editing"
    bl_space_type = 'GRAPH_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Offset"

    def draw(self, context):
        draw_offset_panel(self.layout, context)

_ANIM_EDITOR_TYPES = {'GRAPH_EDITOR', 'DOPESHEET_EDITOR'}

def _get_anim_editor_view2d(context, space_type):
    region = context.region
    if (region and region.type == 'WINDOW' and context.area
            and context.area.type == space_type):
        return region.view2d, region
    for area in context.screen.areas:
        if area.type == space_type:
            for r in area.regions:
                if r.type == 'WINDOW':
                    return r.view2d, r
    return None, None

def _easing_band_frame_bounds(obj, windowB, windowA):
    """Frame range covered by the selected keys and by the easing window.

    Returns (min_sel, max_sel, min_window, max_window), or None when no channel
    has a usable selection.

    A channel with a single keyframe has no neighbour to blend with, so it
    cannot describe an easing window. Such channels are skipped: counting them
    folded the band onto their one key's frame. A rig usually has a few
    constant channels keyed only at the first frame, which pinned the left edge
    of both the selection and the window to that frame and flattened the
    "before" band to zero width.
    """
    fcurves = _get_obj_fcurves(obj)
    if not fcurves:
        return None

    min_sel_frame = float('inf')
    max_sel_frame = float('-inf')
    min_window_frame = float('inf')
    max_window_frame = float('-inf')
    found = False

    for fcurve in fcurves:
        if not fcurve.select:
            continue

        size = len(fcurve.keyframe_points)
        if size < 2:
            continue
        selected = np.empty(size, dtype=bool)
        fcurve.keyframe_points.foreach_get('select_control_point', selected)
        indices = np.where(selected)[0]
        if len(indices) == 0:
            continue
        if len(indices) > 1 and not np.all(np.diff(indices) == 1):
            continue

        found = True
        first_idx = int(indices[0])
        last_idx = int(indices[-1])
        first_frame = fcurve.keyframe_points[first_idx].co[0]
        last_frame = fcurve.keyframe_points[last_idx].co[0]

        min_sel_frame = min(min_sel_frame, first_frame)
        max_sel_frame = max(max_sel_frame, last_frame)

        ws_idx = max(0, min(size - 1, first_idx - windowB))
        we_idx = max(0, min(size - 1, last_idx + windowA))
        min_window_frame = min(min_window_frame, fcurve.keyframe_points[ws_idx].co[0])
        max_window_frame = max(max_window_frame, fcurve.keyframe_points[we_idx].co[0])

    if not found:
        return None
    return min_sel_frame, max_sel_frame, min_window_frame, max_window_frame


def _easing_weight(scene):
    """The ease(t) the offset itself uses, for the active curve and mode."""
    easing_fun = EASINGS[scene.anim_offset_easing_function][scene.anim_offset_easing_mode]

    def ease(t):
        if t <= 0:
            return 0.0
        if t >= 1:
            return 1.0
        return easing_fun(t)

    return ease


def _draw_shaped_band(shader, x0, x1, y_bottom, y_top, weight_at, color):
    """Vertical band whose opacity follows weight_at(u), u running 0 -> 1.

    Shaping the opacity by the easing curve is what makes the selected curve
    and mode visible in the overlay.
    """
    if x1 <= x0:
        return
    r, g, b, a = color
    segments = max(1, int(abs(x1 - x0) / 4))
    verts = []
    colors = []
    for i in range(segments + 1):
        u = i / segments
        x = x0 + (x1 - x0) * u
        alpha = a * max(0.0, min(1.0, weight_at(u)))
        verts.append((x, y_bottom))
        verts.append((x, y_top))
        colors.append((r, g, b, alpha))
        colors.append((r, g, b, alpha))
    batch = batch_for_shader(shader, 'TRI_STRIP', {"pos": verts, "color": colors})
    batch.draw(shader)


def draw_easing_overlay(space_type, force=False):
    context = bpy.context
    scene = context.scene
    if not scene.use_anim_offset_mode or not scene.anim_offset_mode_only_selected:
        return
    window = scene.anim_offset_easing_window_sizes
    windowB = max(0, window[0])
    windowA = max(0, window[1])

    view2d, region = _get_anim_editor_view2d(context, space_type)
    if not view2d:
        return

    obj = context.active_object
    if not obj or not obj.animation_data:
        return

    bounds = _easing_band_frame_bounds(obj, windowB, windowA)
    if bounds is None:
        return
    min_sel_frame, max_sel_frame, min_window_frame, max_window_frame = bounds

    y_bottom = region.y
    y_top = region.y + region.height

    x_win_start = view2d.view_to_region(min_window_frame, 0.0, clip=False)[0]
    x_win_end = view2d.view_to_region(max_window_frame, 0.0, clip=False)[0]
    x_sel_start = view2d.view_to_region(min_sel_frame, 0.0, clip=False)[0]
    x_sel_end = view2d.view_to_region(max_sel_frame, 0.0, clip=False)[0]

    gpu.state.blend_set('ALPHA')

    ease = _easing_weight(scene)
    band_color = (0.25, 0.75, 0.25, 0.30)
    before_band = windowB > 0 and x_sel_start > x_win_start
    after_band = windowA > 0 and x_win_end > x_sel_end

    if not get_is_easing_overlay_gradient(context):
        # A flat fill first so the extent of the window stays readable on its own.
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')

        def draw_rect(x0, x1, color):
            if x1 <= x0:
                return
            verts = [(x0, y_bottom), (x1, y_bottom), (x1, y_top), (x0, y_top)]
            batch = batch_for_shader(shader, 'TRI_FAN', {"pos": verts})
            shader.uniform_float("color", color)
            batch.draw(shader)

        if before_band:
            draw_rect(x_win_start, x_sel_start, (0.25, 0.75, 0.25, 0.10))
        draw_rect(x_sel_start, x_sel_end, (0.25, 0.75, 0.25, 0.18))
        if after_band:
            draw_rect(x_sel_end, x_win_end, (0.25, 0.75, 0.25, 0.10))

    # The curve profile goes on top of the fill in both overlay styles, so
    # picking another Curve or Mode always changes what is drawn. Without this
    # the flat overlay looked identical for in / out / in & out.
    smooth_shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    if before_band:
        _draw_shaped_band(smooth_shader, x_win_start, x_sel_start,
                          y_bottom, y_top, ease, band_color)
    _draw_shaped_band(smooth_shader, x_sel_start, x_sel_end,
                      y_bottom, y_top, lambda u: 1.0, band_color)
    if after_band:
        _draw_shaped_band(smooth_shader, x_sel_end, x_win_end,
                          y_bottom, y_top, lambda u: ease(1.0 - u), band_color)

    gpu.state.blend_set('NONE')

class ANIM_OT_auto_select_hold(bpy.types.Operator):
    bl_idname = "anim.auto_select_hold"
    bl_label = "Auto Selection Modifier Key"
    bl_description = "Remember whether the 1 or 2 key is held down for the auto selection wheel"
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        return bool(scene.use_anim_offset_mode and scene.anim_offset_auto_select_by_frame)

    def invoke(self, context, event):
        side = 'left' if event.type == 'ONE' else 'right'
        g_auto_select_hold[side] = event.value == 'PRESS'
        return {'FINISHED'}

    def execute(self, context):
        return {'FINISHED'}


class ANIM_OT_auto_select_scroll(bpy.types.Operator):
    bl_idname = "anim.auto_select_scroll"
    bl_label = "Adjust Auto Selection"
    bl_description = ("Hold 1 and scroll to extend the automatic keyframe selection to the left, "
                      "hold 2 to extend it to the right, hold both keys to extend both sides "
                      "at the same time\n"
                      "Hold Shift to change the selection by 10 keyframes at once")
    bl_options = {'REGISTER', 'UNDO'}

    direction : EnumProperty (
            name="Direction",
            description="Side of the automatic selection to extend",
            items=[
                ('left', 'Left', 'Extend the selection towards earlier keyframes'),
                ('right', 'Right', 'Extend the selection towards later keyframes'),
                ('both', 'Both', 'Extend the selection on both sides'),
            ],
            default='both'
        ) # type: ignore

    step : IntProperty (
            name="Step",
            description="Number of keyframes to add to or remove from the selection",
            default=1
        ) # type: ignore

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if not (scene.use_anim_offset_mode and scene.anim_offset_auto_select_by_frame):
            return False
        area = getattr(context, 'area', None)
        return area is not None and area.type in _ANIM_EDITOR_TYPES

    def invoke(self, context, event):
        left = g_auto_select_hold['left']
        right = g_auto_select_hold['right']
        if left and right:
            self.direction = 'both'
        elif left:
            self.direction = 'left'
        elif right:
            self.direction = 'right'
        else:
            return {'PASS_THROUGH'}
        self.step = 10 if event.shift else 1
        self.step *= 1 if event.type == 'WHEELUPMOUSE' else -1
        return self.execute(context)

    def execute(self, context):
        scene = context.scene
        if self.direction == 'left':
            changed = adjust_auto_select(scene, self.step, 0)
        elif self.direction == 'right':
            changed = adjust_auto_select(scene, 0, self.step)
        else:
            changed = adjust_auto_select(scene, self.step, self.step)
        if not changed:
            return {'CANCELLED'}
        return {'FINISHED'}


g_transform_wrapper = None

# --- Hold 3 / 4 / 5 + Wheel in the animation editors ------------------------
# The number keys are tracked with a tiny operator (like the 1/2 auto select
# keys) so that the wheel can tell "plain scroll = zoom" from
# "hold a number + scroll = edit an easing option".
#   3 -> easing window      4 -> curve (easing function)      5 -> easing mode
HOLD_KEY_WINDOW = 'THREE'
HOLD_KEY_CURVE = 'FOUR'
HOLD_KEY_MODE = 'FIVE'
HOLD_KEY_EVENTS = (HOLD_KEY_WINDOW, HOLD_KEY_CURVE, HOLD_KEY_MODE)

g_easing_wheel_keys = {key: False for key in HOLD_KEY_EVENTS}

# Kept for backwards compatibility with the 1.10 names/tests.
g_easing_wheel_key_down = False

# Wall-clock time of the last number-key event. Blender can swallow the release
# (focus change, reload, cancelled modal), so the flags are also considered stale
# after HOLD_KEY_TIMEOUT seconds. Holding a modifier produces no further events,
# so the window has to be generous: it is a safety net against a stuck flag, not
# a timer for normal use. Scrolling with a key held refreshes it.
g_easing_wheel_key_time = 0.0
HOLD_KEY_TIMEOUT = 180.0


def _any_hold_key_down():
    return any(g_easing_wheel_keys.values())


def _held_hold_key():
    """Which tracked key is held, if any (3 wins over 4 over 5)."""
    for key in HOLD_KEY_EVENTS:
        if g_easing_wheel_keys.get(key):
            return key
    return None


def _set_hold_key(event_type, down):
    global g_easing_wheel_key_down
    if event_type not in g_easing_wheel_keys:
        return False
    g_easing_wheel_keys[event_type] = bool(down)
    # keep the legacy single-flag in sync with the 3 key
    g_easing_wheel_key_down = bool(g_easing_wheel_keys.get(HOLD_KEY_WINDOW))
    return True


def _clear_hold_keys():
    global g_easing_wheel_key_down, g_easing_wheel_key_time
    for key in HOLD_KEY_EVENTS:
        g_easing_wheel_keys[key] = False
    g_easing_wheel_key_down = False
    g_easing_wheel_key_time = 0.0


def _expire_hold_key_flag():
    """Drop hold flags that were never cleared by a release event.

    Never remove the draw handler from here: this runs from inside the draw
    callback itself (and from the wheel operator), and removing a handler while
    it is being executed corrupts Blender's heap (EXCEPTION_ACCESS_VIOLATION in
    tbbmalloc). Only clear the flags; the handler stays idle until it is removed
    by a key release, unregister, or the next file load.
    """
    if not _any_hold_key_down() or g_easing_wheel_key_time <= 0.0:
        return
    if time.monotonic() - g_easing_wheel_key_time > HOLD_KEY_TIMEOUT:
        for key in HOLD_KEY_EVENTS:
            g_easing_wheel_keys[key] = False
        global g_easing_wheel_key_down
        g_easing_wheel_key_down = False


class ANIM_OT_easing_window_wheel_hold(bpy.types.Operator):
    bl_idname = "anim.easing_window_wheel_hold"
    bl_label = "Easing Wheel Modifier Key"
    bl_description = ("Remember which number key is held for the easing wheel: "
                      "3 = window, 4 = curve, 5 = mode")
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        return get_is_easing_window_wheel_in_editors(context)

    def invoke(self, context, event):
        global g_easing_wheel_key_time
        if not _set_hold_key(event.type, event.value == 'PRESS'):
            return {'PASS_THROUGH'}
        g_easing_wheel_key_time = time.monotonic()
        # the green band follows the 3 key exactly like the drag operator's overlay
        if g_easing_wheel_keys[HOLD_KEY_WINDOW]:
            _start_hold3_overlay()
        else:
            _request_stop_hold3_overlay()
        _tag_anim_editors_redraw(context)
        return {'FINISHED'}

    def execute(self, context):
        return {'FINISHED'}


EASING_CURVE_ORDER = ('linear', 'sine', 'quadratic', 'cubic',
                      'quartic', 'quintic', 'exponential', 'circular')
EASING_MODE_ORDER = ('in', 'out', 'in_out')


def _cycle_enum(scene, attr, order, step):
    """Move an enum property along *order* by *step*, wrapping around."""
    current = getattr(scene, attr)
    try:
        index = order.index(current)
    except ValueError:
        index = 0
    new_value = order[(index + step) % len(order)]
    if new_value == current:
        return False
    setattr(scene, attr, new_value)
    return True


class ANIM_OT_easing_window_wheel(bpy.types.Operator):
    bl_idname = "anim.easing_window_wheel"
    bl_label = "Easing Wheel"
    bl_description = ("Hold a number key and scroll the mouse wheel: "
                      "3 changes the easing window (Ctrl edits the 'After' side, "
                      "Shift steps by 10), 4 cycles the curve, 5 cycles the mode")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if not (scene.use_anim_offset_mode and scene.anim_offset_mode_only_selected):
            return False
        if not get_is_easing_window_wheel_in_editors(context):
            return False
        area = getattr(context, 'area', None)
        return area is not None and area.type in _ANIM_EDITOR_TYPES

    def invoke(self, context, event):
        # A held key can outlive its release when Blender loses the event (focus
        # change, add-on reload). Fall back on a timestamp so a stale flag cannot
        # hijack plain scrolling for ever.
        _expire_hold_key_flag()
        held = _held_hold_key()
        if held is None:
            # plain scroll: let the editor zoom as usual
            return {'PASS_THROUGH'}
        # scrolling with the key held counts as activity, so keep the flags fresh
        global g_easing_wheel_key_time
        g_easing_wheel_key_time = time.monotonic()

        scene = context.scene
        step = 10 if event.shift else 1
        if event.type == 'WHEELDOWNMOUSE':
            step = -step

        if held == HOLD_KEY_CURVE:
            changed = _cycle_enum(scene, 'anim_offset_easing_function',
                                 EASING_CURVE_ORDER, 1 if step > 0 else -1)
        elif held == HOLD_KEY_MODE:
            changed = _cycle_enum(scene, 'anim_offset_easing_mode',
                                 EASING_MODE_ORDER, 1 if step > 0 else -1)
        else:
            index = 1 if (scene.anim_offset_use_asymmetric_window and event.ctrl) else 0
            changed = adjust_easing_window(scene, index, step)

        if not changed:
            return {'CANCELLED'}
        _tag_anim_editors_redraw(context)
        return {'FINISHED'}

    def execute(self, context):
        return {'FINISHED'}


class ANIM_OT_transform_with_easing_window(bpy.types.Operator):
    bl_idname = "anim.transform_with_easing_window"
    bl_label = "Transform (wheel adjusts easing window)"
    bl_description = ("Move, rotate or scale the selection; the mouse wheel adjusts the easing window "
                      "while the transform is running")
    bl_options = {'REGISTER'}

    operation : EnumProperty (
            name="Operation",
            description="Transform operation that is wrapped",
            items=[
                ('TRANSLATE', 'Translate', 'Move the selection'),
                ('ROTATE', 'Rotate', 'Rotate the selection'),
                ('RESIZE', 'Resize', 'Scale the selection'),
            ],
            default='TRANSLATE'
        ) # type: ignore

    @classmethod
    def poll(cls, context):
        scene = context.scene
        if not (scene.use_anim_offset_mode and scene.anim_offset_mode_only_selected):
            return False
        if not get_is_transform_wheel_easing_window(context):
            return False
        area = getattr(context, 'area', None)
        return area is not None and area.type == 'VIEW_3D'

    def invoke(self, context, event):
        global g_transform_wrapper
        operation = {
            'G': ('TRANSLATE', bpy.ops.transform.translate),
            'R': ('ROTATE', bpy.ops.transform.rotate),
            'S': ('RESIZE', bpy.ops.transform.resize),
        }.get(event.type)
        if operation is None:
            return {'PASS_THROUGH'}
        self.operation = operation[0]

        previous = g_transform_wrapper
        if previous is not None:
            previous._finish = True

        result = operation[1]('INVOKE_DEFAULT')
        if 'RUNNING_MODAL' not in result:
            g_transform_wrapper = None
            return result

        self._finish = False
        self._ending = False
        g_transform_wrapper = self
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        """Everything is forwarded to the transform operator below; only the
        mouse wheel is consumed here to adjust the easing window."""
        global g_transform_wrapper

        if getattr(self, '_finish', True):
            if g_transform_wrapper is self:
                g_transform_wrapper = None
            return {'FINISHED'}

        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            scene = context.scene
            index = 1 if (scene.anim_offset_use_asymmetric_window and event.ctrl) else 0
            step = 10 if event.shift else 1
            step *= 1 if event.type == 'WHEELUPMOUSE' else -1
            adjust_easing_window(scene, index, step)
            _tag_anim_editors_redraw(context)
            return {'RUNNING_MODAL'}

        if getattr(self, '_ending', False):
            self._finish = True
            return {'PASS_THROUGH'}

        if event.type in {'LEFTMOUSE', 'RIGHTMOUSE', 'ESC'} and event.value == 'PRESS':
            # the transform finishes on this event, retire the wrapper afterwards
            self._ending = True

        return {'PASS_THROUGH'}


# The generic View2D keymap is resolved before the editor keymaps, so the wheel
# bindings have to live there as well, otherwise Blender scrolls the view.
_AUTO_SELECT_WHEEL_KEYMAPS = (
    ('Dopesheet', 'DOPESHEET_EDITOR'),
    ('Graph Editor', 'GRAPH_EDITOR'),
    ('View2D', 'EMPTY'),
)

_AUTO_SELECT_HOLD_KEYMAPS = (
    ('Dopesheet', 'DOPESHEET_EDITOR'),
    ('Graph Editor', 'GRAPH_EDITOR'),
)

# Hold 3 + wheel: same three keymaps the auto select wheel uses, because Blender
# resolves the wheel through View2D before the editor keymaps.
_EASING_WHEEL_KEYMAPS = (
    ('Dopesheet', 'DOPESHEET_EDITOR'),
    ('Graph Editor', 'GRAPH_EDITOR'),
    ('View2D', 'EMPTY'),
)

_TRANSFORM_KEYMAPS = (
    'Object Mode', 'Mesh', 'Pose', 'Armature', 'Lattice', 'Curve', 'Curves',
    'Metaball', 'Particle', 'Font', 'Paint Curve',
)

_g_auto_select_keymap_items = ()

def _keymap(kc, name, space_type='EMPTY'):
    km = kc.keymaps.get(name)
    if km is None:
        km = kc.keymaps.new(name=name, space_type=space_type)
    return km

def _register_auto_select_keymaps():
    global _g_auto_select_keymap_items
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon or wm.keyconfigs.default
    if kc is None:
        return
    items = []

    for name, space_type in _AUTO_SELECT_WHEEL_KEYMAPS:
        km = _keymap(kc, name, space_type)
        for event_type in ('WHEELUPMOUSE', 'WHEELDOWNMOUSE'):
            duplicate = any(it.idname == 'anim.auto_select_scroll' and it.type == event_type
                            for it in km.keymap_items)
            if duplicate:
                continue
            items.append((km, km.keymap_items.new('anim.auto_select_scroll',
                                                  event_type, 'PRESS')))

    for name, space_type in _AUTO_SELECT_HOLD_KEYMAPS:
        km = _keymap(kc, name, space_type)
        for key in ('ONE', 'TWO'):
            for value in ('PRESS', 'RELEASE'):
                duplicate = any(it.idname == 'anim.auto_select_hold' and it.type == key
                                and it.value == value for it in km.keymap_items)
                if duplicate:
                    continue
                items.append((km, km.keymap_items.new('anim.auto_select_hold', key, value)))

    # --- Hold 3 + wheel: the wheel operator and the 3 key tracker -------------
    for name, space_type in _EASING_WHEEL_KEYMAPS:
        km = _keymap(kc, name, space_type)
        for event_type in ('WHEELUPMOUSE', 'WHEELDOWNMOUSE'):
            duplicate = any(it.idname == 'anim.easing_window_wheel' and it.type == event_type
                            for it in km.keymap_items)
            if duplicate:
                continue
            items.append((km, km.keymap_items.new('anim.easing_window_wheel',
                                                  event_type, 'PRESS')))

    for name, space_type in _AUTO_SELECT_HOLD_KEYMAPS:
        km = _keymap(kc, name, space_type)
        for key in ('THREE', 'FOUR', 'FIVE'):
            for value in ('PRESS', 'RELEASE'):
                duplicate = any(it.idname == 'anim.easing_window_wheel_hold' and it.type == key
                                and it.value == value for it in km.keymap_items)
                if duplicate:
                    continue
                items.append((km, km.keymap_items.new('anim.easing_window_wheel_hold',
                                                      key, value)))

    for name in _TRANSFORM_KEYMAPS:
        km = _keymap(kc, name, 'EMPTY')
        for key in ('G', 'R', 'S'):
            duplicate = any(it.idname == 'anim.transform_with_easing_window' and it.type == key
                            for it in km.keymap_items)
            if duplicate:
                continue
            items.append((km, km.keymap_items.new('anim.transform_with_easing_window',
                                                  key, 'PRESS')))

    _g_auto_select_keymap_items = tuple(items)

def _unregister_auto_select_keymaps():
    global _g_auto_select_keymap_items
    for km, kmi in reversed(_g_auto_select_keymap_items):
        try:
            km.keymap_items.remove(kmi)
        except (ReferenceError, RuntimeError, ValueError):
            pass
    _g_auto_select_keymap_items = ()

def _easing_window_overlay_start():
    return [
        (space_cls, space_cls.draw_handler_add(
            draw_easing_overlay, (space_type,), 'WINDOW', 'POST_PIXEL'))
        for space_cls, space_type in (
            (bpy.types.SpaceGraphEditor, 'GRAPH_EDITOR'),
            (bpy.types.SpaceDopeSheetEditor, 'DOPESHEET_EDITOR'),
        )
    ]

_DRAW_HANDLE_HOLD3 = None

# Removing a draw handler while Blender is executing it corrupts the heap, so the
# stop request is queued to the next timer tick instead of running inline. Only
# the flag is cleared immediately; the handler simply draws nothing until then.
_g_hold3_stop_pending = False


def _hold3_overlay_stop_deferred():
    global _g_hold3_stop_pending
    _g_hold3_stop_pending = False
    _stop_hold3_overlay()
    return None


def _start_hold3_overlay():
    """Show the green selection band while the 3 key is held down."""
    global _DRAW_HANDLE_HOLD3, _g_hold3_stop_pending
    _g_hold3_stop_pending = False
    if _DRAW_HANDLE_HOLD3 is not None:
        return
    handlers = []
    for space_cls, space_type in (
            (bpy.types.SpaceGraphEditor, 'GRAPH_EDITOR'),
            (bpy.types.SpaceDopeSheetEditor, 'DOPESHEET_EDITOR')):
        handlers.append((space_cls, space_cls.draw_handler_add(
            draw_hold3_overlay, (space_type,), 'WINDOW', 'POST_PIXEL')))
    _DRAW_HANDLE_HOLD3 = tuple(handlers)


def _request_stop_hold3_overlay():
    """Ask for the overlay to go away without touching the handler inline."""
    global _g_hold3_stop_pending
    if _DRAW_HANDLE_HOLD3 is None or _g_hold3_stop_pending:
        return
    _g_hold3_stop_pending = True
    try:
        bpy.app.timers.register(_hold3_overlay_stop_deferred, first_interval=0.0)
    except (AttributeError, ValueError):
        _g_hold3_stop_pending = False


def _stop_hold3_overlay():
    global _DRAW_HANDLE_HOLD3
    if not _DRAW_HANDLE_HOLD3:
        _DRAW_HANDLE_HOLD3 = None
        return
    for space_cls, handler in _DRAW_HANDLE_HOLD3:
        try:
            space_cls.draw_handler_remove(handler, 'WINDOW')
        except (ValueError, TypeError):
            pass
    _DRAW_HANDLE_HOLD3 = None


def draw_hold3_overlay(space_type):
    """Same green band as the drag operator, shown while 3 is held."""
    if not g_easing_wheel_keys.get(HOLD_KEY_WINDOW):
        return
    _expire_hold_key_flag()
    if not g_easing_wheel_keys.get(HOLD_KEY_WINDOW):
        return
    draw_easing_overlay(space_type, force=True)


def _easing_window_redraw(context, popup_region=None):
    for area in context.screen.areas:
        if area.type in _ANIM_EDITOR_TYPES:
            area.tag_redraw()
    if popup_region:
        try:
            popup_region.tag_refresh_ui()
        except ReferenceError:
            pass

class ANIM_OT_adjust_easing_window_drag(bpy.types.Operator):
    bl_idname = "anim.adjust_easing_window_drag"
    bl_label = "Drag Easing Window"
    bl_description = "Move the mouse left/right to adjust the easing window, click to confirm"
    bl_options = {'BLOCKING', 'GRAB_CURSOR_X'}

    _draw_handlers = ()
    _popup_region = None

    @classmethod
    def poll(cls, context):
        return (context.scene.use_anim_offset_mode and
                context.scene.anim_offset_mode_only_selected)

    def invoke(self, context, event):
        self._stage = 'before'
        self._init_mouse_x = event.mouse_x
        self._init_window = tuple(context.scene.anim_offset_easing_window_sizes)
        self._popup_region = context.region_popup
        self._init_value = self._init_window[0]
        self._draw_handlers = _easing_window_overlay_start()
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set('MOVE_X')
        return {'RUNNING_MODAL'}

    def _redraw(self, context):
        for area in context.screen.areas:
            if area.type in _ANIM_EDITOR_TYPES:
                area.tag_redraw()
        try:
            if self._popup_region:
                self._popup_region.tag_refresh_ui()
        except ReferenceError:
            self._popup_region = None

    def _cleanup(self, context):
        for space_cls, handler in self._draw_handlers:
            space_cls.draw_handler_remove(handler, 'WINDOW')
        self._draw_handlers = ()
        context.window.cursor_modal_restore()
        self._redraw(context)

    def modal(self, context, event):
        if event.type == 'ESC' or event.type == 'RIGHTMOUSE':
            self.cancel(context)
            return {'CANCELLED'}

        if event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            if context.scene.anim_offset_use_asymmetric_window is True and self._stage == 'before':
                # Switch to the second stage: keep the operator running so the
                # mouse can now adjust the "after" window.
                self._stage = 'after'
                self._init_mouse_x = event.mouse_x
                self._init_value = context.scene.anim_offset_easing_window_sizes[1]
            else:
                self.finish(context)
                return {'FINISHED'}

        if event.type == 'MOUSEMOVE':
            delta = int((event.mouse_x - self._init_mouse_x) / 10)
            new_value = max(0, self._init_value + delta)
            if self._stage == 'before':
                context.scene.anim_offset_easing_window_sizes[0] = new_value
            else:
                context.scene.anim_offset_easing_window_sizes[1] = new_value
            self._redraw(context)

        return {'RUNNING_MODAL'}

    def finish(self, context):
        self._cleanup(context)

    def cancel(self, context):
        context.scene.anim_offset_easing_window_sizes = self._init_window
        self._cleanup(context)


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

g_easing_window_sync_active = False

def _on_easing_curve_mode_change(scene, context):
    """Redraw the editors so the overlay follows the new curve or mode at once."""
    _tag_anim_editors_redraw(context)


def _on_easing_window_options_change(scene, context):
    global g_easing_window_sync_active
    if g_easing_window_sync_active or scene.anim_offset_use_asymmetric_window:
        return
    window = scene.anim_offset_easing_window_sizes
    if window[1] != window[0]:
        g_easing_window_sync_active = True
        try:
            window[1] = window[0]
        finally:
            g_easing_window_sync_active = False

def draw_header(self, context):
    # st = context.space_data
    # if st.mode == 'TIMELINE': # don't show in timeline
    #     return

    row = self.layout.row(align=True)
    row.prop(context.scene, "use_anim_offset_mode", icon='CON_TRANSLIKE', toggle=True, text='')
    row.active = not context.scene.tool_settings.use_keyframe_insert_auto
    sub = row.row(align=True)
    sub.popover(
        panel=DOPESHEET_PT_anim_offset_mode.bl_idname,
        text="",
    )
    sub.active = context.scene.use_anim_offset_mode

def _safe_unregister_class(cls):
    """Unregister *cls* if it is still registered (a second disable would fail)."""
    if getattr(cls, "is_registered", True) is False:
        return
    try:
        bpy.utils.unregister_class(cls)
    except (RuntimeError, TypeError, AttributeError):
        pass

# Reloading the add-on (install over, enable/disable) creates NEW function
# objects for the same module-level callbacks, so identity checks are useless:
# the stale copy would survive and the UI would draw twice. Match by name and
# module instead.
def _callback_id(func):
    """Stable identity of a callback across add-on reloads.

    The same file can be imported as 'anim_auto_offset' or as
    'bl_ext.blender_org.anim_auto_offset', so only the last module component
    is compared.
    """
    module = getattr(func, "__module__", "") or ""
    return (getattr(func, "__name__", None), module.split(".")[-1])

def _same_callback(a, b):
    return _callback_id(a) == _callback_id(b)

def _add_handler(collection, func):
    """Append *func* exactly once, dropping stale copies of itself."""
    _remove_handler(collection, func)
    collection.append(func)

def _remove_handler(collection, func):
    for existing in [f for f in collection if _same_callback(f, func)]:
        collection.remove(existing)

def _add_ui_callback(collection, func):
    """Append a header draw callback exactly once (duplicates draw twice)."""
    _remove_ui_callback(collection, func)
    collection.append(func)

def _remove_ui_callback(collection, func):
    # *_HT_header is not iterable itself; _dyn_ui_initialize() returns the list
    get_funcs = getattr(collection, '_dyn_ui_initialize', None)
    if get_funcs is None:
        return
    for existing in [f for f in get_funcs() if _same_callback(f, func)]:
        collection.remove(existing)

g_is_registered = False

def register():
    # Blender sometimes calls register() twice (e.g. enabling the add-on while a
    # copy is already loaded); without this guard the header buttons, handlers
    # and keymaps would be added twice and show up duplicated in the UI.
    global g_is_registered
    if g_is_registered:
        try:
            unregister()
        except Exception:
            pass
    g_is_registered = False

    bpy.types.Scene.use_keyframe_insert_auto_old = BoolProperty () # internal

    bpy.types.Scene.use_anim_offset_mode = BoolProperty (
            name="Offset editing",
            description="Apply property value changes as an offset to all keyframe points\n"
                        "NOTE: This option only takes effect when 'Auto keying' is disabled",
            default=False,
            update=_on_anim_offset_mode_change
        )
    bpy.types.Scene.anim_offset_use_asymmetric_window = BoolProperty (
            name="Asymmetric Window",
            description="Use separate easing window sizes before and after the selected keyframes",
            default=False,
            update=_on_easing_window_options_change
        )
    bpy.types.Scene.anim_offset_mode_only_selected = BoolProperty (
            name="Affect only selected keyframes",
            description="Affect only selected keyframe points when Offset editing is enabled",
            default=False
        )
    bpy.types.Scene.anim_offset_auto_select_by_frame = BoolProperty (
            name="Auto select keys by frame",
            description="Automatically select the keyframe points around the current frame\n"
                        "Hold 1 and scroll the mouse wheel to extend the selection to the left,\n"
                        "hold 2 to extend it to the right, hold both keys to extend both sides",
            default=False,
            update=_on_auto_select_change
        )
    bpy.types.Scene.anim_offset_auto_select_counts = IntVectorProperty (
            name="Auto select range",
            description="Number of keyframes selected before and after the current frame\n"
                        "Adjust with 1 + Wheel (left) and 2 + Wheel (right)",
            default=(0, 0),
            min=0,
            max=9999,
            size=2,
            update=_on_auto_select_counts_change
        )
    # NOTE: newer than the old scalar 'anim_offset_easing_window' property.
    # Keeping a different name avoids reading that scalar as an array in .blend
    # files saved by older versions.
    bpy.types.Scene.anim_offset_easing_window_sizes = IntVectorProperty (
            name="Easing window",
            description="Number of keyframes before and after the selected range to blend with the applied offset\n"
                        "NOTE: This option only takes effect when the selected keyframes (for all F-Curves) are in consecutive order",
            default=(0, 0),
            min=0,
            max=9999,
            size=2,
            update=_on_easing_window_options_change
        )
    bpy.types.Scene.anim_offset_easing_function = EnumProperty (
            name="Easing function",
            description="Curve shape for the easing blend",
            items=[
                ('linear', 'Linear', 'Linear blend'),
                ('sine', 'Sine', 'Sine curve'),
                ('quadratic', 'Quadratic', 'Quadratic curve'),
                ('cubic', 'Cubic', 'Cubic curve'),
                ('quartic', 'Quartic', 'Quartic curve'),
                ('quintic', 'Quintic', 'Quintic curve'),
                ('exponential', 'Exponential', 'Exponential curve'),
                ('circular', 'Circular', 'Circular curve'),
            ],
            default='linear',
            update=_on_easing_curve_mode_change
        )
    bpy.types.Scene.anim_offset_easing_mode = EnumProperty (
            name="Easing mode",
            description="Easing direction applied to the blend curve",
            items=[
                ('in', 'In', 'Ease in (slow start, fast end)'),
                ('out', 'Out', 'Ease out (fast start, slow end)'),
                ('in_out', 'In & Out', 'Ease in and out (S-curve)'),
            ],
            default='in_out',
            update=_on_easing_curve_mode_change
        )

    g_is_registered = True

    bpy.utils.register_class(AnimAutoOffsetPreferences)
    bpy.utils.register_class(DOPESHEET_PT_anim_offset_mode)
    bpy.utils.register_class(ANIM_PT_offset_editing_dope)
    bpy.utils.register_class(ANIM_PT_offset_editing_graph)
    bpy.utils.register_class(ANIM_OT_auto_select_hold)
    bpy.utils.register_class(ANIM_OT_auto_select_scroll)
    bpy.utils.register_class(ANIM_OT_easing_window_wheel_hold)
    bpy.utils.register_class(ANIM_OT_easing_window_wheel)
    bpy.utils.register_class(ANIM_OT_transform_with_easing_window)
    bpy.utils.register_class(ANIM_OT_adjust_easing_window_drag)
    _register_message_bus()
    _add_ui_callback(bpy.types.GRAPH_HT_header, draw_header)
    _add_ui_callback(bpy.types.DOPESHEET_HT_header, draw_header)
    _add_handler(bpy.app.handlers.depsgraph_update_post, post_depsgraph_update)
    _add_handler(bpy.app.handlers.depsgraph_update_pre, pre_depsgraph_update)
    _add_handler(bpy.app.handlers.undo_post, post_redo_undo)
    _add_handler(bpy.app.handlers.redo_post, post_redo_undo)
    _add_handler(bpy.app.handlers.undo_pre, pre_redo_undo)
    _add_handler(bpy.app.handlers.redo_pre, pre_redo_undo)
    _add_handler(bpy.app.handlers.load_post, post_load)
    _add_handler(bpy.app.handlers.frame_change_post, auto_select_frame_change)
    _register_auto_select_keymaps()

def unregister():
    _unregister_auto_select_keymaps()
    _clear_hold_keys()
    _stop_hold3_overlay()
    _remove_handler(bpy.app.handlers.frame_change_post, auto_select_frame_change)
    _remove_handler(bpy.app.handlers.load_post, post_load)
    _remove_handler(bpy.app.handlers.redo_pre, pre_redo_undo)
    _remove_handler(bpy.app.handlers.undo_pre, pre_redo_undo)
    _remove_handler(bpy.app.handlers.redo_post, post_redo_undo)
    _remove_handler(bpy.app.handlers.undo_post, post_redo_undo)
    _remove_handler(bpy.app.handlers.depsgraph_update_pre, pre_depsgraph_update)
    _remove_handler(bpy.app.handlers.depsgraph_update_post, post_depsgraph_update)
    _remove_ui_callback(bpy.types.DOPESHEET_HT_header, draw_header)
    _remove_ui_callback(bpy.types.GRAPH_HT_header, draw_header)
    _unregister_message_bus()
    for cls in (ANIM_PT_offset_editing_graph,
                ANIM_PT_offset_editing_dope,
                ANIM_OT_easing_window_wheel,
                ANIM_OT_easing_window_wheel_hold,
                ANIM_OT_transform_with_easing_window,
                ANIM_OT_auto_select_scroll,
                ANIM_OT_auto_select_hold,
                ANIM_OT_adjust_easing_window_drag,
                DOPESHEET_PT_anim_offset_mode,
                AnimAutoOffsetPreferences):
        _safe_unregister_class(cls)

    # delete the scene properties, tolerating a partial registration (a second
    # disable used to raise AttributeError here and abort the rest of unregister)
    for attr in ('anim_offset_easing_mode',
                 'anim_offset_use_asymmetric_window',
                 'anim_offset_easing_function',
                 'anim_offset_easing_window_sizes',
                 'anim_offset_auto_select_counts',
                 'anim_offset_auto_select_by_frame',
                 'anim_offset_mode_only_selected',
                 'use_anim_offset_mode',
                 'use_keyframe_insert_auto_old'):
        try:
            delattr(bpy.types.Scene, attr)
        except (AttributeError, RuntimeError):
            pass
    g_is_registered = False
