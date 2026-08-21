import baostock as bs
import pandas as pd
import os
import time

# ============ 配置 ============
START_DATE = "2021-01-01"
END_DATE = "2026-05-22"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


# ============ 1. 下载全量股票列表 ============
def download_stock_list():
    """下载全量A股股票列表"""
    print("正在下载全量股票列表...")

    bs.login()
    rs = bs.query_stock_basic(code_name="")

    data_list = []
    while rs.next():
        row = rs.get_row_data()
        # row: [code, code_name, ipoDate, outDate, type, status]
        if row[5] == "1":  # 只保留上市状态为"1"的股票
            data_list.append(
                {
                    "stock_code": row[0],  # sh.600519
                    "stock_name": row[1],  # 贵州茅台
                    "ipo_date": row[2],  # 上市日期
                    "out_date": row[3],  # 退市日期
                    "type": row[4],  # 股票类型
                }
            )

    bs.logout()

    df = pd.DataFrame(data_list)
    save_path = os.path.join(SCRIPT_DIR, "stock_list.csv")
    df.to_csv(save_path, index=False, encoding="utf-8")
    print(f"股票列表已保存至 {save_path}，共 {len(df)} 只")
    return df


# ============ 2. 下载行业分类 ============
def download_industry():
    """下载股票行业分类（申万一级）"""
    print("正在下载行业分类...")

    bs.login()

    # 先获取全量股票
    rs_stock = bs.query_stock_basic()
    stock_codes = []
    while rs_stock.next():
        row = rs_stock.get_row_data()
        if row[5] == "1":
            stock_codes.append(row[0])

    # 逐个查询行业分类
    industry_data = []
    total = len(stock_codes)

    for i, code in enumerate(stock_codes):
        if (i + 1) % 100 == 0:
            print(f"  进度: {i + 1}/{total}")

        try:
            rs = bs.query_stock_industry(code)
            while rs.next():
                row = rs.get_row_data()
                industry_data.append(
                    {
                        "stock_code": row[1],  # 股票代码
                        "stock_name": row[2],  # 股票名称
                        "industry": row[3],  # 所属行业（申万一级）
                    }
                )
        except:
            continue

        time.sleep(0.1)

    bs.logout()

    df = pd.DataFrame(industry_data)
    save_path = os.path.join(SCRIPT_DIR, "industry.csv")
    df.to_csv(save_path, index=False, encoding="utf-8")
    print(f"行业分类已保存至 {save_path}，共 {len(df)} 条记录")
    return df

# =========== 下载基本面数据（估值 PE/PB，日频） ============
def download_fundamental(start_date=START_DATE, end_date=END_DATE,
                         output="fundamental.csv", codes=None):
    """下载全量 A 股日频估值数据 (PE-TTM, PB)，保存到 fundamental.csv。

    字段: stock_code(去 sh./sz. 前缀), date(YYYY-MM-DD), pe, pb
    说明: baostock 的 PE/PB 内嵌在 query_history_k_data_plus 的估值字段 peTTM/pbMRQ 里
          （日频），并非独立的 query_indicator（该函数不存在）；与 hfq_clean.csv 同粒度，
          无需前滚、无未来函数；日期格式统一在 clean_fundamental() 完成。
    可选 codes: 传入代码列表可只下部分股票（用于小批量试跑），默认下全市场。
    """
    print("正在下载全量 A 股估值数据 (PE/PB)...")

    bs.login()

    # 取全量上市股票代码（status==1）；允许外部指定子集
    if codes is None:
        rs = bs.query_stock_basic(code_name="")
        codes = []
        while rs.next():
            row = rs.get_row_data()
            if row[5] == "1":
                codes.append(row[0])  # sh.600519 / sz.000001
    total = len(codes)
    print(f"共 {total} 只股票，开始下载估值（逐代码，耗时取决于网络）...")

    all_rows = []
    fail_count = 0

    for i, code in enumerate(codes):
        if (i + 1) % 200 == 0:
            print(f"  进度: {i + 1}/{total} | 失败: {fail_count}")

        try:
            # 注意：baostock 的 PE/PB 不是 query_indicator（该函数不存在），
            # 而是内嵌在 query_history_k_data_plus 的估值字段 peTTM/pbMRQ 里（日频）
            rs = bs.query_history_k_data_plus(
                code,
                "date,code,peTTM,pbMRQ",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3",  # 不复权（估值字段与复权方式无关）
            )
            if rs.error_code != "0":
                print(f"  [错误] {code} 估值查询失败: {rs.error_msg}")
                fail_count += 1
                continue

            rows = []
            while rs.next():
                rows.append(rs.get_row_data())

            if rows:
                df = pd.DataFrame(rows, columns=rs.fields)
                # 去掉 sh./sz. 前缀，对齐 clean 表格式
                df["stock_code"] = code.replace("sh.", "").replace("sz.", "")
                df = df.rename(columns={"peTTM": "pe", "pbMRQ": "pb"})
                df = df[["stock_code", "date", "pe", "pb"]]
                all_rows.append(df)
            else:
                # 查询成功但无数据（如长期停牌/未上市区间）
                fail_count += 1

        except Exception as e:
            print(f"  [异常] {code}: {e}")
            fail_count += 1  # 单只失败不阻断整体，最后汇总

        time.sleep(0.1)

    bs.logout()

    if all_rows:
        result = pd.concat(all_rows, ignore_index=True)
        for col in ["pe", "pb"]:
            result[col] = pd.to_numeric(result[col], errors="coerce")
        save_path = os.path.join(SCRIPT_DIR, output)
        result.to_csv(save_path, index=False, encoding="utf-8")
        print(
            f"估值数据已保存至 {save_path}，共 {len(result)} 条 | "
            f"{total - fail_count} 只成功 | {fail_count} 只失败"
        )
        return result
    else:
        print("未下载到任何估值数据")
        return None


# =========== 清洗基本面数据（对齐股票列表 + 统一日期格式） ============
def clean_fundamental(raw="fundamental.csv", output="fundamental_clean.csv"):
    """清洗估值数据，输出 fundamental_clean.csv（与 hfq_clean.csv 同口径）。

    - 对齐 stock_list_clean.csv 的股票列表（只保留已清洗股票池，与既有清洗逻辑一致）
    - 日期统一为 TRADINGDAY 列、格式 YYYY/M/D（与 hfq_clean.csv 完全一致，便于按
      stock_code + TRADINGDAY 合并）
    - pe/pb 数值化（亏损股 pe 为空/负，保留原值交由回测层判定）
    """
    raw_path = os.path.join(SCRIPT_DIR, raw)
    if not os.path.exists(raw_path):
        print(f"未找到 {raw_path}，请先运行 download_fundamental()")
        return None

    fund = pd.read_csv(raw_path, dtype={"stock_code": str})

    # 对齐已有 clean 股票列表（取白名单，而非交集——fundamental 本就按全市场下载）
    clean_list_path = os.path.join(SCRIPT_DIR, "stock_list_clean.csv")
    if os.path.exists(clean_list_path):
        valid_codes = set(
            pd.read_csv(clean_list_path, dtype={"stock_code": str})["stock_code"]
        )
        before = len(fund)
        fund = fund[fund["stock_code"].isin(valid_codes)]
        print(f"对齐股票列表: {before} -> {len(fund)} 条（白名单 {len(valid_codes)} 只）")
    else:
        print("未找到 stock_list_clean.csv，跳过对齐（请先跑完整清洗流程）")

    # 日期统一为 YYYY/M/D，列名 TRADINGDAY（与 hfq 完全一致，无前导零）
    parsed = pd.to_datetime(fund["date"], errors="coerce")
    fund["TRADINGDAY"] = parsed.apply(
        lambda d: f"{d.year}/{d.month}/{d.day}" if pd.notna(d) else None
    )
    fund = fund.dropna(subset=["TRADINGDAY"])

    fund = fund[["stock_code", "TRADINGDAY", "pe", "pb"]]
    for col in ["pe", "pb"]:
        fund[col] = pd.to_numeric(fund[col], errors="coerce")

    save_path = os.path.join(SCRIPT_DIR, output)
    fund.to_csv(save_path, index=False, encoding="utf-8")
    print(
        f"估值清洗完成: {save_path}，共 {len(fund)} 条 | "
        f"覆盖 {fund['stock_code'].nunique()} 只"
    )
    return fund


# =========== 下载指数行情（真实基准，baostock） ===========
# 精选宽基 + 板块指数清单。
# 数据源：baostock（与 download_fundamental 同源，已在文件顶部 import，网络可达）。
#   baostock 支持 sh./sz. 前缀的指数代码：沪深300(sh.000300)、中证全指(sh.000985)、
#   创业板指(sz.399006)、上证指数(sh.000001) 以及 399xxx 系列中证/国证行业指数
#   （如 中证白酒 sz.399997）。对部分细分主题指数（H 开头的中证细分如 H30590 中证机器人、
#   930/931 开头的中证主题如 930995 半导体）baostock 通常无覆盖——保留尝试，
#   拉取为空/异常时记入 missing/failed 并末尾打印，对应板块在回测层自动回退为
#   行业内等权篮子（见 skills/E_backtest.md），不阻断整体。
# 落盘到 index_clean.csv 的 index_code 统一为「去前缀裸代码」（000300 / 399997 /
#   H30590 …），与 E_backtest.md 的 load_benchmark 匹配口径一致。
INDEX_LIST = {
    # —— 宽基（所有想法通用市场基准，baostock 支持）——
    "sh.000300": "沪深300",
    "sh.000985": "中证全指",
    "sz.399006": "创业板指",
    "sh.000001": "上证指数",
    "sh.000905": "中证500",
    "sz.399001": "深证成指",
    # —— 板块（399xxx 系列中证/国证行业指数，baostock 支持）——
    "sz.399997": "中证白酒",
    "sz.399808": "中证新能源",
    "sz.399932": "中证主要消费",
    "sz.399989": "中证医疗",
    "sz.399967": "中证军工",
    "sz.399986": "中证银行",
    "sz.399998": "中证煤炭",
    "sz.399971": "中证传媒",
    "sz.399975": "中证证券公司",
    # —— 细分主题（H/930/931 系列，baostock 多数无覆盖 → 尝试后回退行业等权篮子）——
    "sh.H30590":    "中证机器人",
    "sh.930995": "中证半导体",
    "sh.930652": "中证电子",
    "sh.930651": "中证计算机",
    "sh.930713": "中证人工智能",
    "sz.931008": "中证汽车",
}


def download_index(start_date=START_DATE, end_date=END_DATE,
                   output="index_clean.csv", codes=None):
    """下载真实指数日线行情，落盘 data/index_clean.csv，作为回测基准序列。

    数据源：baostock（query_history_k_data_plus，与 download_fundamental 同源）。
    列：index_code, index_name, TRADINGDAY, close
        - TRADINGDAY 格式 YYYY/M/D（与 hfq_clean.csv 完全一致，便于按日合并）
        - close 为指数收盘价（指数本身即价格序列，无需复权）
        - index_code 为去前缀裸代码（000300 / 399997 / H30590…），与 E 匹配口径一致
    兜底：某 code 拉取为空/异常，记入 missing/failed 并末尾打印，不阻断整体；
         对应板块回测层自动回退行业内等权篮子（见 skills/E_backtest.md）。
    """
    bs.login()
    target = codes if codes is not None else list(INDEX_LIST.keys())
    print(f"正在用 baostock 下载 {len(target)} 个指数日线（{start_date} ~ {end_date}）...")

    frames, missing, failed = [], [], []
    for full_code in target:
        name = INDEX_LIST[full_code]
        bare = full_code.replace("sh.", "").replace("sz.", "")  # 去前缀存裸代码
        try:
            rs = bs.query_history_k_data_plus(
                full_code, "date,close",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="3",
            )
            if rs.error_code != "0":
                print(f"  [空] {full_code} {name}：{rs.error_msg}（回测层将回退行业等权篮子）")
                missing.append(full_code)
                continue
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                print(f"  [空] {full_code} {name}：无数据（回退行业等权篮子）")
                missing.append(full_code)
                continue
            sub = pd.DataFrame(rows, columns=rs.fields)
            sub = sub.rename(columns={"date": "TRADINGDAY"})
            sub["TRADINGDAY"] = pd.to_datetime(sub["TRADINGDAY"], errors="coerce").apply(
                lambda d: f"{d.year}/{d.month}/{d.day}" if pd.notna(d) else None
            )
            sub = sub.dropna(subset=["TRADINGDAY"])
            sub["close"] = pd.to_numeric(sub["close"], errors="coerce")
            sub["index_code"] = bare
            sub["index_name"] = name
            sub = sub[["index_code", "index_name", "TRADINGDAY", "close"]]
            frames.append(sub)
            print(f"  [OK] {full_code} {name}：{len(sub)} 行")
        except Exception as e:
            print(f"  [异常] {full_code} {name}：{e}")
            failed.append(full_code)
            continue
        time.sleep(0.1)

    bs.logout()

    if not frames:
        print("未下载到任何指数数据")
        return None

    result = pd.concat(frames, ignore_index=True)
    result = result.sort_values(["index_code", "TRADINGDAY"]).reset_index(drop=True)
    save_path = os.path.join(SCRIPT_DIR, output)
    result.to_csv(save_path, index=False, encoding="utf-8")
    print(f"指数行情已保存至 {save_path}，共 {len(result)} 行 | "
          f"覆盖 {result['index_code'].nunique()} 个指数")
    if missing:
        print(f"[提示] 以下 code 无数据（已跳过，回测层回退行业等权篮子）：{missing}")
    if failed:
        print(f"[提示] 以下 code 拉取异常（已跳过）：{failed}")
    return result


# ============ 执行入口 ============
if __name__ == "__main__":
    print("=== 基本面估值数据：下载 + 清洗 ===\n")

    # 1) 全量 A 股 PE/PB 日频下载（baostock 逐代码；全市场数千只，耗时较长）
    # 试跑 3 只，避免一上来全市场几小时
    download_fundamental(codes=["sh.600519", "sz.000001", "sh.688289"])

    # 2) 清洗：对齐 stock_list_clean 股票列表 + 日期统一为 TRADINGDAY(YYYY/M/D)
    clean_fundamental()

    # 3) 指数行情（真实基准，baostock；与 download_fundamental 同源，无需额外安装）
    #    下载后落盘 data/index_clean.csv，回测层据此取真实指数基准，缺失则回退行业等权篮子。
    download_index()

    """
    # 3) 后复权价格表 pct_chg 重算（既有逻辑，保留）
    clean = pd.read_csv(os.path.join(SCRIPT_DIR, "hfq_clean.csv"), dtype={"stock_code": str})
    clean["pct_chg"] = (
        (clean["ADJCLOSEPRICE"] - clean["ADJPREVCLOSE"]) / clean["ADJPREVCLOSE"] * 100
    )
    clean.to_csv(
        os.path.join(SCRIPT_DIR, "hfq_clean.csv"), index=False, encoding="utf-8"
    )
    """

    print("\n=== 数据处理完成 ===")
