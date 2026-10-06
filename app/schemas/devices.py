from typing import Literal

from pydantic import BaseModel, Field

# Expo's own format: ExponentPushToken[...] (current) or ExpoPushToken[...] (older SDKs).
EXPO_TOKEN_PATTERN = r"^Expo(nent)?PushToken\[[^\[\]\s]{1,200}\]$"


class PushTokenRegister(BaseModel):
    token: str = Field(pattern=EXPO_TOKEN_PATTERN, max_length=255)
    platform: Literal["ios", "android"]


class PushTokenDelete(BaseModel):
    token: str = Field(min_length=1, max_length=255)


class PushTokenResponse(BaseModel):
    token: str
    platform: str
