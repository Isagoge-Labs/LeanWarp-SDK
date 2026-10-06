from __future__ import annotations

from copy import deepcopy

import httpx
import pytest
from leanwarp_cloud import LeanWarpCloud, LeanWarpCloudError
from leanwarp_cloud.resources import published_worker_resources


def test_published_default_hides_retained_catalog_and_maximum_prices():
    catalog = {
        "default_resource_profile": "elastic",
        "idle_seconds": 600,
        "resources": [
            {"name": "standard", "minute_microusd": 33161},
            {
                "name": "elastic",
                "billing": "elastic_consumption_v1",
                "cpu_request_cores": 2,
                "cpu_limit_cores": 8,
                "memory_request_mib": 16384,
                "memory_limit_mib": 65536,
                "customer_cpu_cores": "2",
                "customer_memory_mib": 16384,
                "minimum_minute_microusd": 16700,
                "maximum_minute_microusd": 89069,
                "elastic_pricing_policy": {"policy_id": "retained-operator-detail"},
                "hold_microusd": 222672,
            },
        ],
    }
    original = deepcopy(catalog)
    assert published_worker_resources(catalog) == {
        "idle_seconds": 600,
        "worker": {
            "cpu_request_cores": 2,
            "cpu_limit_cores": 8,
            "memory_request_mib": 16384,
            "memory_limit_mib": 65536,
            "customer_cpu_cores": "2",
            "customer_memory_mib": 16384,
            "hold_microusd": 222672,
            "usage_based": True,
            "starting_minute_microusd": 16700,
        },
    }
    with LeanWarpCloud(
        "secret",
        base_url="https://api.example",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=catalog)),
    ) as cloud:
        assert cloud.resources() == original
    assert catalog == original


def test_older_api_only_falls_back_to_standard_and_does_not_invent_a_quote():
    assert published_worker_resources(
        {"resources": [{"name": "large"}, {"name": "standard", "minute_microusd": 33161}]}
    ) == {"worker": {"usage_based": False, "starting_minute_microusd": 33161}}
    assert published_worker_resources({"resources": [{"name": "standard"}]}) == {
        "worker": {"usage_based": False}
    }


@pytest.mark.parametrize(
    "catalog",
    [
        {},
        {"resources": [{"name": "large"}]},
        {"default_resource_profile": "missing", "resources": [{"name": "standard"}]},
        {"default_resource_profile": None, "resources": [{"name": "standard"}]},
        {"resources": [{"name": "standard"}, {"name": "standard"}]},
    ],
)
def test_unpublished_or_ambiguous_defaults_fail_clearly(catalog):
    with pytest.raises(LeanWarpCloudError, match="one published worker") as raised:
        published_worker_resources(catalog)
    assert raised.value.code == "worker_pricing_unavailable"
