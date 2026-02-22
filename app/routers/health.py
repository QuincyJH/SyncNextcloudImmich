from fastapi import APIRouter
import os
import shutil

router = APIRouter()


@router.get("/", response_model=str)
def healthcheck() -> str:
	return "ok"


@router.get("/dependencies")
def dependency_check():
	immich_go_bin = os.environ.get("IMMICH_GO_BIN", "immich-go")
	resolved_path = shutil.which(immich_go_bin)
	return {
		"immich_go": {
			"configured": immich_go_bin,
			"available": resolved_path is not None,
			"resolved_path": resolved_path,
		}
	}
