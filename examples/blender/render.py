"""Self-authored Blender scene. Only native primitives, lighting and materials.

Called once per frame: process isolation is intentionally the slow, simple MVP.
"""
import argparse
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector

p = argparse.ArgumentParser()
p.add_argument("--frame", type=int, required=True)
p.add_argument("--output", required=True)
p.add_argument("--temp", required=True)
a = p.parse_args(sys.argv[sys.argv.index("--")+1:])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.preferences.filepaths.temporary_directory = a.temp
scene = bpy.context.scene
scene.render.engine = "BLENDER_EEVEE_NEXT"
scene.render.resolution_x = scene.render.resolution_y = 192
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "PNG"
scene.render.fps = 12
scene.frame_end = 12
scene.world = bpy.data.worlds.new("World")
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs[0].default_value = (0.055, 0.065, 0.09, 1)
scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.4


def material(name, color, roughness):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*color, 1)
    bsdf.inputs["Roughness"].default_value = roughness
    return m


bpy.ops.mesh.primitive_uv_sphere_add(segments=48, ring_count=24, location=(0, 0, 1.1))
subject = bpy.context.object
subject.name = "ProceduralSubject"
subject.data.materials.append(material("Blue ceramic", (0.025, 0.33, 0.72), 0.23))
bpy.ops.object.shade_smooth()
for frame in range(1, 13):
    subject.location.x = 0.24 * math.sin(frame / 12 * math.tau)
    subject.location.z = 1.15 + 0.10 * math.cos(frame / 12 * math.tau)
    subject.keyframe_insert("location", frame=frame)
bpy.ops.mesh.primitive_plane_add(size=200)
bpy.context.object.data.materials.append(material("Floor", (0.07, 0.085, 0.11), 0.7))
bpy.ops.object.light_add(type="AREA", location=(2, -3, 5))
bpy.context.object.data.energy = 450
bpy.context.object.data.shape = "DISK"
bpy.context.object.data.size = 4
bpy.ops.object.camera_add(location=(3, -6, 3.0))
camera = bpy.context.object
camera.rotation_euler = (Vector((0, 0, 1.0))-camera.location).to_track_quat("-Z", "Y").to_euler()
camera.data.type = "ORTHO"
camera.data.ortho_scale = 3.5
scene.camera = camera
scene.frame_set(a.frame)
scene.render.filepath = a.output
# Editable artifact saved outside the temporary directory and reproducible from this script.
if a.frame == 1:
    bpy.ops.wm.save_as_mainfile(filepath=str(Path(a.output).parent.parent / "demo.blend"))
bpy.ops.render.render(write_still=True)
