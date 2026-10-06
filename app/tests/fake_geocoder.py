"""A fake geocoder for tests: nothing here talks to the network."""

from typing import Callable, Dict, List, Optional, Tuple

from app.services.geocoding import GeocodeResult


class FakeGeocoder:
    """Returns a canned result (or None) and remembers every address it was asked about."""

    enabled = True

    def __init__(self, result: Optional[GeocodeResult] = None, *, raises: Optional[Exception] = None,
                 on_call: Optional[Callable[[], None]] = None):
        self.result = result
        self.raises = raises
        self.on_call = on_call
        self.calls: List[Dict[str, str]] = []

    def geocode(self, address: str, *, city: str, state: str) -> Optional[GeocodeResult]:
        self.calls.append({"address": address, "city": city, "state": state})
        if self.on_call:
            self.on_call()
        if self.raises:
            raise self.raises
        return self.result


# Obviously fake but inside the boxes of the cities they name.
LEKKI = GeocodeResult(lat=6.4474, lng=3.4723, precision="rooftop")
GARKI = GeocodeResult(lat=9.0400, lng=7.4900, precision="rooftop")
LONDON = GeocodeResult(lat=51.5074, lng=-0.1278, precision="rooftop")
