from pydantic import BaseModel, Field

from oms_hub.llm.domain import ProviderName


class CredentialUpdate(BaseModel):
    credential: str = Field(default="", max_length=8192)


class ModelUpdate(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class ActiveProviderUpdate(BaseModel):
    provider: ProviderName

