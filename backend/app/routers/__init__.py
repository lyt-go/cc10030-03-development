"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import region_density, resource

# 固定路径路由必须先注册，避免 /api/{module} 动态路径抢先匹配到业务动作。
ROUTERS = [region_density, resource]
