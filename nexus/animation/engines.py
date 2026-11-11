"""External animation engines driven headlessly: Manim (2D explainers) and Blender (3D).

Users describe WHAT they want (a spec); NEXUS writes the scene script and runs the tool in a subprocess.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import config, costguard, db

MANIM_TEMPLATE = '''
from manim import *

SPEC = {spec}

class Explainer(Scene):
    def construct(self):
        self.camera.background_color = "#0b1020"
        title = Text(SPEC["title"], weight=BOLD).scale(0.9).to_edge(UP)
        self.play(Write(title), run_time=1.0)
        self.play(Create(Line(LEFT * 3, RIGHT * 3, color=BLUE).next_to(title, DOWN, buff=0.15)), run_time=0.5)
        # a row of labelled stages connected by arrows: the process the narration explains
        colors = [BLUE, TEAL, GREEN, YELLOW, ORANGE, RED]
        boxes = VGroup()
        for i, step in enumerate(SPEC["steps"]):
            label = Text(step["label"], font_size=26)
            box = RoundedRectangle(corner_radius=0.18, width=max(2.2, label.width + 0.6), height=1.1, color=colors[i % 6], fill_opacity=0.18)
            boxes.add(VGroup(box, label))
        boxes.arrange(RIGHT, buff=0.9)
        if boxes.width > 12.5:
            boxes.scale(12.5 / boxes.width)
        boxes.shift(UP * 0.3)
        caption = None
        prev = None
        for i, (grp, step) in enumerate(zip(boxes, SPEC["steps"])):
            anims = [FadeIn(grp, shift=UP * 0.3)]
            if prev is not None:
                arrow = Arrow(prev.get_right(), grp.get_left(), buff=0.1, color=GREY_B, stroke_width=4)
                anims.append(GrowArrow(arrow))
            self.play(*anims, run_time=0.7)
            new_caption = Text(step.get("detail", ""), font_size=24, color=GREY_A).to_edge(DOWN, buff=0.9)
            if caption is None:
                self.play(FadeIn(new_caption), run_time=0.4)
            else:
                self.play(ReplacementTransform(caption, new_caption), run_time=0.4)
            caption = new_caption
            self.play(Indicate(grp, scale_factor=1.08, color=colors[i % 6]), run_time=0.6)
            self.wait(SPEC.get("hold", 0.5))
            prev = grp
        # a dot travels through the whole pipeline to show the flow
        dot = Dot(color=WHITE, radius=0.12).move_to(boxes[0].get_center())
        self.play(FadeIn(dot), run_time=0.2)
        for grp in boxes[1:]:
            self.play(dot.animate.move_to(grp.get_center()), run_time=0.5)
        self.play(FadeOut(dot), run_time=0.2)
        self.wait(0.6)
'''


def manim_explainer(out: Path, title: str, steps: list[dict], quality: str = "m", hold: float = 0.5) -> Path:
    """2D explainer: `steps` = [{"label": "...", "detail": "..."}]. quality l=480p15, m=720p30, h=1080p60."""
    costguard.authorize("manim", "render")
    work = config.CACHE / "manim" / out.stem
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    scene = work / "scene.py"
    scene.write_text(MANIM_TEMPLATE.format(spec=json.dumps({"title": title, "steps": steps, "hold": hold}, ensure_ascii=False)))
    t0 = time.time()
    manim_bin = Path(sys.executable).parent / "manim"
    r = subprocess.run([str(manim_bin), "render", f"-q{quality}", "--media_dir", str(work / "media"), "--disable_caching",
                        "-o", "explainer.mp4", str(scene), "Explainer"], capture_output=True, text=True, cwd=work, timeout=1800)
    found = list((work / "media").rglob("explainer.mp4"))
    if r.returncode != 0 or not found:
        raise RuntimeError(f"manim failed: {r.stderr[-800:]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(found[0], out)
    db.metric("manim_seconds", time.time() - t0)
    return out


BLENDER_SCRIPT = '''
import bpy, math, sys, json
spec = json.loads(sys.argv[sys.argv.index("--") + 1])
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
sc.cycles.samples = spec["samples"]
sc.cycles.use_denoising = False
sc.cycles.max_bounces = 3
sc.render.resolution_x, sc.render.resolution_y = spec["size"]
sc.render.fps = spec["fps"]
sc.frame_start, sc.frame_end = 1, spec["frames"]
sc.render.image_settings.file_format = "PNG"
sc.render.filepath = spec["out"]
sc.render.threads_mode = "FIXED"; sc.render.threads = 4

def mat(name, color, metallic=0.0, rough=0.4, emit=0.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Metallic"].default_value = metallic
    b.inputs["Roughness"].default_value = rough
    if emit:
        b.inputs["Emission Color"].default_value = (*color, 1); b.inputs["Emission Strength"].default_value = emit
    return m

world = bpy.data.worlds.new("w"); sc.world = world; world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.012, 0.018, 0.04, 1)
world.node_tree.nodes["Background"].inputs[1].default_value = 1.0

bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, -1.0))
bpy.context.object.data.materials.append(mat("floor", (0.03, 0.04, 0.07), 0.0, 0.25))

# centre piece: extruded 3D text on a slowly rotating pedestal
bpy.ops.object.text_add(location=(0, 0, 0.1), rotation=(math.radians(90), 0, 0))
t = bpy.context.object
t.data.body = spec["text"]; t.data.extrude = 0.12; t.data.bevel_depth = 0.015; t.data.align_x = "CENTER"; t.data.align_y = "CENTER"
t.data.size = 1.1
t.data.materials.append(mat("gold", (1.0, 0.62, 0.12), 1.0, 0.22))

bpy.ops.mesh.primitive_cylinder_add(radius=2.4, depth=0.25, location=(0, 0, -0.85), vertices=96)
ped = bpy.context.object; ped.data.materials.append(mat("ped", (0.05, 0.3, 0.6), 0.6, 0.3))

# orbiting spheres: simple procedural animation
orbs = []
for i in range(spec["orbs"]):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.16, segments=32, ring_count=16)
    o = bpy.context.object; bpy.ops.object.shade_smooth()
    o.data.materials.append(mat(f"orb{i}", [(0.0, 0.75, 1.0), (1.0, 0.3, 0.45), (0.45, 0.9, 0.5)][i % 3], 0.2, 0.2, 2.5))
    orbs.append(o)
for f in range(1, spec["frames"] + 1):
    a = 2 * math.pi * (f - 1) / spec["frames"]
    for i, o in enumerate(orbs):
        ph = a * (1 + 0.5 * (i % 2)) + i * 2 * math.pi / len(orbs)
        o.location = (2.0 * math.cos(ph), 2.0 * math.sin(ph), 0.2 + 0.5 * math.sin(ph * 2 + i))
        o.keyframe_insert("location", frame=f)

bpy.ops.object.light_add(type="AREA", location=(3.5, -4.5, 5)); k = bpy.context.object
k.data.energy = 900; k.data.size = 4; k.rotation_euler = (math.radians(50), 0, math.radians(35))
bpy.ops.object.light_add(type="AREA", location=(-5, -2, 2.5)); r = bpy.context.object
r.data.energy = 350; r.data.size = 3; r.data.color = (0.5, 0.7, 1.0); r.rotation_euler = (math.radians(70), 0, math.radians(-65))

# camera on an orbit rig (animated empty), slight push-in
bpy.ops.object.empty_add(location=(0, 0, 0.1)); rig = bpy.context.object
bpy.ops.object.camera_add(location=(0, -7.2, 1.7)); cam = bpy.context.object
cam.parent = rig; sc.camera = cam
tr = cam.constraints.new("TRACK_TO"); tr.target = t; tr.track_axis = "TRACK_NEGATIVE_Z"; tr.up_axis = "UP_Y"
cam.data.lens = 40
rig.rotation_euler = (0, 0, math.radians(-28)); rig.keyframe_insert("rotation_euler", frame=1)
rig.rotation_euler = (0, 0, math.radians(28)); rig.keyframe_insert("rotation_euler", frame=spec["frames"])
cam.location = (0, -7.2, 1.7); cam.keyframe_insert("location", frame=1)
cam.location = (0, -6.0, 1.4); cam.keyframe_insert("location", frame=spec["frames"])
bpy.ops.render.render(animation=True)
'''


def blender_title(out: Path, text: str, seconds: float = 2.0, size=(640, 360), fps: int = 24, samples: int = 24, orbs: int = 5) -> dict:
    """3D title/product-style shot: extruded text, pedestal, orbiting emissive spheres, orbiting camera. Cycles on CPU."""
    costguard.authorize("blender", "render")
    from ..media import ff
    work = config.CACHE / "blender" / out.stem
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    script = work / "scene.py"
    script.write_text(BLENDER_SCRIPT)
    frames = int(seconds * fps)
    spec = {"text": text, "frames": frames, "fps": fps, "size": list(size), "samples": samples, "orbs": orbs, "out": str(work / "f_")}
    t0 = time.time()
    r = subprocess.run(["blender", "-b", "--python", str(script), "--", json.dumps(spec)], capture_output=True, text=True, timeout=3600)
    pngs = sorted(work.glob("f_*.png"))
    if r.returncode != 0 or len(pngs) < frames:
        raise RuntimeError(f"blender failed ({len(pngs)}/{frames} frames): {(r.stderr or r.stdout)[-800:]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    ff.run(["-framerate", fps, "-i", work / "f_%04d.png", *config.VCODEC, "-r", fps, out], op="encode-blender")
    elapsed = time.time() - t0
    db.metric("blender_sec_per_frame", elapsed / frames, size=f"{size[0]}x{size[1]}", samples=samples)
    return {"path": str(out), "frames": frames, "elapsed": round(elapsed, 1), "sec_per_frame": round(elapsed / frames, 2)}
