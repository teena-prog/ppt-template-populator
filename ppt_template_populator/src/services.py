from __future__ import annotations

from dataclasses import dataclass

from config import Settings
from .elastic_client import create_client as create_elastic_client
from .minio_client import create_client as create_minio_client
from .template_retriever import TemplateRetriever
from .watsonx_client import WatsonxClient


@dataclass
class ApplicationServices:
    settings: Settings
    elastic: object
    minio: object
    retriever: TemplateRetriever

    def watsonx(self) -> WatsonxClient:
        missing = self.settings.watsonx_missing()
        if missing:
            raise ValueError("Missing required configuration: " + ", ".join(missing))
        return WatsonxClient(
            api_key=self.settings.watsonx_api_key.get_secret_value(),
            url=self.settings.watsonx_url,
            project_id=self.settings.watsonx_project_id,
            timeout=self.settings.request_timeout_seconds,
        )


def build_application_services(settings: Settings) -> ApplicationServices:
    elastic_password = (
        settings.elasticsearch_password.get_secret_value()
        if settings.elasticsearch_password else None
    )
    elastic = create_elastic_client(
        settings.elasticsearch_url,
        settings.elasticsearch_username,
        elastic_password,
        settings.request_timeout_seconds,
    )
    minio_secret = (
        settings.minio_secret_key.get_secret_value()
        if settings.minio_secret_key else None
    )
    minio = create_minio_client(
        settings.minio_endpoint,
        settings.minio_access_key,
        minio_secret,
        settings.minio_secure,
    )
    return ApplicationServices(
        settings=settings,
        elastic=elastic,
        minio=minio,
        retriever=TemplateRetriever(elastic, minio),
    )
