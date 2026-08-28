import time
import unittest
import weakref
from unittest.mock import patch

from fastapi import HTTPException

import api


class _Connection:
    def close(self):
        pass


class _Network:
    pass


class ApiMemorySafetyTests(unittest.TestCase):
    def tearDown(self):
        api._network_cache = None

    def test_changed_network_request_evicts_old_graph_before_build(self):
        old_network = _Network()
        old_network_ref = weakref.ref(old_network)
        api._network_cache = (("old-key",), time.monotonic(), old_network)
        del old_network
        replacement = _Network()
        weights = {
            "curviness": 0.7,
            "traffic": 0.6,
            "city_avoidance": 0.8,
            "scenery": 0.7,
            "paved_only": True,
        }

        def build_replacement(*_args, **_kwargs):
            self.assertIsNone(api._network_cache)
            self.assertIsNone(old_network_ref())
            return replacement

        with patch("api.get_connection", return_value=_Connection()), patch(
            "api.build_network", side_effect=build_replacement
        ):
            result = api._get_network("minnesota", -93.1, 44.8, 20_000, weights)

        self.assertIs(result, replacement)
        self.assertIs(api._network_cache[2], replacement)

    def test_overlapping_route_request_returns_retryable_error(self):
        request = api.RouteRequest(
            region="minnesota",
            start_lat=44.8,
            start_lon=-93.1,
            target_distance_mi=40,
        )
        api._route_generation_lock.acquire()
        try:
            with self.assertRaises(HTTPException) as raised:
                api.generate_route(request)
        finally:
            api._route_generation_lock.release()

        self.assertEqual(raised.exception.status_code, 429)
        self.assertIn("still being generated", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
