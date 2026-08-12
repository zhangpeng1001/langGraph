# 07：Studio、依赖版本与迁移说明

## 1. 图注册

根目录 `langgraph.json` 使用标准模块路径注册图：

```json
{
  "python_version": "3.11",
  "dependencies": ["."],
  "graphs": {
    "basic": "./src/langgraph_demo/graphs/ex01_状态图基础.py:graph",
    "react": "./src/langgraph_demo/graphs/ex10_ReAct智能体.py:graph",
    "assistant": "./src/langgraph_demo/graphs/ex11_综合学习助手.py:graph"
  }
}
```

每个目标模块都导出已编译变量 `graph`。导入 ReAct 和 assistant 时不会立即读取 API
Key，只有执行模型节点才需要 `.env`。

## 2. 参考工程内部运行时

参考工程通过企业内部 `langgraph-api 1.7.x` 提供服务端和 Studio 能力。它不是本教程
默认运行所必需的公共依赖。

如果本机已配置有权限的企业包源，可以参考：

```powershell
poetry run pip install -r requirements-studio.txt
poetry run python studio_main.py
```

具体服务端参数、认证和 Studio 地址由所在环境决定。项目不会保存：

- 私有包源用户名或密码；
- 内部网关地址；
- API Key；
- 参考工程现存的任何认证配置。

没有内部运行时也不影响全部 CLI 示例和单元测试。

## 3. 为什么固定 LangGraph 0.3.34

用户指定依赖从 `normal_agent` 获取。实际检查结果中：

- `pyproject.toml` 只声明较宽的内部 API 版本；
- 旧 `requirements.txt` 的部分数值已落后；
- 当前 Poetry lock/环境使用 LangGraph `0.3.34`、checkpoint `2.1.0`、
  langchain-core `0.3.66`。

因此本项目用这些实际运行版本作为唯一基线，避免把两套 API 混在一起。

## 4. 与当前官方 1.x 文档的关系

当前 [LangGraph 官方概览](https://docs.langchain.com/oss/python/langgraph/overview)
主要面向更新的主线版本。核心思想仍一致：StateGraph、reducer、conditional edge、
Send、Command、checkpointer、Store、interrupt 和 streaming。

可能变化的部分包括：

- 高级 Agent 构建 API 的推荐入口；
- context/config schema 写法；
- 部分参数名称和弃用项；
- CLI、Studio 与部署包的安装方式；
- prebuilt Agent 的所属包和推荐替代方案。

复制官方新代码前，先执行：

```powershell
poetry run python -m langgraph_demo doctor
```

确认本项目仍是 0.3.34，再查看对应版本源码或迁移指南。

## 5. 迁移到 1.x 的建议顺序

1. 新建分支并更新核心依赖，不要原地同时改业务逻辑。
2. 先运行全部无网络单元测试，定位 Graph API 差异。
3. 处理构建和 schema 弃用警告。
4. 验证 checkpointer、Store、interrupt 恢复和 time travel。
5. 验证 ToolNode 消息格式以及真实模型 tool_calls。
6. 验证 Studio 配置和服务端接口。
7. 最后执行显式开启的真实 LLM 冒烟测试。

默认测试采用依赖注入，正是为了让升级时快速判断问题在图逻辑还是模型服务。

## 6. 导入自检

可以手动检查所有注册目标：

```powershell
poetry run python -c "from langgraph_demo.graphs.ex11_综合学习助手 import graph; print(graph.name)"
```

完整的 JSON 注册目标导入检查已包含在 `tests/test_project_contract.py`。

