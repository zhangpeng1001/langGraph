"""本地开发服务器入口，不需要 Docker、数据库或外部平台。"""

from __future__ import annotations

import uvicorn


def main() -> None:
    """启动 FastAPI；reload 关闭以保证 MemorySaver 检查点不因重载丢失。"""

    uvicorn.run("data_platform.api:app", host="127.0.0.1", port=8003, reload=False)


if __name__ == "__main__":
    main()
