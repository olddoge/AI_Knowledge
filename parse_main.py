import json

from src.config import load_env
from src.parse_requester.mineru_parser import MineruParseWorker, build_mineru_parse_config


def main() -> None:
    """MinerU 解析模块独立入口。

    该入口不启动扫描、清洗或入库模块。启动后会持续领取 rag_files 中
    parse_status = 0 的任务，直到当前进程领不到可执行数据后自动退出。
    """
    config = build_mineru_parse_config(load_env())
    worker = MineruParseWorker(config)

    try:
        print(f"MinerU 解析模块启动，批量大小：{config.task_batch_size}")
        result = worker.run_until_idle()
        print(f"MinerU 解析模块结束：{json.dumps(result, ensure_ascii=False)}")
    except KeyboardInterrupt:
        print("MinerU 解析模块已手动终止。")
    except Exception as exc:
        print(f"MinerU 解析模块执行失败：{exc}")
        raise


if __name__ == "__main__":
    main()
