from fastapi import APIRouter,Depends
from sqlalchemy.orm import Session
from ..config.database import get_db
from ..controllers.dependencies import roles
from ..constants.roles import Role
from ..schemas.replacement import ReplacementPreflightInput,ReplacementConfirmInput
from ..services.survey_service import get_survey
from ..services.replacement_service import preflight,confirm,list_replacements
router=APIRouter(tags=["replacement"])

@router.post("/surveys/{sid}/replacement/preflight")
def run_preflight(sid:int,data:ReplacementPreflightInput,db:Session=Depends(get_db),user=Depends(roles(Role.ADMIN,Role.ANALYST))):
    return preflight(db,get_survey(db,sid),data.model_dump(),user)

@router.post("/surveys/{sid}/replacement/confirm")
def run_confirm(sid:int,data:ReplacementConfirmInput,db:Session=Depends(get_db),user=Depends(roles(Role.ADMIN,Role.ANALYST))):
    return confirm(db,get_survey(db,sid),data.token,user)

@router.get("/surveys/{sid}/replacement")
def index(sid:int,db:Session=Depends(get_db),user=Depends(roles(Role.ADMIN,Role.ANALYST))):
    return list_replacements(db,get_survey(db,sid))
