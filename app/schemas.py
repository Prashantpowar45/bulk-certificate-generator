"""Validate the envelope first, then validate each recipient independently."""
from datetime import date
from typing import Annotated, Any
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

# The predefined template is scoped to printable ASCII in this assignment.
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120, pattern=r'^[ -~]+$')]


class CertificateInfo(BaseModel):
    model_config = ConfigDict(extra='forbid')
    course_name: Text
    organization: Text
    issue_date: date


class JobCreate(CertificateInfo):
    recipients: list[Any] = Field(min_length=1, max_length=1000)


class Recipient(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: Text
    email: EmailStr

    @field_validator('email')
    @classmethod
    def email_length(cls, value):
        if len(value) > 254:
            raise ValueError('Email must be at most 254 characters')
        return value
