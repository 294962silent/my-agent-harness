"""任务模板集合。

导入本包即完成模板注册（`from experiments.agent_rl import tasks`）。
新增模板只需在此处 import 一次。
"""

from experiments.agent_rl.tasks import t1_bugfix  # noqa: F401
from experiments.agent_rl.tasks import t2_feature  # noqa: F401
from experiments.agent_rl.tasks import t3_refactor  # noqa: F401
from experiments.agent_rl.tasks import t4_transform  # noqa: F401
from experiments.agent_rl.tasks import t5_cli  # noqa: F401
from experiments.agent_rl.tasks import t6_large  # noqa: F401
from experiments.agent_rl.tasks import t7_multi_bug  # noqa: F401

# t7 用运行时注册（避免与 @register 装饰器的导入顺序耦合）
t7_multi_bug.register_template()

__all__ = [
    "t1_bugfix",
    "t2_feature",
    "t3_refactor",
    "t4_transform",
    "t5_cli",
    "t6_large",
    "t7_multi_bug",
]
