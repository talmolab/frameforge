"""Source registry. Backends import lazily so the package loads without their SDKs."""

SOURCE_KINDS = ("pylon",)


def make_source(camera_config, full_config, **options):
    kind = camera_config.kind
    if kind == "pylon":
        from .pylon import PylonSource
        return PylonSource(camera_config, full_config, **options)
    raise ValueError(f"unknown source kind {kind!r}; choose from {SOURCE_KINDS}")
