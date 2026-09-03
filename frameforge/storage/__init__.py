"""Storage registry. Backends import lazily so the package loads without their SDKs."""

STORAGE_KINDS = ("smb", "s3")


def make_storage(storage_config):
    kind = storage_config.kind
    if kind == "smb":
        from .smb import SmbStorage
        return SmbStorage(server=storage_config.server,
                          share=storage_config.share,
                          root=storage_config.root)
    if kind == "s3":
        from .s3 import S3Storage
        return S3Storage(bucket=storage_config.bucket,
                         prefix=storage_config.prefix,
                         endpoint_url=storage_config.endpoint_url,
                         region=storage_config.region)
    raise ValueError(f"unknown storage kind {kind!r}; choose from {STORAGE_KINDS}")
