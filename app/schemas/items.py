"""
app/schemas/items.py
"""

import uuid
from datetime import datetime

from pydantic import BaseModel

class ItemResponse(BaseModel):
    id:           uuid.UUID
    product_name: str
    itemcd:       str | None
    item_cls_cd:  str | None
    tax_ty_cd:    str | None
    uom:          str | None
    pkg_cd:       str | None

    model_config = {"from_attributes": True}