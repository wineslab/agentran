import aiohttp
import csv
import io
from typing import Optional


class InfluxClient:
    """Async InfluxDB read client using Flux queries."""

    def __init__(self, url: str, org: str, bucket: str, token: str, measurement: str = 'dl_scheduler'):
        self.url = url
        self.org = org
        self.bucket = bucket
        self.token = token
        self.measurement = measurement
        self._session: Optional[aiohttp.ClientSession] = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Token {self.token}",
                    "Content-Type": "application/vnd.flux",
                    "Accept": "application/csv",
                }
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def query(self, flux: str) -> list[dict]:
        """Run a Flux query and return parsed CSV rows as list of dicts."""
        await self._ensure_session()
        query_url = f"{self.url}/api/v2/query?org={self.org}"
        try:
            async with self._session.post(query_url, data=flux) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[InfluxClient] Query failed ({resp.status}): {body[:200]}")
                    return []
                text = await resp.text()
                # InfluxDB returns multiple CSV tables separated by blank lines
                # Parse all non-empty tables
                rows = []
                for line in csv.DictReader(io.StringIO(text)):
                    if line.get("_value") is not None or line.get("_field") is not None:
                        rows.append(dict(line))
                return rows
        except Exception as e:
            print(f"[InfluxClient] Query error: {e}")
            return []

    async def write(self, line_protocol: str) -> bool:
        """Write line protocol data to InfluxDB."""
        await self._ensure_session()
        write_url = f"{self.url}/api/v2/write?org={self.org}&bucket={self.bucket}&precision=ms"
        try:
            async with self._session.post(
                write_url, data=line_protocol,
                headers={"Content-Type": "text/plain"}
            ) as resp:
                if resp.status != 204:
                    body = await resp.text()
                    print(f"[InfluxClient] Write failed ({resp.status}): {body[:200]}")
                    return False
                return True
        except Exception as e:
            print(f"[InfluxClient] Write error: {e}")
            return False

    async def health_check(self) -> bool:
        """Test connectivity to InfluxDB."""
        await self._ensure_session()
        try:
            async with self._session.get(f"{self.url}/health") as resp:
                return resp.status == 200
        except Exception as e:
            print(f"[InfluxClient] Health check failed: {e}")
            return False
