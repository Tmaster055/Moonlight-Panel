import os
import logging
from dotenv import load_dotenv
try:
    from cloudflare import Cloudflare
except Exception:  # pragma: no cover - optional dependency
    Cloudflare = None

load_dotenv()
log = logging.getLogger(__name__)


def _get_env(name, default=None):
    return os.environ.get(name) or default


def create_srv_record(server_name, target=os.getenv("TARGET"), port=25565, priority=0, weight=5, ttl=60):
    client = Cloudflare(
        api_token=os.getenv("CLOUDFLARE_API_TOKEN"),
    )
    record_response = client.dns.records.create(
        zone_id=os.getenv("CLOUDFLARE_ZONE_ID"),
        name=f"_minecraft._tcp.{server_name}",
        ttl=ttl,
        type="SRV",
        data={
            "port": port,
            "priority": priority,
            "target": target,
            "weight": weight
        }
    )
    print(record_response)


def delete_srv_record(server_name):
    client = Cloudflare(
        api_token=os.getenv("CLOUDFLARE_API_TOKEN"),
    )
    page = client.dns.records.list(
        zone_id=os.getenv("CLOUDFLARE_ZONE_ID"),
    )
    pages = page.result
    filtered_pages = [page for page in pages if page.name.startswith(f"_minecraft._tcp.{server_name}.")]
    ids = [page.id for page in filtered_pages]
    for id in ids:
        id = id

    client = Cloudflare(
        api_token=os.getenv("CLOUDFLARE_API_TOKEN"),
    )
    record = client.dns.records.delete(
        dns_record_id=id,
        zone_id=os.getenv("CLOUDFLARE_ZONE_ID"),
    )
    print(record.id)
