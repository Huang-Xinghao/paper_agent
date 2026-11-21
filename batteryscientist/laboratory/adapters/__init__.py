from .bts import BtsAdapter
from .land import LandAdapter


def default_adapters():
    return {adapter.source_id: adapter for adapter in (BtsAdapter(), LandAdapter())}
