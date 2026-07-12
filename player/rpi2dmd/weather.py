"""OpenWeather fetcher (the only optional network dependency).

A background thread refreshes the current conditions every
weather.refresh_min minutes, failing silently when offline. The last good
result is cached at <run_dir>/weather.json so a reboot can show weather
before the first fetch completes.

Python 3.7 compatible; stdlib only.
"""

import json
import os
import threading
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from . import paths

API_URL = "https://api.openweathermap.org/data/2.5/weather"
FETCH_TIMEOUT_S = 10
CACHE_MAX_AGE_S = 24 * 3600
STALE_MAX_AGE_S = 3 * 3600   # don't display readings older than this


def fetch(cfg):
    """One synchronous fetch -> data dict or None (never raises)."""
    key = cfg.get("weather.api_key", "")
    zip_code = cfg.get("weather.zip_code", "")
    if not key or not zip_code:
        return None
    params = urlencode({
        "zip": "%s,%s" % (zip_code, cfg.get("weather.country", "US")),
        "appid": key,
        "units": cfg.get("weather.units", "imperial"),
    })
    try:
        resp = urlopen(API_URL + "?" + params, timeout=FETCH_TIMEOUT_S)
        try:
            doc = json.loads(resp.read().decode("utf-8"))
        finally:
            resp.close()
        return {
            "temp": doc["main"]["temp"],
            "condition": doc["weather"][0]["main"],
            "city": doc.get("name", ""),
            "units": cfg.get("weather.units", "imperial"),
            "fetched_at": time.time(),
        }
    except Exception:
        return None


def _cache_path():
    return os.path.join(paths.run_dir(), "weather.json")


class WeatherService(object):
    """Background refresher; data() returns the latest result or None."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._data = self._load_cache()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run,
                                            name="weather", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def data(self):
        """Latest result, or None once it is too stale to show (network
        loss must not leave hours-old readings on the panel)."""
        doc = self._data
        if doc is None:
            return None
        try:
            age = time.time() - float(doc.get("fetched_at", 0))
        except (TypeError, ValueError):
            return None
        return doc if age <= STALE_MAX_AGE_S else None

    # -- internals ---------------------------------------------------------
    def _load_cache(self):
        try:
            with open(_cache_path(), "r", encoding="utf-8") as f:
                doc = json.load(f)
            if time.time() - float(doc.get("fetched_at", 0)) < CACHE_MAX_AGE_S:
                return doc
        except (OSError, ValueError, TypeError):
            pass
        return None

    def _save_cache(self, doc):
        path = _cache_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def _run(self):
        while not self._stop.is_set():
            wait_s = 30
            if self.cfg.get("weather.enabled", False):
                doc = fetch(self.cfg)
                if doc is not None:
                    self._data = doc
                    self._save_cache(doc)
                try:
                    refresh = float(self.cfg.get("weather.refresh_min", 60))
                except (TypeError, ValueError):
                    refresh = 60
                wait_s = max(60, refresh * 60)
                if doc is None:
                    wait_s = min(wait_s, 300)  # retry sooner after a failure
            self._stop.wait(wait_s)
