# Agent RL 沙盒基础镜像
# 目的：容器内能跑 pytest 与被测代码，并能运行 harness 内核（Python 侧）。
# 注意：python:3.11-slim 基于 Debian 13 (trixie)，源文件是 deb822 格式的 *.sources；
# 容器内无法直连 deb.debian.org，必须换国内镜像。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# 清空原有源（deb822 的 .sources 与旧的 .list 都要清）后改用清华镜像。
# 只保留 main 源：git / procps / curl 都在 main 里；security 源在 Debian 13 上路径易配错。
RUN rm -rf /etc/apt/sources.list.d /etc/apt/sources.list \
    && mkdir -p /etc/apt/sources.list.d \
    && printf 'Types: deb\nURIs: https://mirrors.tuna.tsinghua.edu.cn/debian\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.pgp\n' \
       > /etc/apt/sources.list.d/tuna.sources \
    && printf 'Acquire::Retries "5";\nAcquire::https::Timeout "60";\n' > /etc/apt/apt.conf.d/99retry

# git：看 diff / 提交；procps：timeout；curl：调试
RUN apt-get update \
    && apt-get install -y --no-install-recommends git procps curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 测试与工具依赖（任务判定只用这些，保持精简）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    pytest==8.3.4 \
    pytest-json-report==1.5.0

# harness 内核依赖（与仓库 pyproject 的 dependencies 对齐）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    "openai>=2.50.0" \
    "anthropic>=0.40.0" \
    "pydantic>=2.0" \
    "httpx>=0.27.0" \
    "python-dotenv>=1.0,<2.0" \
    "pyyaml>=6.0" \
    "mcp>=2.0.0,<2.1" \
    "filelock>=3.32.6"

RUN useradd -m -u 1000 agent
WORKDIR /workspace

# 冒烟检查：镜像构建后即可验证
RUN python -c "import pytest, pydantic, httpx; print('image ok')"
