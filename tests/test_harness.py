import importlib.util
import os

import addon_utils
import bpy

# Normally the harness tests the repository source directly. The installed
# mode is used by packaging tests to make sure Blender loads the same code
# after installing the release zip.
TEST_INSTALLED_ADDON = os.environ.get("VRM_RIGIFY_TEST_INSTALLED") == "true"
if TEST_INSTALLED_ADDON:
    import vrm_rigify  # noqa: E402
else:
    addon_path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "vrm_rigify", "__init__.py"
    ))
    addon_spec = importlib.util.spec_from_file_location(
        "vrm_rigify_under_test", addon_path
    )
    assert addon_spec is not None and addon_spec.loader is not None
    vrm_rigify = importlib.util.module_from_spec(addon_spec)
    addon_spec.loader.exec_module(vrm_rigify)


def enable_addon(module_name: str):
    # Addons must be enabled with `default_set=True` so they are registered
    # in `preferences.addons`, which Rigify reads during registration.
    module = addon_utils.enable(module_name, default_set=True)
    assert module is not None, f"failed to enable addon '{module_name}'"


def enable_vrm_addon():
    # The VRM addon's module name differs between Blender versions: it is
    # installed as an extension (`bl_ext.user_default.vrm`) on Blender 4.2
    # and later, and as a legacy addon on earlier versions. Prefer the exact
    # extension name so a stale legacy install can never shadow it.
    module_names = [module.__name__ for module in addon_utils.modules()]
    candidates = [name for name in module_names if name == "bl_ext.user_default.vrm"]
    candidates += [name for name in module_names if "vrm" in name.lower()]
    for module_name in candidates:
        print(f"enabling VRM addon '{module_name}'")
        enable_addon(module_name)
        return
    raise Exception("no VRM addon is installed")


def import_vrm_model(model_path: str) -> bpy.types.Object:
    objects_before = set(bpy.data.objects)
    bpy.ops.import_scene.vrm(filepath=model_path)
    objects_imported = set(bpy.data.objects) - objects_before
    [vrm_object] = [node for node in objects_imported if node.type == "ARMATURE"]
    return vrm_object


def vrm_model_vertex_group_names(mesh_objects: list[bpy.types.Object]) -> set[str]:
    # Only vertex groups that actually have weights assigned
    # need a matching bone in the generated rig.
    names = set()
    for mesh in mesh_objects:
        group_names_by_index = {group.index: group.name for group in mesh.vertex_groups}
        for vertex in mesh.data.vertices:
            for group in vertex.groups:
                if group.weight > 0.0:
                    names.add(group_names_by_index[group.group])
    return names


def check_control_bones_exist(rig_object: bpy.types.Object):
    control_bone_names = [
        "torso",
        "head",
        "hand_ik.L",
        "hand_ik.R",
        "foot_ik.L",
        "foot_ik.R",
    ]

    for bone_name in control_bone_names:
        assert bone_name in rig_object.data.bones, \
            f"control bone '{bone_name}' is missing from the generated rig"


def check_deform_coverage(rig_object: bpy.types.Object, vertex_group_names: set[str]):
    # Every vertex group used by the model's meshes must exist as a deforming
    # bone in the generated rig or else parts of the model will not follow the
    # rig. A bone with a matching name is not enough: the armature modifier
    # ignores bones that have deformation disabled.
    deform_bone_names = {bone.name for bone in rig_object.data.bones if bone.use_deform}
    missing = sorted(vertex_group_names - deform_bone_names)
    assert not missing, f"vertex groups have no matching deform bone: {missing}"


def check_shape_key_controls(rig_object: bpy.types.Object, vrm_object: bpy.types.Object):
    rig_extension = rig_object.data.vrm_addon_extension
    vrm_extension = vrm_object.data.vrm_addon_extension
    # Note: `.keys()` views on ID properties do not implement
    # equality so they must be converted into sets to be compared.
    rig_presets = set(rig_extension.vrm1["expressions"]["preset"].keys())
    vrm_presets = set(vrm_extension.vrm1["expressions"]["preset"].keys())
    assert len(vrm_presets) > 0, "model has no preset expressions"
    assert rig_presets == vrm_presets, \
        f"rig expressions do not match the model: {rig_presets ^ vrm_presets}"


def check_meshes_attached_to_rig(
    rig_object: bpy.types.Object,
    vrm_object: bpy.types.Object,
    mesh_objects: list[bpy.types.Object],
):
    assert mesh_objects, "model has no meshes"
    for mesh_object in mesh_objects:
        assert mesh_object.parent == rig_object, \
            f"mesh '{mesh_object.name}' is not parented to the generated rig"

        armature_modifiers = [
            modifier for modifier in mesh_object.modifiers
            if modifier.type == "ARMATURE"
        ]
        assert any(modifier.object == rig_object for modifier in armature_modifiers), \
            f"mesh '{mesh_object.name}' has no modifier targeting the generated rig"
        assert not any(modifier.object == vrm_object for modifier in armature_modifiers), \
            f"mesh '{mesh_object.name}' still has a modifier targeting the VRM armature"

        armature_modifier_indices = [
            index for index, modifier in enumerate(mesh_object.modifiers)
            if modifier.type == "ARMATURE"
        ]
        assert armature_modifier_indices == list(range(len(armature_modifiers))), \
            f"mesh '{mesh_object.name}' armature modifiers are not first"


def check_conversion_markers(
    rig_object: bpy.types.Object,
    vrm_object: bpy.types.Object,
    mesh_objects: list[bpy.types.Object],
):
    expected_version = ".".join(
        str(component) for component in vrm_rigify.bl_info["version"]
    )
    assert rig_object["vrm_rigify_version"] == expected_version
    assert rig_object["vrm_rigify_source_armature"] == vrm_object.name
    assert rig_object["vrm_rigify_attached_mesh_count"] == len(mesh_objects)


def check_repair_operator(
    rig_object: bpy.types.Object,
    vrm_object: bpy.types.Object,
    mesh_objects: list[bpy.types.Object],
):
    # Recreate the state written by versions that generated a Rigify armature
    # but left the imported meshes attached to the hidden VRM armature.
    bpy.ops.object.mode_set(mode="OBJECT")
    for property_name in [
        "vrm_rigify_version",
        "vrm_rigify_source_armature",
        "vrm_rigify_attached_mesh_count",
    ]:
        del rig_object[property_name]
    for mesh_object in mesh_objects:
        mesh_object.parent = vrm_object
        [armature_modifier] = [
            modifier for modifier in mesh_object.modifiers
            if modifier.type == "ARMATURE"
        ]
        armature_modifier.object = vrm_object

    bpy.context.view_layer.objects.active = rig_object
    rig_object.select_set(True)
    result = bpy.ops.vrm_rigify.repair_mesh_attachment()
    assert result == {"FINISHED"}
    check_meshes_attached_to_rig(
        rig_object, vrm_object, mesh_objects
    )
    check_conversion_markers(rig_object, vrm_object, mesh_objects)


def check_meshes_deform_with_rig(
    rig_object: bpy.types.Object,
    mesh_objects: list[bpy.types.Object],
):
    depsgraph = bpy.context.evaluated_depsgraph_get()

    def evaluated_vertex_positions():
        positions = {}
        for mesh_object in mesh_objects:
            evaluated_object = mesh_object.evaluated_get(depsgraph)
            positions[mesh_object.name] = [
                evaluated_object.matrix_world @ vertex.co
                for vertex in evaluated_object.data.vertices
            ]
        return positions

    head_control = rig_object.pose.bones["head"]
    original_location = head_control.location.copy()
    positions_before = evaluated_vertex_positions()
    head_control.location.x += 0.1
    bpy.context.view_layer.update()
    positions_after = evaluated_vertex_positions()

    maximum_movement_by_mesh = {
        mesh_object.name: max(
            (position_after - position_before).length
            for position_before, position_after in zip(
                positions_before[mesh_object.name],
                positions_after[mesh_object.name],
            )
        )
        for mesh_object in mesh_objects
    }

    head_control.location = original_location
    bpy.context.view_layer.update()
    stationary_meshes = sorted(
        mesh_name
        for mesh_name, maximum_movement in maximum_movement_by_mesh.items()
        if maximum_movement <= 0.001
    )
    assert not stationary_meshes, \
        f"meshes do not deform with the Rigify head control: {stationary_meshes}"


def main():
    model_path = os.environ["VRM_TEST_MODEL_PATH"]

    # The addon's command must be usable from Blender's normal menus without
    # exposing developer-only operators through the Developer Extras setting.
    bpy.context.preferences.view.show_developer_ui = False

    enable_addon("rigify")
    enable_vrm_addon()
    if TEST_INSTALLED_ADDON:
        enable_addon("vrm_rigify")
        print(f"testing installed addon at '{vrm_rigify.__file__}'")
    else:
        # An installed copy can be enabled by the test profile. Unregister it
        # before registering the repository source under test.
        addon_utils.disable("vrm_rigify", default_set=False)
        vrm_rigify.register()

    # Start from an empty scene.
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()

    vrm_object = import_vrm_model(model_path)
    vrm_descendants = set(vrm_object.children_recursive)
    mesh_objects = [
        node for node in bpy.context.view_layer.objects
        if node.type == "MESH" and (
            node in vrm_descendants
            or any(
                modifier.type == "ARMATURE"
                and modifier.object == vrm_object
                for modifier in node.modifiers
            )
        )
    ]
    vertex_group_names = vrm_model_vertex_group_names(mesh_objects)

    # Generate the rig.
    objects_before = set(bpy.data.objects)
    bpy.context.view_layer.objects.active = vrm_object
    result = bpy.ops.vrm_rigify.create_rig()
    assert result == {"FINISHED"}

    # Find the generated rig.
    objects_generated = set(bpy.data.objects) - objects_before
    [rig_object] = [
        node for node in objects_generated
        if node.type == "ARMATURE" and not node.name.endswith(".metarig")
    ]

    check_control_bones_exist(rig_object)
    check_deform_coverage(rig_object, vertex_group_names)
    check_shape_key_controls(rig_object, vrm_object)
    check_meshes_attached_to_rig(rig_object, vrm_object, mesh_objects)
    check_conversion_markers(rig_object, vrm_object, mesh_objects)
    assert bpy.context.active_object == rig_object, "generated rig is not active"
    assert rig_object.mode == "POSE", "generated rig is not in Pose Mode"
    check_repair_operator(rig_object, vrm_object, mesh_objects)
    check_meshes_deform_with_rig(rig_object, mesh_objects)

    print(f"model '{os.path.basename(model_path)}' passed all checks")


main()
