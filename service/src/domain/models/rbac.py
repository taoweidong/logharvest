from datetime import datetime

from sqlmodel import Field, SQLModel

from src.domain.models.entities import utcnow


class Menu(SQLModel, table=True):
    """菜单即权限：目录/页面/权限节点共用一张表。

    ``name`` 对目录和页面是前端路由名，对权限节点是权限码；全局唯一，便于授权与
    权限解析都按名字而不是不稳定的自增 id 交流。
    """

    __tablename__ = "menus"

    id: int | None = Field(default=None, primary_key=True)
    parent_id: int | None = Field(default=None, foreign_key="menus.id", index=True)
    name: str = Field(index=True, unique=True)
    menu_type: int = 0  # 0 目录 1 页面 2 权限
    path: str = ""
    component: str = ""
    title: str = ""
    icon: str = ""
    rank: int = 0
    show_link: bool = True
    keep_alive: bool = False
    is_active: bool = True
    is_system: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class RoleMenu(SQLModel, table=True):
    __tablename__ = "role_menus"

    role_id: int = Field(foreign_key="roles.id", primary_key=True)
    menu_id: int = Field(foreign_key="menus.id", primary_key=True)
