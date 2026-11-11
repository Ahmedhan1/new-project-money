"""Job handler used by the crash-recovery test. Each stage appends to a counter file so the test can
prove how many times it really executed across worker processes."""
import os
import time
from pathlib import Path

from nexus import jobs


def _count(name: str) -> None:
    with open(Path(os.environ["NEXUS_HOME"]) / f"ran_{name}", "a") as f:
        f.write("x")


@jobs.handler("crash-test")
def crash_test(job):
    def a():
        _count("a")
        return {"value": 21}

    def b():
        _count("b")
        marker = Path(os.environ["NEXUS_HOME"]) / "b_started"
        if not marker.exists():  # first attempt: signal the test, then hang until the worker is killed
            marker.write_text("1")
            time.sleep(120)
        return {"value": 2}

    def c():
        _count("c")
        return "done"

    x = job.stage("a", a)["value"] * job.stage("b", b)["value"]
    job.stage("c", c)
    return {"answer": x}
