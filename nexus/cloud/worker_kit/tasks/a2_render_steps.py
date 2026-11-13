# ruff: noqa: F821
# Account 2 task (nexus/timeline/remote_render.py compiles the steps). Appended to Account 1's nexus_worker.py when a
# worker script is built: `task`, `Context`, `ffprobe` and `_ffmpeg` are that script's globals.


@task("a2_render_steps")
def task_a2_render_steps(ctx: Context) -> dict:
    """Run Account 2's compiled FFmpeg steps. params: steps (list of argument lists with {in:<name>} / {work}
    placeholders), output (file name in {work}), expect {width,height,fps,duration}. One checkpoint per finished step."""
    import time as _t
    steps = ctx.params["steps"]
    done = int(ctx.resume_state.get("steps_done", 0))
    work = str(ctx.work)

    def sub(a):
        a = str(a).replace("{work}", work)
        if a.startswith("{in:") and a.endswith("}"):
            name = a[4:-1]
            if name not in ctx.inputs:
                raise ValueError(f"input {name!r} was not delivered to the worker (have {sorted(ctx.inputs)})")
            return str(ctx.inputs[name])
        return a

    t0 = _t.time()
    for i, st in enumerate(steps):
        if i < done:
            ctx.log(f"step {i}: done in an earlier attempt - skipped")
            continue
        _ffmpeg(["ffmpeg", "-hide_banner", "-nostdin", "-y", *[sub(a) for a in st]], ctx, ctx.bundle.get("max_runtime_s") or 3600)
        ctx.checkpoint(f"step_{i}", {"steps_done": i + 1})
    out = ctx.work / ctx.params.get("output", "master.mp4")
    info = ffprobe(out)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    dur = float((info.get("format") or {}).get("duration") or 0)
    exp = ctx.params.get("expect") or {}
    ok = (not exp or (v.get("width") == exp.get("width") and v.get("height") == exp.get("height")
                      and abs(dur - float(exp.get("duration", dur))) <= 2.0 / max(1, int(exp.get("fps", 25)))))
    ctx.artifact(out, "master", producer="account2", task="a2_render_steps")
    return {"steps": len(steps), "steps_skipped": done, "render_s": round(_t.time() - t0, 3), "output_duration_s": dur,
            "width": v.get("width"), "height": v.get("height"), "matches_expectation": ok}
