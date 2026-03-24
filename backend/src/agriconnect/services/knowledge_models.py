from sqlalchemy import Column, String, JSON, Integer, DateTime
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func
import uuid

Base = declarative_base()

class CropKnowledge(Base):
    __tablename__ = "crop_knowledge"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug = Column(String, unique=True, nullable=False)
    crop_name = Column(String, nullable=False)
    variety = Column(String)
    zone_category = Column(String) # Nord, Centre, Sud
    technical_sheet = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def to_dict(self):
        return {
            "id": str(self.id),
            "slug": self.slug,
            "crop_name": self.crop_name,
            "variety": self.variety,
            "technical_sheet": self.technical_sheet
        }
