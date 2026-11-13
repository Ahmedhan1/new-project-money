"""Minimal ONNX writer (protobuf wire format by hand) so a tiny network can be rebuilt from a safetensors file and run
with onnxruntime, without torch or the `onnx` package. Only the message fields NEXUS needs are encoded (onnx.proto3):

    ModelProto{ir_version=1, producer_name=2, graph=7, opset_import=8}
    GraphProto{node=1, name=2, initializer=5, input=11, output=12}
    NodeProto{input=1, output=2, name=3, op_type=4, attribute=5}
    AttributeProto{name=1, f=2, i=3, s=4, ints=8, type=20}     TensorProto{dims=1, data_type=2, name=8, raw_data=9}
    ValueInfoProto{name=1, type=2} TypeProto{tensor_type=1} Tensor{elem_type=1, shape=2} Shape{dim=1} Dim{value=1, param=2}

Also `read_safetensors(path)` -> {name: np.ndarray} (header JSON + raw little-endian data; F32/F16/BF16).
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import numpy as np

FLOAT, INT64 = 1, 7
ATTR_FLOAT, ATTR_INT, ATTR_STRING, ATTR_INTS = 1, 2, 3, 7


def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def f_int(field: int, v: int) -> bytes:
    return _key(field, 0) + _varint(int(v))


def f_bytes(field: int, v: bytes | str) -> bytes:
    b = v.encode() if isinstance(v, str) else v
    return _key(field, 2) + _varint(len(b)) + b


def f_float(field: int, v: float) -> bytes:
    return _key(field, 5) + struct.pack("<f", float(v))


def attr(name: str, v) -> bytes:
    if isinstance(v, float):
        body = f_bytes(1, name) + f_float(2, v) + f_int(20, ATTR_FLOAT)
    elif isinstance(v, int):
        body = f_bytes(1, name) + f_int(3, v) + f_int(20, ATTR_INT)
    elif isinstance(v, str):
        body = f_bytes(1, name) + f_bytes(4, v) + f_int(20, ATTR_STRING)
    else:
        body = f_bytes(1, name) + b"".join(f_int(8, x) for x in v) + f_int(20, ATTR_INTS)
    return body


def node(op: str, inputs: list[str], outputs: list[str], name: str = "", **attrs) -> bytes:
    b = b"".join(f_bytes(1, i) for i in inputs) + b"".join(f_bytes(2, o) for o in outputs)
    b += f_bytes(3, name or outputs[0]) + f_bytes(4, op)
    b += b"".join(f_bytes(5, attr(k, v)) for k, v in attrs.items())
    return b


def tensor(name: str, arr: np.ndarray) -> bytes:
    a = np.ascontiguousarray(arr)
    dt = {np.dtype(np.float32): FLOAT, np.dtype(np.int64): INT64}[a.dtype]
    return b"".join(f_int(1, d) for d in a.shape) + f_int(2, dt) + f_bytes(8, name) + f_bytes(9, a.astype(a.dtype.newbyteorder("<")).tobytes())


def value_info(name: str, dims: list) -> bytes:
    shape = b"".join(f_bytes(1, f_int(1, d) if isinstance(d, int) else f_bytes(2, d)) for d in dims)
    tt = f_int(1, FLOAT) + f_bytes(2, shape)
    return f_bytes(1, name) + f_bytes(2, f_bytes(1, tt))


def model(nodes: list[bytes], inits: list[bytes], inputs: list[bytes], outputs: list[bytes], opset: int = 13,
          name: str = "nexus") -> bytes:
    g = b"".join(f_bytes(1, n) for n in nodes) + f_bytes(2, name) + b"".join(f_bytes(5, t) for t in inits)
    g += b"".join(f_bytes(11, i) for i in inputs) + b"".join(f_bytes(12, o) for o in outputs)
    return f_int(1, 8) + f_bytes(2, "nexus.enhance.onnx_build") + f_bytes(7, g) + f_bytes(8, f_bytes(1, "") + f_int(2, opset))


def read_safetensors(path: str | Path) -> dict[str, np.ndarray]:
    data = Path(path).read_bytes()
    n = struct.unpack("<Q", data[:8])[0]
    header = json.loads(data[8:8 + n])
    base = 8 + n
    out = {}
    for k, v in header.items():
        if k == "__metadata__":
            continue
        s, e = v["data_offsets"]
        raw = data[base + s: base + e]
        if v["dtype"] == "F32":
            a = np.frombuffer(raw, "<f4")
        elif v["dtype"] == "F16":
            a = np.frombuffer(raw, "<f2").astype(np.float32)
        elif v["dtype"] == "BF16":
            a = (np.frombuffer(raw, "<u2").astype(np.uint32) << 16).view(np.float32)
        else:
            raise ValueError(f"unsupported dtype {v['dtype']} for {k}")
        out[k] = a.reshape(v["shape"]).astype(np.float32)
    return out
