import os
import secrets

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status

from app.services import config_service

router = APIRouter()


def require_config_token(x_config_token: str | None = Header(default=None)):
    """
    Gate every config endpoint behind a shared secret supplied in the
    `X-Config-Token` header. The token is read from CONFIG_API_TOKEN.

    If CONFIG_API_TOKEN is unset the API is disabled (503) rather than left
    open — these endpoints can read/write Immich credentials, so failing closed
    is the safe default.
    """
    expected = os.environ.get("CONFIG_API_TOKEN")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Config API is disabled. Set CONFIG_API_TOKEN to enable it.",
        )
    if not x_config_token or not secrets.compare_digest(x_config_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Config-Token header.",
        )


@router.get("/mapping", dependencies=[Depends(require_config_token)])
def get_mapping():
    return config_service.read_mapping()


@router.put("/mapping", dependencies=[Depends(require_config_token)])
def put_mapping(data: dict = Body(...)):
    try:
        config_service.write_mapping(data)
    except config_service.ConfigValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return {"status": "saved", "file": "mapping.json"}


@router.get("/user-config", dependencies=[Depends(require_config_token)])
def get_user_config():
    return config_service.read_user_config()


@router.put("/user-config", dependencies=[Depends(require_config_token)])
def put_user_config(data: list = Body(...)):
    try:
        config_service.write_user_config(data)
    except config_service.ConfigValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    return {"status": "saved", "file": "user_config.json"}
