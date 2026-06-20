import time
import pytest

torch = pytest.importorskip("torch")
pytest.importorskip("torch_geometric")

from detection.gnn_model import (
    TGATWashRingDetector, safe_load_gnn_checkpoint, save_gnn_checkpoint, NODE_FEATURE_DIM,
)


def _random_graph(n_nodes=500, n_edges=1500):
    x = torch.rand(n_nodes, NODE_FEATURE_DIM)
    edge_index = torch.randint(0, n_nodes, (2, n_edges))
    edge_attr = torch.rand(n_edges, 3)
    edge_time = torch.rand(n_edges) * 14400
    return x, edge_index, edge_attr, edge_time


def test_forward_pass_shape_and_range():
    model = TGATWashRingDetector()
    x, ei, ea, et = _random_graph(50, 100)
    out = model(x, ei, ea, et)
    assert out.shape == (50, 1)
    assert torch.all(out >= 0) and torch.all(out <= 1)


def test_safe_load_uses_weights_only(tmp_path, monkeypatch):
    model = TGATWashRingDetector()
    path = tmp_path / "gnn_model.pt"
    save_gnn_checkpoint(model, str(path))

    calls = {}
    real_load = torch.load

    def spy_load(*args, **kwargs):
        calls["weights_only"] = kwargs.get("weights_only")
        if kwargs.get("weights_only") is False:
            raise AssertionError("unsafe torch.load(weights_only=False) was called")
        return real_load(*args, **kwargs)

    monkeypatch.setattr(torch, "load", spy_load)
    safe_load_gnn_checkpoint(str(path))
    assert calls["weights_only"] is True


def test_inference_benchmark_500_nodes_cpu():
    model = TGATWashRingDetector()
    x, ei, ea, et = _random_graph(500, 2000)
    model.eval()
    with torch.no_grad():
        start = time.perf_counter()
        model(x, ei, ea, et)
        elapsed_ms = (time.perf_counter() - start) * 1000
    assert elapsed_ms <= 200, f"GNN forward pass took {elapsed_ms:.1f}ms, exceeds 200ms budget"
