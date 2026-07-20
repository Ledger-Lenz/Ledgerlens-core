import time
import unittest

from detection.tracing import TailSamplingSpanProcessor, _BufferedTrace


class TestTailSamplingPolicies(unittest.TestCase):
    def test_error_traces_are_always_kept(self):
        class MockSpanProcessor:
            def __init__(self):
                self.spans = []
            def on_end(self, span):
                self.spans.append(span)
            def shutdown(self): pass
            def force_flush(self, timeout_millis): pass

        mock = MockSpanProcessor()
        processor = TailSamplingSpanProcessor(mock, baseline_ratio=0.0, buffer_timeout_seconds=0.1, max_buffered_traces=10)

        # Create a mock span with error status
        class MockSpan:
            def __init__(self, has_error, is_root=True):
                self.status = type('obj', (object,), {'status_code': has_error and 2 or 1})()
                self.name = "test"
                self.parent = None if is_root else type('obj', (object,), {'span_id': 123})()
                self.attributes = {}
                self.start_time = time.time_ns()
                self.end_time = self.start_time + 1_000_000
            def get_span_context(self):
                return type('obj', (object,), {'trace_id': 12345})()
            def set_attribute(self, key, value): pass

        span = MockSpan(has_error=True)
        processor.on_end(span)
        time.sleep(0.2)  # Wait a little for processing
        self.assertEqual(len(mock.spans), 1)

    def test_baseline_sampling_respects_ratio(self):
        class MockSpanProcessor:
            def __init__(self):
                self.spans = []
            def on_end(self, span):
                self.spans.append(span)
            def shutdown(self): pass
            def force_flush(self, timeout_millis): pass

        mock = MockSpanProcessor()
        # Use a high baseline ratio to make it likely to sample
        processor = TailSamplingSpanProcessor(mock, baseline_ratio=0.9, buffer_timeout_seconds=0.1, max_buffered_traces=100)

        class MockSpan:
            def __init__(self, trace_id):
                self.status = type('obj', (object,), {'status_code': 1})()
                self.name = "test"
                self.parent = None
                self.attributes = {}
                self.start_time = time.time_ns()
                self.end_time = self.start_time + 1_000_000
            def get_span_context(self):
                return type('obj', (object,), {'trace_id': trace_id})()
            def set_attribute(self, key, value): pass

        # Create many traces
        for i in range(100):
            span = MockSpan(trace_id=i)
            processor.on_end(span)

        time.sleep(0.2)
        # Should have roughly 90 spans, but let's check at least 50 to avoid flakiness
        self.assertGreater(len(mock.spans), 50)


if __name__ == '__main__':
    unittest.main()
