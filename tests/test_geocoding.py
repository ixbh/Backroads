import io
import json
import unittest

from geocoding import NominatimGeocoder


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class GeocodingTests(unittest.TestCase):
    def test_search_parses_results_and_caches_repeated_address(self):
        calls = []
        payload = [{
            "display_name": "123 Main Street, Cottage Grove, Minnesota",
            "lat": "44.8277",
            "lon": "-92.9438",
            "category": "place",
            "type": "house",
        }]

        def opener(request, timeout):
            calls.append((request, timeout))
            return _Response(json.dumps(payload).encode())

        geocoder = NominatimGeocoder(opener=opener, min_interval_seconds=0)
        first = geocoder.search("  123   Main Street, Cottage Grove MN ")
        second = geocoder.search("123 Main Street, Cottage Grove MN")

        self.assertEqual(len(calls), 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0].lat, 44.8277)
        self.assertIn("Backroad-Beta", calls[0][0].get_header("User-agent"))
        self.assertIn("countrycodes=us", calls[0][0].full_url)

    def test_near_coordinates_add_a_non_bounding_viewbox(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return _Response(b"[]")

        geocoder = NominatimGeocoder(opener=opener, min_interval_seconds=0)
        self.assertEqual(geocoder.search("Cottage Grove", near_lat=44.8, near_lon=-93.1), [])
        self.assertIn("viewbox=", calls[0].full_url)
        self.assertNotIn("bounded=1", calls[0].full_url)

    def test_short_queries_do_not_contact_provider(self):
        geocoder = NominatimGeocoder(opener=lambda *_args, **_kwargs: self.fail("called"))
        self.assertEqual(geocoder.search(" x "), [])


if __name__ == "__main__":
    unittest.main()
