import bpy # type: ignore
from bpy.props import BoolProperty, IntVectorProperty, EnumProperty # type: ignore
from bpy.app.handlers import persistent # type: ignore
from mathutils import Vector # type: ignore
import numpy as np # type: ignore
import math
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

    def draw(self, context):
        self.layout.prop(self, 'auto_key_override')
        self.layout.prop(self, 'easing_overlay_gradient')

def get_is_auto_key_override(context):
    return context.preferences.addons[__package__].preferences.auto_key_override

def get_is_easing_overlay_gradient(context):
    return context.preferences.addons[__package__].preferences.easing_overlay_gradient

def _get_obj_fcurves(obj):
    action = obj.animation_data.action
    action_slot = obj.animation_data.action_slot
    if not action or not action_slot:
        return None
    # TODO: update to support layers when they are actually introduced
    channelbag = action.layers[0].strips[0].channelbag(action_slot)
    if channelbag is None:
        return None
    return channelbag.fcurves

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
        'in':     lambda t: math.sin(t * math.pi / 2),
        'out':    lambda t: 1 - math.cos(t * math.pi / 2),
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

@persistent
def post_load(none, other_none) -> None:
    _register_message_bus()

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
        scene = context.scene
        layout = self.layout
        row = layout.row(align=True)
        row.prop(scene, 'anim_offset_mode_only_selected')
        row.active = scene.use_anim_offset_mode

        only_sel = scene.anim_offset_mode_only_selected
        is_sequential = True
        obj = context.active_object
        obj_is_valid = obj and obj.animation_data

        if only_sel and obj_is_valid:
            is_sequential = self._selected_keyframes_are_sequential(obj)

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
        _col.operator('anim.adjust_easing_window', text='', icon='DRIVER_DISTANCE')
        _col.enabled = only_sel and is_sequential

        if (only_sel and not is_sequential and has_easing_window
            and obj_is_valid and self._selected_keyframes_are_non_consecutive(obj)):
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

def draw_easing_overlay(space_type):
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
    fcurves = _get_obj_fcurves(obj)
    if not fcurves:
        return

    min_sel_frame = float('inf')
    max_sel_frame = float('-inf')
    min_window_frame = float('inf')
    max_window_frame = float('-inf')
    found = False

    for fcurve in fcurves:
        if not fcurve.select:
            continue

        size = len(fcurve.keyframe_points)
        if size == 0:
            continue
        selected = np.empty(size, dtype=bool)
        fcurve.keyframe_points.foreach_get('select_control_point', selected)
        indices = np.where(selected)[0]
        if len(indices) == 0:
            continue
        if len(indices) > 1 and not np.all(np.diff(indices) == 1):
            continue

        found = True
        first_idx = indices[0]
        last_idx = indices[-1]
        first_frame = fcurve.keyframe_points[first_idx].co[0]
        last_frame = fcurve.keyframe_points[last_idx].co[0]

        min_sel_frame = min(min_sel_frame, first_frame)
        max_sel_frame = max(max_sel_frame, last_frame)

        ws_idx = max(0, min(size - 1, first_idx - windowB))
        we_idx = max(0, min(size - 1, last_idx + windowA))
        min_window_frame = min(min_window_frame, fcurve.keyframe_points[ws_idx].co[0])
        max_window_frame = max(max_window_frame, fcurve.keyframe_points[we_idx].co[0])

    if not found:
        return

    y_bottom = region.y
    y_top = region.y + region.height

    x_win_start = view2d.view_to_region(min_window_frame, 0.0, clip=False)[0]
    x_win_end = view2d.view_to_region(max_window_frame, 0.0, clip=False)[0]
    x_sel_start = view2d.view_to_region(min_sel_frame, 0.0, clip=False)[0]
    x_sel_end = view2d.view_to_region(max_sel_frame, 0.0, clip=False)[0]

    gpu.state.blend_set('ALPHA')

    if get_is_easing_overlay_gradient(context):
        easing_fun = EASINGS[scene.anim_offset_easing_function][scene.anim_offset_easing_mode]

        def ease(t):
            if t <= 0:
                return 0.0
            if t >= 1:
                return 1.0
            return easing_fun(t)

        shader = gpu.shader.from_builtin('SMOOTH_COLOR')

        def draw_band(x0, x1, weight_at, color):
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

        color = (0.25, 0.75, 0.25, 0.30)
        if windowB > 0 and x_sel_start > x_win_start:
            draw_band(x_win_start, x_sel_start, ease, color)
        draw_band(x_sel_start, x_sel_end, lambda u: 1.0, color)
        if windowA > 0 and x_win_end > x_sel_end:
            draw_band(x_sel_end, x_win_end, lambda u: ease(1.0 - u), color)
    else:
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')

        def draw_band(x0, x1, color):
            if x1 <= x0:
                return
            verts = [(x0, y_bottom), (x1, y_bottom), (x1, y_top), (x0, y_top)]
            batch = batch_for_shader(shader, 'TRI_FAN', {"pos": verts})
            shader.uniform_float("color", color)
            batch.draw(shader)

        if windowB > 0 and x_sel_start > x_win_start:
            draw_band(x_win_start, x_sel_start, (0.25, 0.75, 0.25, 0.12))
        draw_band(x_sel_start, x_sel_end, (0.25, 0.75, 0.25, 0.30))
        if windowA > 0 and x_win_end > x_sel_end:
            draw_band(x_sel_end, x_win_end, (0.25, 0.75, 0.25, 0.12))

    gpu.state.blend_set('NONE')

class ANIM_OT_adjust_easing_window(bpy.types.Operator):
    bl_idname = "anim.adjust_easing_window"
    bl_label = "Adjust Easing Window"
    bl_description = "Move the mouse left/right to adjust the easing window, click to confirm, scroll to cycle the easing curve"
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
        self._init_easing_function = context.scene.anim_offset_easing_function
        self._init_value = self._init_window[0]
        self._popup_region = context.region_popup
        self._draw_handlers = [
            (space_cls, space_cls.draw_handler_add(
                draw_easing_overlay, (space_type,), 'WINDOW', 'POST_PIXEL'))
            for space_cls, space_type in (
                (bpy.types.SpaceGraphEditor, 'GRAPH_EDITOR'),
                (bpy.types.SpaceDopeSheetEditor, 'DOPESHEET_EDITOR'),
            )
        ]
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

        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            current = context.scene.anim_offset_easing_function
            index = EASING_FUNCTIONS.index(current)
            step = 1 if event.type == 'WHEELUPMOUSE' else -1
            context.scene.anim_offset_easing_function = EASING_FUNCTIONS[
                (index + step) % len(EASING_FUNCTIONS)]
            self._redraw(context)
            return {'RUNNING_MODAL'}

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
        context.scene.anim_offset_easing_function = self._init_easing_function
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

def register():
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
        )

    bpy.utils.register_class(AnimAutoOffsetPreferences)
    bpy.utils.register_class(DOPESHEET_PT_anim_offset_mode)
    bpy.utils.register_class(ANIM_OT_adjust_easing_window)
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
    bpy.utils.unregister_class(ANIM_OT_adjust_easing_window)
    bpy.utils.unregister_class(DOPESHEET_PT_anim_offset_mode)
    bpy.utils.unregister_class(AnimAutoOffsetPreferences)

    del bpy.types.Scene.anim_offset_easing_mode
    del bpy.types.Scene.anim_offset_use_asymmetric_window
    del bpy.types.Scene.anim_offset_easing_function
    del bpy.types.Scene.anim_offset_easing_window_sizes
    del bpy.types.Scene.anim_offset_mode_only_selected
    del bpy.types.Scene.use_anim_offset_mode
    del bpy.types.Scene.use_keyframe_insert_auto_old
