import json

from src.config import load_env
from src.data_cleaner.clean_task import build_clean_task_config, run_clean_task_processes


def main() -> None:
    """清洗模块独立入口。

    该入口只启动清洗流程，不启动扫描、解析或其他模块。启动后会创建配置数量的
    清洗进程；每个进程每批从 rag_files 原子领取 CLEAN_TASK_BATCH_SIZE 条
    clean_status=0 的记录，全部处理到空闲后自动退出。
    """
    config = build_clean_task_config(load_env())

    try:
        result = run_clean_task_processes(config)
    except KeyboardInterrupt:
        print("清洗模块已手动终止，已领取但未完成的任务会在超过过期时间后重试")
        return
    except Exception as exc:
        print(f"清洗模块执行失败：{exc}")
        raise

    print(f"清洗模块结束：{json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
