"""Generated gRPC / protobuf code for LedgerLens.

Regenerate after editing proto/ledgerlens/v1/scoring.proto with (from repo root,
matching the flat, non-package-nested layout of the files in this directory):

    python -m grpc_tools.protoc -I proto/ledgerlens/v1 \\
        --python_out=generated --grpc_python_out=generated \\
        proto/ledgerlens/v1/scoring.proto

grpc_tools.protoc's generated scoring_pb2_grpc.py imports scoring_pb2 with a
bare `import scoring_pb2`, which fails from within this package -- after
regenerating, restore the relative-import-with-fallback at the top of
scoring_pb2_grpc.py:

    try:
        from . import scoring_pb2 as scoring__pb2
    except ImportError:
        import scoring_pb2 as scoring__pb2

The committed gencode version must not exceed the `protobuf` runtime pinned
in requirements.txt (see google.protobuf.runtime_version.ValidateProtobufRuntimeVersion
at the top of scoring_pb2.py) -- a newer protoc than the pinned runtime
produces gencode the runtime then refuses to load.
"""

from generated import scoring_pb2, scoring_pb2_grpc

__all__ = ["scoring_pb2", "scoring_pb2_grpc"]
