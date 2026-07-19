import re

import bpy

bl_info = {
    "name": "VRM Rigify",
    "author": "Nanoskript",
    "description": "Generates Rigify armatures for VRM models",
    "version": (0, 4, 2),
    "blender": (4, 1, 0),
    "location": "3D Viewport > Object",
    "doc_url": "https://github.com/nanoskript/vrm-rigify",
    "tracker_url": "https://github.com/nanoskript/vrm-rigify/issues",
    "category": "Rigging",
}


def addon_version_string():
    return ".".join(str(component) for component in bl_info["version"])


class ModeContext:
    def __init__(self, mode):
        self.mode = mode

    def __enter__(self):
        self.old_mode = bpy.context.object.mode
        bpy.ops.object.mode_set(mode=self.mode)

    def __exit__(self, _type, _value, _trace):
        bpy.ops.object.mode_set(mode=self.old_mode)

    @staticmethod
    def editing(node: bpy.types.Object):
        node.select_set(True)
        return ModeContext("EDIT")


def objects_by_name_patterns(objects, patterns: list[str]):
    object_matches = []
    for node in objects:
        matches = False
        for pattern in patterns:
            matches |= bool(re.match(pattern, node.name))
        if matches:
            object_matches.append(node)
    return object_matches


def full_bone_path(bone: bpy.types.Bone | bpy.types.EditBone) -> str:
    bone_chain = list(reversed(bone.parent_recursive)) + [bone]
    return '/'.join([bone.name for bone in bone_chain])


def generate_template_metarig(metarig_name: str) -> bpy.types.Object:
    try:
        # Generate a humanoid metarig and automatically
        # assign VRM bone types to the metarig.
        bpy.ops.object.armature_human_metarig_add()
        metarig = bpy.context.view_layer.objects.active
        metarig.name = metarig_name
        metarig.data.name = metarig_name
        return metarig
    except AttributeError as e:
        raise Exception("Failed to spawn metarig. Is the Rigify addon enabled?") from e


def assign_vrm1_human_bones_automatically(node: bpy.types.Object):
    try:
        bpy.ops.vrm.assign_vrm1_humanoid_human_bones_automatically(
            armature_object_name=node.name
        )
    except TypeError:
        # Versions of the VRM addon before 4.0 only
        # accept the `armature_name` argument alias.
        bpy.ops.vrm.assign_vrm1_humanoid_human_bones_automatically(
            armature_name=node.name
        )


def compute_metarig_and_vrm_model_bone_mapping(metarig: bpy.types.Object, vrm_object: bpy.types.Object):
    assign_vrm1_human_bones_automatically(metarig)
    assign_vrm1_human_bones_automatically(vrm_object)

    armature_metarig: bpy.types.Armature = metarig.data
    armature_vrm: bpy.types.Armature = vrm_object.data
    metarig_human_bones = armature_metarig.vrm_addon_extension.vrm1.humanoid.human_bones
    vrm_human_bones = armature_vrm.vrm_addon_extension.vrm1.humanoid.human_bones

    # Compute a bone mapping between the metarig
    # and VRM model based on VRM bone types.
    bone_mapping = []
    for bone_type in metarig_human_bones.keys():
        if bone_type in ["last_bone_names", "initial_automatic_bone_assignment"]:
            continue

        # Newer versions of the VRM addon include entries in `human_bones`
        # that are not bone references, so skip anything without a `node`.
        metarig_bone = getattr(metarig_human_bones, bone_type, None)
        vrm_bone = getattr(vrm_human_bones, bone_type, None)
        if not (hasattr(metarig_bone, "node") and hasattr(vrm_bone, "node")):
            continue

        if vrm_bone.node.bone_name:
            bone_mapping.append((metarig_bone.node.bone_name, vrm_bone.node.bone_name))
    return bone_mapping


def remove_or_log_unmapped_metarig_bones(metarig: bpy.types.Object, bone_mapping):
    mapped_metarig_bone_names = set([metarig_bone for metarig_bone, vrm_bone in bone_mapping])
    armature_metarig: bpy.types.Armature = metarig.data
    with ModeContext.editing(metarig):
        for metarig_bone in armature_metarig.edit_bones:
            if metarig_bone.name in mapped_metarig_bone_names:
                continue

            # spine.003 (Upper Chest) is an optional VRM bone. Remove it if it
            # cannot be mapped or else Rigify will fail to generate the rig due to
            # a disconnection between spine.003 and spine.004.
            # spine.005 (upper neck) can never be mapped because VRM models only
            # have a single neck bone. If it is kept, it collapses to zero length
            # and breaks the neck chain, so Rigify will not generate a deform
            # bone for the head.
            # FIXME: Add heuristics for mapping breast bones.
            if metarig_bone.name not in ["spine.003", "spine.005", "breast.L", "breast.R"]:
                print(f"metarig bone is not mapped '{full_bone_path(metarig_bone)}'")
                continue

            print(f"removing unmapped metarig bone '{full_bone_path(metarig_bone)}'")
            armature_metarig.edit_bones.remove(metarig_bone)


def position_metarig_bones_to_vrm_model(metarig: bpy.types.Object, vrm_object: bpy.types.Object, bone_mapping):
    armature_metarig: bpy.types.Armature = metarig.data
    armature_vrm: bpy.types.Armature = vrm_object.data
    with ModeContext.editing(metarig):
        metarig.matrix_world = vrm_object.matrix_world
        for metarig_bone_name, vrm_bone_name in bone_mapping:
            metarig_bone = armature_metarig.edit_bones[metarig_bone_name]
            vrm_bone = armature_vrm.bones[vrm_bone_name]

            print(f"positioning '{full_bone_path(metarig_bone)}' to '{full_bone_path(vrm_bone)}'")
            metarig_bone.select = True
            metarig_bone.head = vrm_bone.head_local
            metarig_bone.tail = vrm_bone.tail_local


def fix_position_of_metarig_spine_bones(metarig: bpy.types.Object, bone_mapping):
    mapped_metarig_bone_names = set([metarig_bone for metarig_bone, vrm_bone in bone_mapping])
    armature_metarig: bpy.types.Armature = metarig.data
    with ModeContext.editing(metarig):
        # If spine.003 and spine.004 are present, ensure that they are connected
        # to each other, otherwise Rigify will fail to generate the rig.
        armature_metarig.edit_bones["spine.004"].use_connect = True
        armature_metarig.edit_bones["spine.004"].use_connect = False

        # Reconnect the head bone to the end of the neck chain (spine.005 has
        # been removed), otherwise Rigify will exclude the head bone from the
        # neck rig and will not generate a deform bone for it. Only reconnect
        # if the neck bone has been mapped: connecting to an unpositioned neck
        # bone would move the head bone away from the model's head position.
        if "spine.004" in mapped_metarig_bone_names:
            armature_metarig.edit_bones["spine.006"].use_connect = True


def remove_metarig_palm_bones(metarig: bpy.types.Object):
    # There isn't a bone mapping for the palm bones so let's remove them.
    armature_metarig: bpy.types.Armature = metarig.data
    with ModeContext.editing(metarig):
        edit_bones = armature_metarig.edit_bones
        for bone in objects_by_name_patterns(edit_bones, [r"^palm.*$"]):
            print(f"deleting metarig palm bone '{bone.name}'")
            edit_bones.remove(bone)


def fix_metarig_limb_rotation_axes(metarig: bpy.types.Object):
    limb_bones = [
        r"^upper_arm\.(L|R)$",
        r"^thigh\.(L|R)$",
    ]

    finger_bones = [
        r"^f_pinky\.01\.(L|R)$",
        r"^f_ring\.01\.(L|R)$",
        r"^f_middle\.01\.(L|R)$",
        r"^f_index\.01\.(L|R)$",
        r"^thumb\.01\.(L|R)$",
    ]

    pose_bones = metarig.pose.bones
    for bone in objects_by_name_patterns(pose_bones, limb_bones):
        print(f"amending bone parameters for limb '{bone.name}'")
        # Ensure local bend direction is correct.
        bone.rigify_parameters.rotation_axis = 'x'

    # Amend armature fingers.
    for bone in objects_by_name_patterns(pose_bones, finger_bones):
        print(f"amending bone parameters for finger '{bone.name}'")
        # Ensure primary bend direction is correct.
        axis = 'Z' if bone.name.endswith('L') else '-Z'
        bone.rigify_parameters.primary_rotation_axis = axis


def invoke_rigify_generate(metarig: bpy.types.Object) -> bpy.types.Object:
    bpy.context.view_layer.objects.active = metarig
    bpy.ops.pose.rigify_generate()
    return bpy.context.view_layer.objects.active


def removed_generated_rig_facial_bones(rig_object: bpy.types.Object):
    rig_bones_to_delete_by_name_pattern = [
        # Facial expressions and features are managed by shape keys,
        # so we remove all facial bones except for eyes.
        r"^(ORG|DEF)-forehead.*$",
        r"^(ORG|DEF)-temple.*$",
        r"^((ORG|DEF)-)?brow.*$",
        r"^((MCH|ORG|DEF)-)?lid\.(B|T).*$",
        r"^((ORG|DEF)-)?ear\.(L|R).*$",
        r"^((MCH|ORG|DEF)-)?tongue.*$",
        r"^((ORG|DEF)-)?chin.*$",
        r"^((ORG|DEF)-)?cheek\.(B|T).*$",
        r"^(ORG-)?teeth\.(B|T)$",
        r"^((ORG|DEF)-)?nose.*$",
        r"^((ORG|DEF)-)?lip.*$",
        r"^((MCH|ORG|DEF)-)?jaw.*$",
        r"^MCH-mouth_lock$",
    ]

    armature_rig: bpy.types.Armature = rig_object.data
    with ModeContext.editing(rig_object):
        bones_to_remove = []
        for bone_root in objects_by_name_patterns(armature_rig.edit_bones, rig_bones_to_delete_by_name_pattern):
            for bone in bone_root.children_recursive + [bone_root]:
                if bone not in bones_to_remove:
                    bones_to_remove.append(bone)

        for bone in bones_to_remove:
            print(f"deleting facial bone '{full_bone_path(bone)}'")
            armature_rig.edit_bones.remove(bone)


def rename_rig_bones_to_match_vrm_model_vertex_groups(rig_object: bpy.types.Object, bone_mapping):
    armature_rig: bpy.types.Armature = rig_object.data
    with ModeContext.editing(rig_object):
        for metarig_bone_name, vrm_bone_name in bone_mapping:
            if metarig_bone_name in ["eye.L", "eye.R"]:
                rig_bone_name = f"ORG-{metarig_bone_name}"
            else:
                rig_bone_name = f"DEF-{metarig_bone_name}"

            # Rigify may not generate a bone for every mapped metarig bone
            # depending on the model and the Rigify version.
            if rig_bone_name not in armature_rig.edit_bones:
                print(f"rig bone '{rig_bone_name}' not found so skipping rename")
                continue

            rig_bone = armature_rig.edit_bones[rig_bone_name]
            rig_bone.use_deform = True
            print(f"renaming bone '{full_bone_path(rig_bone)}' to '{vrm_bone_name}'")
            rig_bone.name = vrm_bone_name


def attach_unmapped_vrm_model_bones_to_rig(rig_object: bpy.types.Object, vrm_object: bpy.types.Object):
    armature_rig: bpy.types.Armature = rig_object.data
    armature_vrm: bpy.types.Armature = vrm_object.data
    with ModeContext.editing(rig_object):
        # Assume retrieved bones are in traversal order.
        for vrm_bone in armature_vrm.bones:
            bone_already_in_rig = vrm_bone.name in armature_rig.edit_bones
            vrm_bone_has_parent = bool(vrm_bone.parent)
            if bone_already_in_rig or not vrm_bone_has_parent:
                continue

            vrm_bone_parent_name = vrm_bone.parent.name
            parent_exists_in_rig = vrm_bone_parent_name in armature_rig.edit_bones
            if not parent_exists_in_rig:
                continue

            parent_bone_in_rig = armature_rig.edit_bones[vrm_bone_parent_name]
            print(f"generating bone '{full_bone_path(parent_bone_in_rig)}/{vrm_bone.name}'")

            bone_in_rig = armature_rig.edit_bones.new(vrm_bone.name)
            bone_in_rig.head = vrm_bone.head_local
            bone_in_rig.tail = vrm_bone.tail_local
            bone_in_rig.parent = parent_bone_in_rig

            # Inherit the parent bone collections for the generated bone.
            for collection in parent_bone_in_rig.collections:
                collection.assign(bone_in_rig)


def assign_id_property(container, key: str, value):
    # Blender 5.0 disallows assigning over an existing
    # group property so remove any existing property first.
    if key in container:
        del container[key]
    container[key] = value


# Enables use of the blend shape proxy and expressions panel from the VRM addon.
def copy_shape_key_controls_from_vrm_armature(rig_object: bpy.types.Object, vrm_object: bpy.types.Object):
    armature_rig: bpy.types.Armature = rig_object.data
    armature_vrm: bpy.types.Armature = vrm_object.data
    blend_shape_master = armature_vrm.vrm_addon_extension.vrm0["blend_shape_master"]
    assign_id_property(armature_rig.vrm_addon_extension.vrm0, "blend_shape_master", blend_shape_master)
    expressions = armature_vrm.vrm_addon_extension.vrm1["expressions"]
    assign_id_property(armature_rig.vrm_addon_extension.vrm1, "expressions", expressions)


def disable_ik_stretching(rig_object: bpy.types.Object):
    for bone in rig_object.pose.bones:
        stretch_key = "IK_Stretch"
        if stretch_key in bone:
            bone[stretch_key] = 0.0


def move_armature_modifiers_to_top(mesh_object: bpy.types.Object):
    armature_modifiers = [
        modifier for modifier in mesh_object.modifiers
        if modifier.type == "ARMATURE"
    ]

    bpy.context.view_layer.objects.active = mesh_object
    mesh_object.select_set(True)
    for target_index, modifier in enumerate(armature_modifiers):
        while list(mesh_object.modifiers).index(modifier) > target_index:
            result = bpy.ops.object.modifier_move_up(modifier=modifier.name)
            if result != {"FINISHED"}:
                raise Exception(
                    f"Failed to move armature modifier '{modifier.name}' "
                    f"on mesh '{mesh_object.name}'"
                )


def vrm_model_mesh_objects(vrm_object: bpy.types.Object):
    descendants = set(vrm_object.children_recursive)
    return [
        node for node in bpy.context.view_layer.objects
        if node.type == "MESH" and (
            node in descendants
            or any(
                modifier.type == "ARMATURE"
                and modifier.object == vrm_object
                for modifier in node.modifiers
            )
        )
    ]


def attach_vrm_model_meshes_to_rig(
    rig_object: bpy.types.Object,
    vrm_object: bpy.types.Object,
) -> list[bpy.types.Object]:
    mesh_objects = vrm_model_mesh_objects(vrm_object)
    if not mesh_objects:
        raise Exception(f"No meshes found for VRM armature '{vrm_object.name}'")

    # Use Blender's Armature Deform parenting operation rather than emulating
    # it with direct property assignments. Besides creating the parent
    # relationship, the operation initializes the armature modifier and parent
    # inverse in the same way as Object > Parent > Armature Deform.
    bpy.ops.object.select_all(action="DESELECT")
    for mesh_object in mesh_objects:
        mesh_object.select_set(True)
    rig_object.select_set(True)
    bpy.context.view_layer.objects.active = rig_object
    result = bpy.ops.object.parent_set(type="ARMATURE")
    if result != {"FINISHED"}:
        raise Exception("Failed to parent VRM meshes to the generated rig")

    for mesh_object in mesh_objects:
        has_rig_modifier = any(
            modifier.type == "ARMATURE"
            and modifier.object == rig_object
            for modifier in mesh_object.modifiers
        )
        if mesh_object.parent != rig_object or not has_rig_modifier:
            raise Exception(
                f"Armature Deform did not attach mesh '{mesh_object.name}' "
                f"to rig '{rig_object.name}'"
            )

        # The new modifier created by the parenting operation replaces the
        # imported VRM modifier. Keeping both would run two armature deformers
        # over the same vertex groups.
        for modifier in list(mesh_object.modifiers):
            if modifier.type == "ARMATURE" and modifier.object == vrm_object:
                mesh_object.modifiers.remove(modifier)

        move_armature_modifiers_to_top(mesh_object)

    return mesh_objects


def find_vrm_source_armature(rig_object: bpy.types.Object):
    source_name = rig_object.get("vrm_rigify_source_armature")
    if source_name:
        source_object = bpy.data.objects.get(source_name)
        if source_object is not None and source_object.type == "ARMATURE":
            return source_object

    candidates = {
        modifier.object
        for mesh_object in bpy.context.view_layer.objects
        if mesh_object.type == "MESH"
        for modifier in mesh_object.modifiers
        if modifier.type == "ARMATURE"
        and modifier.object is not None
        and modifier.object != rig_object
    }
    if len(candidates) != 1:
        names = sorted(candidate.name for candidate in candidates)
        raise Exception(
            "Expected exactly one source VRM armature for "
            f"'{rig_object.name}', found: {names}"
        )
    return candidates.pop()


def mark_completed_conversion(
    rig_object: bpy.types.Object,
    vrm_object: bpy.types.Object,
    mesh_objects: list[bpy.types.Object],
):
    rig_object["vrm_rigify_version"] = addon_version_string()
    rig_object["vrm_rigify_source_armature"] = vrm_object.name
    rig_object["vrm_rigify_attached_mesh_count"] = len(mesh_objects)


def enter_pose_mode(rig_object: bpy.types.Object):
    bpy.ops.object.select_all(action="DESELECT")
    rig_object.hide_set(False)
    rig_object.select_set(True)
    bpy.context.view_layer.objects.active = rig_object
    bpy.ops.object.mode_set(mode="POSE")


class GenerateVRMRig(bpy.types.Operator):
    bl_idname = "vrm_rigify.create_rig"
    bl_label = "Generate Rigify armature for VRM model"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return (
            context.mode == "OBJECT"
            and context.active_object is not None
            and context.active_object.type == "ARMATURE"
        )

    def execute(self, context):
        vrm_object = context.active_object

        metarig = generate_template_metarig(f"{vrm_object.name}.metarig")
        bone_mapping = compute_metarig_and_vrm_model_bone_mapping(metarig, vrm_object)
        remove_metarig_palm_bones(metarig)
        remove_or_log_unmapped_metarig_bones(metarig, bone_mapping)
        position_metarig_bones_to_vrm_model(metarig, vrm_object, bone_mapping)
        fix_position_of_metarig_spine_bones(metarig, bone_mapping)
        fix_metarig_limb_rotation_axes(metarig)
        rig_object = invoke_rigify_generate(metarig)

        removed_generated_rig_facial_bones(rig_object)
        rename_rig_bones_to_match_vrm_model_vertex_groups(rig_object, bone_mapping)
        attach_unmapped_vrm_model_bones_to_rig(rig_object, vrm_object)
        copy_shape_key_controls_from_vrm_armature(rig_object, vrm_object)
        disable_ik_stretching(rig_object)
        mesh_objects = attach_vrm_model_meshes_to_rig(rig_object, vrm_object)

        metarig.hide_set(True)
        vrm_object.hide_set(True)
        enter_pose_mode(rig_object)
        mark_completed_conversion(rig_object, vrm_object, mesh_objects)
        self.report(
            {"INFO"},
            f"VRM Rigify {addon_version_string()}: generated "
            f"'{rig_object.name}' and attached "
            f"{len(mesh_objects)} mesh object(s)",
        )
        return {"FINISHED"}


CLASSES = [
    GenerateVRMRig,
]


def draw_object_menu(self, _context):
    self.layout.separator()
    self.layout.operator(GenerateVRMRig.bl_idname)


def register():
    for clazz in CLASSES:
        bpy.utils.register_class(clazz)
    bpy.types.VIEW3D_MT_object.append(draw_object_menu)


def unregister():
    bpy.types.VIEW3D_MT_object.remove(draw_object_menu)
    for clazz in reversed(CLASSES):
        bpy.utils.unregister_class(clazz)


if __name__ == "__main__":
    register()
