import logging
import os
import struct
from typing import Set, Tuple, Optional

import bpy
from mathutils import Matrix, Quaternion
import mixer
from mixer import ui
from mixer.bl_utils import get_mixer_prefs, get_mixer_props
from mixer.share_data import share_data
from mixer.broadcaster import common
from mixer.broadcaster.common import ClientAttributes, MessageType, RoomAttributes
from mixer.broadcaster.client import Client
from mixer.blender_client import camera as camera_api
from mixer.blender_client import collection as collection_api
from mixer.blender_client import data as data_api
from mixer.blender_client import grease_pencil as grease_pencil_api
from mixer.blender_client import light as light_api
from mixer.blender_client import material as material_api
from mixer.blender_client import mesh as mesh_api
from mixer.blender_client import object_ as object_api
from mixer.blender_client import scene as scene_api
import mixer.shot_manager as shot_manager
from mixer.stats import stats_timer
from mixer.draw import set_draw_handlers

from mixer.blender_client.camera import send_camera
from mixer.blender_client.light import send_light

logger = logging.getLogger(__name__)


def get_target(region: bpy.types.Region, region_3d: bpy.types.RegionView3D, pixel_coords: Tuple[float, float]):
    from bpy_extras import view3d_utils

    view_vector = view3d_utils.region_2d_to_vector_3d(region, region_3d, pixel_coords)
    ray_origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, pixel_coords)
    target = ray_origin + view_vector

    return [target.x, target.y, target.z]


def get_view_frustum_attributes(region: bpy.types.Region, region_3d: bpy.types.RegionView3D):
    from bpy_extras import view3d_utils

    width = region.width
    height = region.height

    eye = view3d_utils.region_2d_to_origin_3d(region, region_3d, (width * 0.5, height * 0.5))
    v1 = get_target(region, region_3d, (0, 0))  # bottom left
    v2 = get_target(region, region_3d, (width, 0))  # bottom right
    v3 = get_target(region, region_3d, (width, height))  # top right
    v4 = get_target(region, region_3d, (0, height))  # top left

    return {
        ClientAttributes.USERSCENES_VIEWS_EYE: list(eye),
        ClientAttributes.USERSCENES_VIEWS_TARGET: list(region_3d.view_location),
        ClientAttributes.USERSCENES_VIEWS_SCREEN_CORNERS: [v1, v2, v3, v4],
    }


class SendSceneContentFailed(Exception):
    pass


class BlenderClient(Client):
    def __init__(self, host=common.DEFAULT_HOST, port=common.DEFAULT_PORT):
        super(BlenderClient, self).__init__(host, port)

        self.textures: Set[str] = set()

        self.skip_next_depsgraph_update = False
        # skip_next_depsgraph_update is set to True in the main timer function when a received command
        # affect blender data and will trigger a depsgraph update; in that case we want to ignore it
        # because it will produce some kind of infinite recursive update
        self.block_signals = False
        # block_signals is set to True when our timer transforms received commands into scene updates

        self._joining: bool = False
        self._joining_room_name: Optional[str] = None
        self._received_command_count: int = 0
        self._received_byte_size: int = 0

    # returns the path of an object
    def get_object_path(self, obj):
        return mixer.blender_client.misc.get_object_path(obj)

    # get first collection
    def get_or_create_collection(self, name: str):
        collection = share_data.blender_collections.get(name)
        if not collection:
            collection = bpy.data.collections.new(name)
            share_data._blender_collections[name] = collection
            bpy.context.scene.collection.children.link(collection)
            share_data.update_collection_temporary_visibility(name)
        return collection

    def get_or_create_path(self, path, data=None) -> bpy.types.Object:
        return mixer.blender_client.misc.get_or_create_path(path, data)

    def get_or_create_object_data(self, path, data):
        return self.get_or_create_path(path, data)

    def get_or_create_mesh(self, mesh_name):
        me = share_data.blender_meshes.get(mesh_name)
        if not me:
            me = bpy.data.meshes.new(mesh_name)
            share_data._blender_meshes[me.name_full] = me
        return me

    def set_transform(self, obj, parent_inverse_matrix, basis_matrix, local_matrix):
        obj.matrix_parent_inverse = parent_inverse_matrix
        obj.matrix_basis = basis_matrix
        obj.matrix_local = local_matrix

    def build_matrix_from_components(self, translate, rotate, scale):
        t = Matrix.Translation(translate)
        r = Quaternion(rotate).to_matrix().to_4x4()
        s = Matrix()
        s[0][0] = scale[0]
        s[1][1] = scale[1]
        s[2][2] = scale[2]
        return s @ r @ t

    def decode_matrix(self, data, index):
        matrix_data, index = common.decode_matrix(data, index)
        m = Matrix()
        m.col[0] = matrix_data[0]
        m.col[1] = matrix_data[1]
        m.col[2] = matrix_data[2]
        m.col[3] = matrix_data[3]
        return m, index

    def build_transform(self, data):
        start = 0
        object_path, start = common.decode_string(data, start)
        parent_invert_matrix, start = self.decode_matrix(data, start)
        basis_matrix, start = self.decode_matrix(data, start)
        local_matrix, start = self.decode_matrix(data, start)

        try:
            obj = self.get_or_create_path(object_path)
        except KeyError:
            # Object doesn't exist anymore
            return
        if obj:
            self.set_transform(obj, parent_invert_matrix, basis_matrix, local_matrix)

    def build_rename(self, data):
        # Object rename, actually
        # renaming the data referenced by Object.data (Light, Camera, ...) is not supported
        old_path, index = common.decode_string(data, 0)
        new_path, index = common.decode_string(data, index)
        logger.info("build_rename %s into %s", old_path, new_path)
        old_name = old_path.split("/")[-1]
        new_name = new_path.split("/")[-1]
        share_data.blender_objects.get(old_name).name = new_name
        share_data.blender_objects_dirty = True
        share_data.old_objects = share_data.blender_objects

    def build_duplicate(self, data):
        src_path, index = common.decode_string(data, 0)
        dst_name, index = common.decode_string(data, index)
        basis_matrix, index = self.decode_matrix(data, index)

        try:
            obj = self.get_or_create_path(src_path)
            new_obj = obj.copy()
            new_obj.name = dst_name
            if hasattr(obj, "data"):
                new_obj.data = obj.data.copy()
                new_obj.data.name = dst_name
                new_obj.animation_data_clear()
            for collection in obj.users_collection:
                collection.objects.link(new_obj)

            self.set_transform(new_obj, obj.matrix_parent_invert, basis_matrix, obj.matrix_parent_invert @ basis_matrix)
        except Exception:
            pass

    def build_delete(self, data):
        path, _ = common.decode_string(data, 0)

        try:
            obj = share_data.blender_objects[path.split("/")[-1]]
        except KeyError:
            # Object doesn't exist anymore
            return
        del share_data._blender_objects[obj.name_full]
        bpy.data.objects.remove(obj, do_unlink=True)

    def build_send_to_trash(self, data):
        path, _ = common.decode_string(data, 0)
        obj = self.get_or_create_path(path)

        share_data.restore_to_collections[obj.name_full] = []
        restore_to = share_data.restore_to_collections[obj.name_full]
        for collection in obj.users_collection:
            restore_to.append(collection.name_full)
            collection.objects.unlink(obj)
        # collection = self.get_or_create_collection()
        # collection.objects.unlink(obj)
        trash_collection = self.get_or_create_collection("__Trash__")
        trash_collection.hide_viewport = True
        trash_collection.objects.link(obj)

    def build_restore_from_trash(self, data):
        name, index = common.decode_string(data, 0)
        path, index = common.decode_string(data, index)

        obj = share_data.blender_objects[name]
        trash_collection = self.get_or_create_collection("__Trash__")
        trash_collection.hide_viewport = True
        trash_collection.objects.unlink(obj)
        restore_to = share_data.restore_to_collections[obj.name_full]
        for collection_name in restore_to:
            collection = self.get_or_create_collection(collection_name)
            collection.objects.link(obj)
        del share_data.restore_to_collections[obj.name_full]
        if len(path) > 0:
            parent_name = path.split("/")[-1]
            obj.parent = share_data.blender_objects.get(parent_name, None)

    def get_transform_buffer(self, obj):
        path = self.get_object_path(obj)
        return (
            common.encode_string(path)
            + common.encode_matrix(obj.matrix_parent_inverse)
            + common.encode_matrix(obj.matrix_basis)
            + common.encode_matrix(obj.matrix_local)
        )

    def send_transform(self, obj):
        transform_buffer = self.get_transform_buffer(obj)
        self.add_command(common.Command(MessageType.TRANSFORM, transform_buffer, 0))

    def build_texture_file(self, data):
        path, index = common.decode_string(data, 0)
        if not os.path.exists(path):
            size, index = common.decode_int(data, index)
            try:
                f = open(path, "wb")
                f.write(data[index : index + size])
                f.close()
                self.textures.add(path)
            except Exception as e:
                logger.error("could not write file %s ...", path)
                logger.error("... %s", e)

    def send_texture_file(self, path):
        if path in self.textures:
            return
        if os.path.exists(path):
            try:
                f = open(path, "rb")
                data = f.read()
                f.close()
                self.send_texture_data(path, data)
            except Exception as e:
                logger.error("could not read file %s ...", path)
                logger.error("... %s", e)

    def send_texture_data(self, path, data):
        name_buffer = common.encode_string(path)
        self.textures.add(path)
        self.add_command(common.Command(MessageType.TEXTURE, name_buffer + common.encode_int(len(data)) + data, 0))

    def get_texture(self, inputs):
        if not inputs:
            return None
        if len(inputs.links) == 1:
            connected_node = inputs.links[0].from_node
            if type(connected_node).__name__ == "ShaderNodeTexImage":
                image = connected_node.image
                pack = image.packed_file
                path = bpy.path.abspath(image.filepath)
                path = path.replace("\\", "/")
                if pack:
                    self.send_texture_data(path, pack.data)
                else:
                    self.send_texture_file(path)
                return path
        return None

    def build_add_keyframe(self, data):
        index = 0
        name, index = common.decode_string(data, index)
        if name not in share_data.blender_objects:
            return name
        ob = share_data.blender_objects[name]
        channel, index = common.decode_string(data, index)
        channel_index, index = common.decode_int(data, index)
        frame, index = common.decode_int(data, index)
        value, index = common.decode_float(data, index)

        if not hasattr(ob, channel):
            ob = ob.data

        attr = getattr(ob, channel)
        if channel_index != -1:
            attr[channel_index] = value
        else:
            attr = value
        setattr(ob, channel, attr)
        ob.keyframe_insert(channel, frame=float(frame), index=channel_index)
        return name

    def build_remove_keyframe(self, data):
        index = 0
        name, index = common.decode_string(data, index)
        if name not in share_data.blender_objects:
            return name
        ob = share_data.blender_objects[name]
        channel, index = common.decode_string(data, index)
        channel_index, index = common.decode_int(data, index)
        if not hasattr(ob, channel):
            ob = ob.data
        ob.keyframe_delete(channel, index=channel_index)
        return name

    def build_query_object_data(self, data):
        index = 0
        name, index = common.decode_string(data, index)
        self.query_object_data(name)

    def build_clear_animations(self, data):
        index = 0
        name, index = common.decode_string(data, index)
        ob = share_data.blender_objects[name]
        ob.animation_data_clear()
        if ob.data:
            ob.data.animation_data_clear()

    def build_montage_mode(self, data):
        index = 0
        montage, index = common.decode_bool(data, index)
        winman = bpy.data.window_managers["WinMan"]
        if hasattr(winman, "UAS_shot_manager_handler_toggle"):
            winman.UAS_shot_manager_handler_toggle = montage

    def send_group_begin(self):
        # The integer sent is for future use: the server might fill it with the group size once all messages
        # have been received, and give the opportunity to future clients to know how many messages they need to process
        # in the group (en probably show a progress bar to their user if their is a lot of message, e.g. initial scene
        # creation)
        self.add_command(common.Command(MessageType.GROUP_BEGIN, common.encode_int(0)))

    def send_group_end(self):
        self.add_command(common.Command(MessageType.GROUP_END))

    def send_material(self, material):
        if not material:
            return
        if material.grease_pencil:
            grease_pencil_api.send_grease_pencil_material(self, material)
        else:
            self.add_command(common.Command(MessageType.MATERIAL, material_api.get_material_buffer(self, material), 0))

    def get_mesh_name(self, mesh):
        return mesh.name_full

    @stats_timer(share_data)
    def send_mesh(self, obj):
        logger.info("send_mesh %s", obj.name_full)
        mesh = obj.data
        mesh_name = self.get_mesh_name(mesh)
        path = self.get_object_path(obj)

        binary_buffer = common.encode_string(path) + common.encode_string(mesh_name)

        binary_buffer += mesh_api.encode_mesh(
            obj, get_mixer_prefs().send_base_meshes, get_mixer_prefs().send_baked_meshes
        )

        # For now include material slots in the same message, but maybe it should be a separated message
        # like Transform
        material_link_dict = {"OBJECT": 0, "DATA": 1}
        material_links = [material_link_dict[slot.link] for slot in obj.material_slots]
        assert len(material_links) == len(obj.data.materials)
        binary_buffer += struct.pack(f"{len(material_links)}I", *material_links)

        for slot in obj.material_slots:
            if slot.link == "DATA":
                binary_buffer += common.encode_string("")
            else:
                binary_buffer += common.encode_string(slot.material.name if slot.material is not None else "")

        self.add_command(common.Command(MessageType.MESH, binary_buffer, 0))

    @stats_timer(share_data)
    def build_mesh(self, command_data):
        index = 0

        path, index = common.decode_string(command_data, index)
        mesh_name, index = common.decode_string(command_data, index)
        logger.info("build_mesh %s", mesh_name)
        obj = self.get_or_create_object_data(path, self.get_or_create_mesh(mesh_name))
        if obj.mode == "EDIT":
            logger.error("Received a mesh for object %s while begin in EDIT mode, ignoring.", path)
            return

        if obj.data is None:
            logger.warning(f"build_mesh: obj.data is None for {obj}")
            return

        index = mesh_api.decode_mesh(self, obj, command_data, index)

        material_slot_count = len(obj.data.materials)
        material_link_dict = ["OBJECT", "DATA"]
        material_links = struct.unpack(f"{material_slot_count}I", command_data[index : index + 4 * material_slot_count])
        for link, slot in zip(material_links, obj.material_slots):
            slot.link = material_link_dict[link]
        index += 4 * material_slot_count

        for slot in obj.material_slots:
            material_name, index = common.decode_string(command_data, index)
            if slot.link == "OBJECT" and material_name != "":
                slot.material = material_api.get_or_create_material(material_name)

    def send_set_current_scene(self, name):
        buffer = common.encode_string(name)
        self.add_command(common.Command(MessageType.SET_SCENE, buffer, 0))

    def send_animation_buffer(self, obj_name, animation_data, channel_name, channel_index=-1):
        if not animation_data:
            return
        action = animation_data.action
        if not action:
            return
        for fcurve in action.fcurves:
            if fcurve.data_path == channel_name:
                if channel_index == -1 or fcurve.array_index == channel_index:
                    key_count = len(fcurve.keyframe_points)
                    times = []
                    values = []
                    for keyframe in fcurve.keyframe_points:
                        times.append(int(keyframe.co[0]))
                        values.append(keyframe.co[1])
                    buffer = (
                        common.encode_string(obj_name)
                        + common.encode_string(channel_name)
                        + common.encode_int(channel_index)
                        + common.int_to_bytes(key_count, 4)
                        + struct.pack(f"{len(times)}i", *times)
                        + struct.pack(f"{len(values)}f", *values)
                    )
                    self.add_command(common.Command(MessageType.CAMERA_ANIMATION, buffer, 0))
                    return

    def send_camera_animations(self, obj):
        self.send_animation_buffer(obj.name_full, obj.animation_data, "location", 0)
        self.send_animation_buffer(obj.name_full, obj.animation_data, "location", 1)
        self.send_animation_buffer(obj.name_full, obj.animation_data, "location", 2)
        self.send_animation_buffer(obj.name_full, obj.animation_data, "rotation_euler", 0)
        self.send_animation_buffer(obj.name_full, obj.animation_data, "rotation_euler", 1)
        self.send_animation_buffer(obj.name_full, obj.animation_data, "rotation_euler", 2)
        self.send_animation_buffer(obj.name_full, obj.data.animation_data, "lens")

    def send_camera_attributes(self, obj):
        buffer = (
            common.encode_string(obj.name_full)
            + common.encode_float(obj.data.lens)
            + common.encode_float(obj.data.dof.aperture_fstop)
            + common.encode_float(obj.data.dof.focus_distance)
        )
        self.add_command(common.Command(MessageType.CAMERA_ATTRIBUTES, buffer, 0))

    def send_current_camera(self, camera_name):
        buffer = common.encode_string(camera_name)
        self.add_command(common.Command(MessageType.CURRENT_CAMERA, buffer, 0))

    def send_deleted_object(self, obj_name):
        self.send_delete(obj_name)

    def send_renamed_objects(self, old_name, new_name):
        if old_name != new_name:
            self.send_rename(old_name, new_name)

    def get_rename_buffer(self, old_name, new_name):
        encoded_old_name = old_name.encode()
        encoded_new_name = new_name.encode()
        buffer = (
            common.int_to_bytes(len(encoded_old_name), 4)
            + encoded_old_name
            + common.int_to_bytes(len(encoded_new_name), 4)
            + encoded_new_name
        )
        return buffer

    def send_rename(self, old_name, new_name):
        logger.info("send_rename %s into %s", old_name, new_name)
        self.add_command(common.Command(MessageType.RENAME, self.get_rename_buffer(old_name, new_name), 0))

    def get_delete_buffer(self, name):
        encoded_name = name.encode()
        buffer = common.int_to_bytes(len(encoded_name), 4) + encoded_name
        return buffer

    def send_delete(self, obj_name):
        logger.info("send_delate %s", obj_name)
        self.add_command(common.Command(MessageType.DELETE, self.get_delete_buffer(obj_name), 0))

    def build_frame(self, data):
        start = 0
        frame, start = common.decode_int(data, start)
        if bpy.context.scene.frame_current != frame:
            previous_value = share_data.client.skip_next_depsgraph_update
            share_data.client.skip_next_depsgraph_update = False
            bpy.context.scene.frame_set(frame)
            share_data.client.skip_next_depsgraph_update = previous_value

    def send_frame(self, frame):
        self.add_command(common.Command(MessageType.FRAME, common.encode_int(frame), 0))

    def send_frame_start_end(self, start, end):
        self.add_command(
            common.Command(MessageType.FRAME_START_END, common.encode_int(start) + common.encode_int(end), 0)
        )

    def override_context(self):
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    override = bpy.context.copy()
                    override["window"] = window
                    override["screen"] = window.screen
                    override["area"] = window.screen.areas[0]
                    return override
        return None

    def build_play(self, command):
        ctx = self.override_context()
        if ctx:
            if not ctx["screen"].is_animation_playing:
                bpy.ops.screen.animation_play(ctx)

    def build_pause(self, command):
        ctx = self.override_context()
        if ctx:
            if ctx["screen"].is_animation_playing:
                bpy.ops.screen.animation_play(ctx)

    def query_object_data(self, object_name):
        previous_value = share_data.client.skip_next_depsgraph_update
        share_data.client.skip_next_depsgraph_update = False

        if object_name not in share_data.blender_objects:
            return
        ob = share_data.blender_objects[object_name]
        update_params(ob)

        share_data.client.skip_next_depsgraph_update = previous_value

    def query_current_frame(self):
        share_data.client.send_frame(bpy.context.scene.frame_current)

    def compute_client_custom_attributes(self):
        scene_attributes = {}
        for scene in bpy.data.scenes:
            scene_attributes[scene.name_full] = {ClientAttributes.USERSCENES_FRAME: scene.frame_current}
            scene_selection = set()
            for obj in scene.objects:
                for view_layer in scene.view_layers:
                    if obj.select_get(view_layer=view_layer):
                        scene_selection.add(obj.name_full)
            scene_attributes[scene.name_full][ClientAttributes.USERSCENES_SELECTED_OBJECTS] = list(scene_selection)
            scene_attributes[scene.name_full][ClientAttributes.USERSCENES_VIEWS] = dict()

        # Send information about opened windows and 3d areas
        # Will server later to display view frustums of users
        windows = []
        for wm in bpy.data.window_managers:
            for window in wm.windows:
                areas_3d = []
                scene = window.scene.name_full
                view_layer = window.view_layer.name
                screen = window.screen.name_full
                for area in window.screen.areas:
                    if area.type == "VIEW_3D":
                        for region in area.regions:
                            if region.type == "WINDOW":
                                view_id = str(area.as_pointer())
                                view_dict = get_view_frustum_attributes(region, area.spaces.active.region_3d)
                                scene_attributes[scene][ClientAttributes.USERSCENES_VIEWS][view_id] = view_dict
                                areas_3d.append(view_id)
                windows.append({"scene": scene, "view_layer": view_layer, "screen": screen, "areas_3d": areas_3d})

        return {"blender_windows": windows, common.ClientAttributes.USERSCENES: scene_attributes}

    @stats_timer(share_data)
    def network_consumer(self):
        assert self.is_connected()

        set_draw_handlers()

        # Loop remains infinite while we have GROUP_BEGIN commands without their corresponding GROUP_END received
        # todo Change this -> probably not a good idea because the sending client might disconnect before GROUP_END occurs
        # or it needs to be guaranteed by the server
        group_count = 0
        while True:
            received_commands = self.fetch_commands(get_mixer_prefs().commands_send_interval)

            set_dirty = True
            # Process all received commands
            for command in received_commands:
                if self._joining and command.type.value > common.MessageType.COMMAND.value:
                    self._received_byte_size += command.byte_size()
                    self._received_command_count += 1
                    if self._joining_room_name in self.rooms_attributes:
                        get_mixer_props().joining_percentage = (
                            self._received_byte_size
                            / self.rooms_attributes[self._joining_room_name][RoomAttributes.BYTE_SIZE]
                        )
                        ui.redraw()

                if command.type == MessageType.GROUP_BEGIN:
                    group_count += 1
                    continue

                if command.type == MessageType.GROUP_END:
                    group_count -= 1
                    continue

                if self.has_default_handler(command.type):
                    if command.type == MessageType.JOIN_ROOM and self._joining:
                        self._joining = False
                        get_mixer_props().joining_percentage = 1

                    ui.update_ui_lists()
                    self.block_signals = False  # todo investigate why we should but this to false here
                    continue

                if set_dirty:
                    share_data.set_dirty()
                    set_dirty = False

                self.block_signals = True

                try:
                    if command.type == MessageType.CONTENT:
                        # The server asks for scene content (at room creation)
                        try:
                            assert share_data.client.current_room is not None
                            self.set_room_attributes(
                                share_data.client.current_room,
                                {"experimental_sync": get_mixer_prefs().experimental_sync},
                            )
                            send_scene_content()
                            # Inform end of content
                            self.add_command(common.Command(MessageType.CONTENT))
                        except Exception as e:
                            raise SendSceneContentFailed() from e
                        continue

                    # Put this to true by default
                    # todo Check build commands that do not trigger depsgraph update
                    # because it can lead to ignoring real updates when a false positive is encountered
                    command_triggers_depsgraph_update = True

                    if command.type == MessageType.GREASE_PENCIL_MESH:
                        grease_pencil_api.build_grease_pencil_mesh(command.data)
                    elif command.type == MessageType.GREASE_PENCIL_MATERIAL:
                        grease_pencil_api.build_grease_pencil_material(command.data)
                    elif command.type == MessageType.GREASE_PENCIL_CONNECTION:
                        grease_pencil_api.build_grease_pencil_connection(command.data)

                    elif command.type == MessageType.CLEAR_CONTENT:
                        clear_scene_content()
                        self._joining = True
                        self._received_command_count = 0
                        self._received_byte_size = 0
                        get_mixer_props().joining_percentage = 0
                        ui.redraw()
                    elif command.type == MessageType.MESH:
                        self.build_mesh(command.data)
                    elif command.type == MessageType.TRANSFORM:
                        self.build_transform(command.data)
                    elif command.type == MessageType.MATERIAL:
                        material_api.build_material(command.data)
                    elif command.type == MessageType.ASSIGN_MATERIAL:
                        material_api.build_assign_material(command.data)
                    elif command.type == MessageType.DELETE:
                        self.build_delete(command.data)
                    elif command.type == MessageType.CAMERA:
                        camera_api.build_camera(command.data)
                    elif command.type == MessageType.LIGHT:
                        light_api.build_light(command.data)
                    elif command.type == MessageType.RENAME:
                        self.build_rename(command.data)
                    elif command.type == MessageType.DUPLICATE:
                        self.build_duplicate(command.data)
                    elif command.type == MessageType.SEND_TO_TRASH:
                        self.build_send_to_trash(command.data)
                    elif command.type == MessageType.RESTORE_FROM_TRASH:
                        self.build_restore_from_trash(command.data)
                    elif command.type == MessageType.TEXTURE:
                        self.build_texture_file(command.data)

                    elif command.type == MessageType.COLLECTION:
                        collection_api.build_collection(command.data)
                    elif command.type == MessageType.COLLECTION_REMOVED:
                        collection_api.build_collection_removed(command.data)

                    elif command.type == MessageType.INSTANCE_COLLECTION:
                        collection_api.build_collection_instance(command.data)

                    elif command.type == MessageType.ADD_COLLECTION_TO_COLLECTION:
                        collection_api.build_collection_to_collection(command.data)
                    elif command.type == MessageType.REMOVE_COLLECTION_FROM_COLLECTION:
                        collection_api.build_remove_collection_from_collection(command.data)
                    elif command.type == MessageType.ADD_OBJECT_TO_COLLECTION:
                        collection_api.build_add_object_to_collection(command.data)
                    elif command.type == MessageType.REMOVE_OBJECT_FROM_COLLECTION:
                        collection_api.build_remove_object_from_collection(command.data)

                    elif command.type == MessageType.ADD_COLLECTION_TO_SCENE:
                        scene_api.build_collection_to_scene(command.data)
                    elif command.type == MessageType.REMOVE_COLLECTION_FROM_SCENE:
                        scene_api.build_remove_collection_from_scene(command.data)
                    elif command.type == MessageType.ADD_OBJECT_TO_SCENE:
                        scene_api.build_add_object_to_scene(command.data)
                    elif command.type == MessageType.REMOVE_OBJECT_FROM_SCENE:
                        scene_api.build_remove_object_from_scene(command.data)

                    elif command.type == MessageType.SCENE:
                        scene_api.build_scene(command.data)
                    elif command.type == MessageType.SCENE_REMOVED:
                        scene_api.build_scene_removed(command.data)
                    elif command.type == MessageType.SCENE_RENAMED:
                        scene_api.build_scene_renamed(command.data)

                    elif command.type == MessageType.OBJECT_VISIBILITY:
                        object_api.build_object_visibility(command.data)

                    elif command.type == MessageType.FRAME:
                        self.build_frame(command.data)
                    elif command.type == MessageType.QUERY_CURRENT_FRAME:
                        self.query_current_frame()

                    elif command.type == MessageType.PLAY:
                        self.build_play(command.data)
                    elif command.type == MessageType.PAUSE:
                        self.build_pause(command.data)
                    elif command.type == MessageType.ADD_KEYFRAME:
                        self.build_add_keyframe(command.data)
                    elif command.type == MessageType.REMOVE_KEYFRAME:
                        self.build_remove_keyframe(command.data)
                    elif command.type == MessageType.QUERY_OBJECT_DATA:
                        self.build_query_object_data(command.data)

                    elif command.type == MessageType.CLEAR_ANIMATIONS:
                        self.build_clear_animations(command.data)
                    elif command.type == MessageType.SHOT_MANAGER_MONTAGE_MODE:
                        self.build_montage_mode(command.data)
                    elif command.type == MessageType.SHOT_MANAGER_ACTION:
                        shot_manager.build_shot_manager_action(command.data)

                    elif command.type == MessageType.BLENDER_DATA_UPDATE:
                        data_api.build_data_update(command.data)
                    elif command.type == MessageType.BLENDER_DATA_REMOVE:
                        data_api.build_data_remove(command.data)
                    else:
                        # Command is ignored, so no depsgraph update can be triggered
                        command_triggers_depsgraph_update = False

                    if command_triggers_depsgraph_update:
                        self.skip_next_depsgraph_update = True

                except Exception as e:
                    logger.warning(f"Exception during processing of message {str(command.type)} ...\n", stack_info=True)
                    if get_mixer_prefs().env == "development" or isinstance(e, SendSceneContentFailed):
                        raise

                self.block_signals = False

            if group_count == 0:
                break

        if not set_dirty:
            share_data.update_current_data()

        # Some objects may have been obtained before their parent
        # In that case we resolve parenting here
        # todo Parenting strategy should be changed: we should store the name of the parent in the command instead of
        # having a path as name
        if len(share_data.pending_parenting) > 0:
            remaining_parentings = set()
            for path in share_data.pending_parenting:
                path_elem = path.split("/")
                ob = None
                parent = None
                for elem in path_elem:
                    ob = share_data.blender_objects.get(elem)
                    if not ob:
                        remaining_parentings.add(path)
                        break
                    if ob.parent != parent:  # do it only if needed, otherwise it resets matrix_parent_inverse
                        ob.parent = parent
                    parent = ob
            share_data.pending_parenting = remaining_parentings

        self.set_client_attributes(self.compute_client_custom_attributes())


def update_params(obj):
    # send collection instances
    if obj.instance_type == "COLLECTION":
        collection_api.send_collection_instance(share_data.client, obj)
        return

    if not hasattr(obj, "data"):
        return

    typename = obj.bl_rna.name
    if obj.data:
        typename = obj.data.bl_rna.name

    supported_lights = ["Sun Light", "Point Light", "Spot Light", "Area Light"]
    if (
        typename != "Camera"
        and typename != "Mesh"
        and typename != "Curve"
        and typename != "Text Curve"
        and typename != "Grease Pencil"
        and typename not in supported_lights
    ):
        return

    if typename == "Camera":
        send_camera(share_data.client, obj)

    if typename in supported_lights:
        send_light(share_data.client, obj)

    if typename == "Grease Pencil":
        for material in obj.data.materials:
            share_data.client.send_material(material)
        grease_pencil_api.send_grease_pencil_mesh(share_data.client, obj)
        grease_pencil_api.send_grease_pencil_connection(share_data.client, obj)

    if typename == "Mesh" or typename == "Curve" or typename == "Text Curve":
        if obj.mode == "OBJECT":
            share_data.client.send_mesh(obj)


def clear_scene_content():
    with HandlerManager(False):

        data = [
            "cameras",
            "collections",
            "curves",
            "grease_pencils",
            "images",
            "lights",
            "objects",
            "materials",
            "metaballs",
            "meshes",
            "textures",
            "worlds",
            "sounds",
        ]

        for name in data:
            collection = getattr(bpy.data, name)
            for obj in collection:
                collection.remove(obj)

        # Cannot remove the last scene at this point, treat it differently
        for scene in bpy.data.scenes[:-1]:
            scene_api.delete_scene(scene)

        share_data.clear_before_state()

        if len(bpy.data.scenes) == 1:
            scene = bpy.data.scenes[0]
            scene.name = "__last_scene_to_be_removed__"


@stats_timer(share_data)
def send_scene_content():
    if get_mixer_prefs().no_send_scene_content:
        return

    with HandlerManager(False):
        # mesh baking may trigger depsgraph_updatewhen more than one view layer and
        # cause to reenter send_scene_data_to_server() and send duplicate messages

        share_data.clear_before_state()
        share_data.init_proxy()
        share_data.client.send_group_begin()

        # Temporary waiting for material sync. Should move to send_scene_data_to_server
        for material in bpy.data.materials:
            share_data.client.send_material(material)

        send_scene_data_to_server(None, None)

        shot_manager.send_scene()
        share_data.client.send_frame_start_end(bpy.context.scene.frame_start, bpy.context.scene.frame_end)
        share_data.start_frame = bpy.context.scene.frame_start
        share_data.end_frame = bpy.context.scene.frame_end
        share_data.client.send_frame(bpy.context.scene.frame_current)

        share_data.client.send_group_end()
