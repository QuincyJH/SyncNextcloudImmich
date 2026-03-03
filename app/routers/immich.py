from app.services import immich_service
import logging
from fastapi import APIRouter

router = APIRouter()

@router.post("/", response_model=str)
def convert_album_to_tag():
    immich_service.convert_album_to_tag(dry_run=False)
    return "album-to-tag started"


@router.post("/clear", response_model=str)
def clear_all_tags(dry_run: bool = False):
    immich_service.clear_all_tags(dry_run=dry_run)
    return "clear-all-tags started"