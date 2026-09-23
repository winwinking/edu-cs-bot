"""JWT 签发与校验。HS256，密钥来自环境变量，claims 固定为 sub/tenant_id/role/exp。"""
import time
from typing import Optional

import jwt
from pydantic import BaseModel

from app.common.config import get_settings

settings = get_settings()


class TokenPayload(BaseModel):
    sub: str
    tenant_id: str
    role: str
    exp: int


class TokenError(Exception):
    """JWT 校验失败统一抛这个（过期、签名不对、格式错误等），上层只用捕获一种异常"""


def create_access_token(
    *,
    user_id: str,
    tenant_id: str,
    role: str,
    expire_minutes: Optional[int] = None,
) -> str:
    minutes = settings.jwt_expire_minutes if expire_minutes is None else expire_minutes
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "role": role,
        "exp": int(time.time()) + minutes * 60,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> TokenPayload:
    try:
        raw = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    return TokenPayload(**raw)
