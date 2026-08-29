"""Replaceable, rate-limited address geocoding for Backroad.

The browser talks only to our backend.  That keeps provider details out of the
UI and lets deployments switch away from the public Nominatim service through
environment variables without shipping a new desktop build.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass


DEFAULT_BASE_URL = "https://nominatim.openstreetmap.org"
DEFAULT_USER_AGENT = "Backroad-Beta/0.2 (+https://github.com/ixbh/Backroads)"


class GeocodingError(RuntimeError):
    """A provider, network, or response error safe to translate to HTTP 502."""


@dataclass(frozen=True, slots=True)
class GeocodingResult:
    display_name: str
    lat: float
    lon: float
    category: str | None = None
    result_type: str | None = None


class NominatimGeocoder:
    """Small Nominatim adapter with a bounded cache and one-request/sec gate.

    Searches are explicit button presses, never autocomplete.  The cache and
    process-wide throttle comply with the public service's modest-use policy
    and also make repeated address lookups instant in the desktop beta.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        country_codes: str = "us",
        min_interval_seconds: float = 1.0,
        cache_size: int = 256,
        opener=urllib.request.urlopen,
        clock=time.monotonic,
        sleeper=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self.country_codes = country_codes
        self.min_interval_seconds = min_interval_seconds
        self.cache_size = cache_size
        self._opener = opener
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last_request_at: float | None = None
        self._cache: OrderedDict[tuple, tuple[GeocodingResult, ...]] = OrderedDict()

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        near_lat: float | None = None,
        near_lon: float | None = None,
    ) -> list[GeocodingResult]:
        normalized = " ".join(query.strip().split())
        if len(normalized) < 3:
            return []
        limit = max(1, min(5, int(limit)))
        key = (normalized.casefold(), limit, near_lat, near_lon, self.country_codes)

        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return list(cached)

            if self._last_request_at is not None:
                wait = self.min_interval_seconds - (self._clock() - self._last_request_at)
                if wait > 0:
                    self._sleeper(wait)

            params = {
                "q": normalized,
                "format": "jsonv2",
                "addressdetails": "1",
                "limit": str(limit),
                "countrycodes": self.country_codes,
            }
            if near_lat is not None and near_lon is not None:
                # A viewbox boosts nearby matches without excluding a valid
                # address just outside it.  This is a bias, not a hard bound.
                params["viewbox"] = (
                    f"{near_lon - 2.5:.5f},{near_lat + 2.0:.5f},"
                    f"{near_lon + 2.5:.5f},{near_lat - 2.0:.5f}"
                )
            request = urllib.request.Request(
                f"{self.base_url}/search?{urllib.parse.urlencode(params)}",
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.8",
                },
            )
            try:
                with self._opener(request, timeout=12) as response:
                    payload = json.load(response)
            except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
                raise GeocodingError("The address service is unavailable right now.") from error
            finally:
                self._last_request_at = self._clock()

            if not isinstance(payload, list):
                raise GeocodingError("The address service returned an unexpected response.")

            results = []
            for item in payload:
                try:
                    results.append(
                        GeocodingResult(
                            display_name=str(item["display_name"]),
                            lat=float(item["lat"]),
                            lon=float(item["lon"]),
                            category=item.get("category") or item.get("class"),
                            result_type=item.get("type"),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            frozen = tuple(results)
            self._cache[key] = frozen
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
            return list(frozen)


def geocoder_from_environment() -> NominatimGeocoder:
    return NominatimGeocoder(
        base_url=os.environ.get("GEOCODER_BASE_URL", DEFAULT_BASE_URL),
        user_agent=os.environ.get("GEOCODER_USER_AGENT", DEFAULT_USER_AGENT),
        country_codes=os.environ.get("GEOCODER_COUNTRY_CODES", "us"),
    )
