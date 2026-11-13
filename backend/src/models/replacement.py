from sqlalchemy import JSON,ForeignKey,String,Text
from sqlalchemy.orm import Mapped,mapped_column
from ..config.database import Base
from ..utils.clock import now
class SurveyReplacement(Base):
    __tablename__="survey_replacements"
    id:Mapped[int]=mapped_column(primary_key=True)
    survey_id:Mapped[int]=mapped_column(ForeignKey("spatial_surveys.id"),index=True)
    token:Mapped[str]=mapped_column(String(64),unique=True,index=True)
    batch_hash:Mapped[str]
    source_state_hash:Mapped[str]
    payload:Mapped[dict]=mapped_column(JSON)
    cell_count:Mapped[int]
    edge_count:Mapped[int]
    preserved_notes:Mapped[int]=mapped_column(default=0)
    status:Mapped[str]=mapped_column(default="preflight",index=True)
    submitted_by:Mapped[str]
    created_at:Mapped[str]=mapped_column(default=now)
    confirmed_by:Mapped[str]=mapped_column(default="")
    confirmed_at:Mapped[str]=mapped_column(default="")
    note:Mapped[str]=mapped_column(Text,default="")
