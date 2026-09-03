"""S3-compatible object store via boto3. Credentials come from the standard
AWS chain (env vars, profile, instance role). A single PUT is atomic on S3,
so no temp-name dance is needed."""

import logging
import posixpath

import boto3

from ..core.logging_setup import DEDUP_INTERVAL_S, DEDUP_KEY

_FAIL_LOG_INTERVAL_S = 600.0


class S3Storage:
    def __init__(self, *, bucket: str, prefix: str = "",
                 endpoint_url: str = "", region: str = "") -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint_url = endpoint_url or None
        self.region = region or None
        self.location = f"s3://{bucket}/{self.prefix}" if self.prefix else f"s3://{bucket}"
        self.logger = logging.getLogger("frameforge.storage.s3")

        self._client = None

    @property
    def alive(self) -> bool:
        return self._client is not None

    def ensure_open(self) -> bool:
        if self._client is not None:
            return True

        try:
            client = boto3.client(
                "s3", endpoint_url=self.endpoint_url, region_name=self.region)
            client.head_bucket(Bucket=self.bucket)
        except Exception as error:
            self.logger.error(
                "S3 bucket check failed bucket=%s err=%s", self.bucket, error,
                extra={DEDUP_KEY: "s3_open_fail",
                       DEDUP_INTERVAL_S: _FAIL_LOG_INTERVAL_S})
            return False

        self._client = client
        self.logger.info("S3 ready location=%s", self.location)
        return True

    def put(self, local_path: str, relative_path: str) -> None:
        key = relative_path.replace("\\", "/")
        if self.prefix:
            key = posixpath.join(self.prefix, key)
        self._client.upload_file(local_path, self.bucket, key)

    def mark_dead(self) -> None:
        self._client = None

    def close(self) -> None:
        self.mark_dead()
