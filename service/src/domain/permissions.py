"""RBAC 权限码的唯一定义处。

权限码就是 ``menus`` 表里 ``menu_type == 2`` 的节点名（见
:mod:`src.domain.models.rbac`）：角色被授予菜单即被授予权限，管理员走
:data:`ALL_PERMISSION_CODE` 通配。

三处消费方必须共用这一份常量：

* 路由在 :func:`src.api.dependencies.require_permission` 上声明所需权限；
* :mod:`src.application.services.rbac_seed` 用它创建权限节点与内置角色的初始授权；
* 前端按钮权限（``meta.auths``）读取的是同一批字符串。

集中定义的原因很实际：权限码写错不会有任何编译期或启动期报错，只会让某个接口
对所有人（或对管理员以外的所有人）返回 403，排查成本远高于一个常量的代价。
"""

ALL_PERMISSION_CODE = "*:*:*"


class Permissions:
    """所有业务权限码；命名与 ``resource:action`` 的字符串一一对应。"""

    JOBS_READ = "jobs:read"
    JOBS_WRITE = "jobs:write"
    JOBS_OPERATE = "jobs:operate"

    RULESETS_READ = "rulesets:read"
    RULESETS_WRITE = "rulesets:write"

    DATASOURCES_READ = "datasources:read"
    DATASOURCES_WRITE = "datasources:write"

    MATCHES_READ = "matches:read"

    WORKERS_READ = "workers:read"
    WORKERS_OPERATE = "workers:operate"

    SYSTEM_READ = "system:read"
    SYSTEM_OPERATE = "system:operate"

    USERS_READ = "users:read"
    USERS_WRITE = "users:write"

    ROLES_READ = "roles:read"
    ROLES_WRITE = "roles:write"

    MENUS_READ = "menus:read"
    MENUS_WRITE = "menus:write"


def all_permission_codes() -> tuple[str, ...]:
    """除通配码以外的全部权限码；种子与测试可以按它做全量覆盖检查。"""
    return tuple(
        value
        for name, value in vars(Permissions).items()
        if not name.startswith("_") and isinstance(value, str)
    )
