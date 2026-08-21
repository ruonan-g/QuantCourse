
import logging
import time
from datetime import datetime
import os

# ================ 日志配置 ====================
def setup_logger(log_dir="pipeline_outputs", log_file="pipeline.log"):
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, log_file)

    logger = logging.getLogger("FactorPipeline")
    logger.setLevel(logging.INFO)

    # 格式：时间 | 级别 | 消息
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 输出到文件，日志文件和每步的输出结果都在同一个根目录下，便于整体归档
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 同时输出到控制台（PyCharm 里能直接看到）
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    logger.propagate = False

    return logger
