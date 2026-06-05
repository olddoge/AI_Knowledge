import json

from src.config import load_env
from src.lightrag_ingest.markdown_upload_task import (
    build_markdown_upload_task_config,
    run_markdown_upload_task,
)


def main() -> None:
    """独立测试入口：上传 MARKDOWN_OUTPUT_PATH 下的 Markdown 原文到 LightRAG。

    该入口只读取 rag_files 中已经解析完成的 parse_path，不清洗文本、不删除文件、
    不更新数据库状态，便于验证 LightRAG 上传接口和 Markdown 原文入库效果。
    """
    config = build_markdown_upload_task_config(load_env())

    try:
        result = run_markdown_upload_task(config)
    except KeyboardInterrupt:
        print("Markdown 上传入口已手动终止；该入口不会修改数据库状态")
        return
    except Exception as exc:
        print(f"Markdown 上传入口执行失败：{exc}")
        raise

    print(f"Markdown 上传入口结束：{json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
