"""参考工程内部 langgraph-api 运行时的可选启动入口。

默认学习流程不依赖这个文件。只有本机已经从有权限的企业包源安装
``langgraph-api 1.7.x`` 时，才使用 ``python studio_main.py``。
"""

try:
    from langgraph_api.cli import main
except ImportError as exc:  # pragma: no cover - 取决于企业内部可选依赖
    raise SystemExit(
        "未安装可选的 langgraph-api 1.7.x。请阅读 docs/07_Studio与版本.md。"
    ) from exc


if __name__ == "__main__":
    main()

