def run_parse_task() -> dict[str, object]:
    """解析任务占位入口，后续会从数据库读取待解析文件并更新解析状态。"""
    return {
        "task": "parse",
        "status": "placeholder",
        "message": "解析任务占位，暂未实现具体逻辑。",
    }
