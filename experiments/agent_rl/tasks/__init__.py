"""任务模板集合。

导入本包即完成模板注册（`from experiments.agent_rl import tasks`）。
新增模板只需在此处 import 一次。
"""

from experiments.agent_rl.tasks import t1_bugfix  # noqa: F401
from experiments.agent_rl.tasks import t2_feature  # noqa: F401
from experiments.agent_rl.tasks import t5_cli  # noqa: F401
from experiments.agent_rl.tasks import t6_large  # noqa: F401
from experiments.agent_rl.tasks import t4_transform  # noqa: F401
from experiments.agent_rl.tasks import t3_refactor  # noqa: F401

__all__ = ["t1_bugfix", "t2_feature", "t3_refactor", "t4_transform", "t5_cli", "t6_large"]
