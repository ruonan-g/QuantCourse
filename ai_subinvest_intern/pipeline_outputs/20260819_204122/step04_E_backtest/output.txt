"""
策略名称：半导体设备四股趋势突破策略（60日均线突破买入/跌破卖出，季度调仓）
回测说明：对北方华创、中微公司、拓荆科技、华海清科四只半导体设备个股的等权组合，以组合净值收盘价上穿60日均线买入、下穿卖出；季度调仓为权重再平衡，信号每日评估。保守版采用单票仓位上限7.5%（组合总仓位30%）、4批建仓、-10%止损；原策略分支采用单票15%（组合总仓位60%）、3批建仓、-15%止损。回测区间2023-05-22至2026-05-22（近3年，锚定数据最新交易日）。
假设前提：用户指定的60日年线落地为60日均线（收盘价）；用户指定季度调仓，最长4期（约1年）作为最大持有期；单票仓位上限来源于用户明示（15%），保守模式因致命风险减半至7.5%；建仓批次用户未明示，采用系统默认3批，保守+1至4批；止损止盈用户未明示，采用系统默认-15%/+20%，保守模式止损收紧至-10%。web_experience检索未提供可直接采用的行业惯例，参数使用静态默认或用户明示值。数据无成交量，未做流动性过滤。
数据来源：data/hfq_clean.csv（个股复权日频，仅使用close/preclose口径），标的名称经data/industry_clean.csv映射为代码；基准来自data/index_clean.csv（真实指数，关键词“半导体设备”匹配）或行业内等权篮子兜底。
"""
import pandas as pd
import numpy as np
import os

# ========== 参数配置 ==========
# STOCK_POOL：D 给出的主标的组合（北方华创、中微公司、拓荆科技、华海清科），不含 alternatives（供参考不进入策略）
STOCK_POOL = ["北方华创", "中微公司", "拓荆科技", "华海清科"]
BUY_CONDITION = "四股等权组合净值收盘价上穿60日均线"   # 来自 D.buy_trigger.condition；source=assumption
SELL_CONDITION = "四股等权组合净值收盘价跌破60日均线"  # 来自 D.sell_trigger.condition；source=assumption
HOLDING_PERIOD = 252      # 交易日，约1年，来自 D.holding_period（季度调仓，最长4期）
STOP_LOSS = -0.10         # 来自 D.risk_control.stop_loss（保守模式收紧至-10%）
TAKE_PROFIT = 0.20        # 来自 D.risk_control.take_profit（+20%）

# 保守版组合总仓位 = 单票7.5% * 4 = 30%（对应单票仓位上限7.5%）；ENTRY_BATCHES=4批等额建仓
MAX_WEIGHT = 0.30         # 组合占总资金最大仓位（保守版，单票7.5%×4）
ENTRY_BATCHES = 4         # 组合分批建仓次数（保守版，单票7.5%分为4批，每批投入总资金7.5%/1批≈总仓位1/4）

# 双分支回测（branch_mode: true）
BRANCH_MODE = True
# 原策略参数（覆盖前）：单票15%×4=60%，3批建仓，-15%止损，+20%止盈
ORIGINAL_MAX_WEIGHT = 0.60      # 单票15%×4
ORIGINAL_ENTRY_BATCHES = 3      # 原策略分批
ORIGINAL_STOP_LOSS = -0.15      # 原策略止损
ORIGINAL_TAKE_PROFIT = 0.20     # 原策略止盈

# 回测窗口：来自 D.backtest_window
START_DATE = pd.to_datetime("2023-05-22")
END_DATE = pd.to_datetime("2026-05-22")

# 基准选择：优先用 data/index_clean.csv 中与想法板块匹配的真实指数（关键词“半导体设备”）
BENCHMARK_INDEX_CODE = None      # 手动指定指数 code；留 None 让下方按关键词自动匹配
SECTOR_HINT = "半导体设备"       # 想法所属板块关键词，用于匹配 index_name
INDEX_PATH = "data/index_clean.csv"

# 交易成本：佣金万2.5双边 + 印花税万5卖出 + 滑点万5双边
BUY_COST_RATE = 0.00025 + 0.0005      # 佣金 + 滑点
SELL_COST_RATE = 0.00025 + 0.0005 + 0.0005  # 佣金 + 印花税 + 滑点

# 涨跌停阈值（按 stock_code 前缀）：688/300/301→20%，8/4/92开头(北交所)→30%，其余→10%
def _limit_threshold(code):
    c = str(code)
    if c.startswith(("688", "300", "301")): return 0.198
    if c.startswith(("8", "4", "92")):      return 0.298
    return 0.098

# ========== 数据加载 ==========
DATA_PATH = "data/hfq_clean.csv"
MAP_PATH = "data/industry_clean.csv"
FALLBACK_MAP_PATH = "data/stock_list_clean.csv"   # 名称映射回退表

def _name_to_code():
    ind = pd.read_csv(MAP_PATH, dtype={"stock_code": str})
    ind["stock_code"] = ind["stock_code"].str.zfill(6)
    m = dict(zip(ind["stock_name"], ind["stock_code"]))
    # 回退：industry_clean.csv 未覆盖的个股/更名股，用 stock_list_clean.csv 补齐
    try:
        fb = pd.read_csv(FALLBACK_MAP_PATH, dtype={"stock_code": str})
        fb["stock_code"] = fb["stock_code"].str.zfill(6)
        for _, r in fb.iterrows():
            m.setdefault(r["stock_name"], r["stock_code"])
    except Exception:
        pass
    return m

def load_data():
    name2code = _name_to_code()
    target_codes = []
    for n in STOCK_POOL:
        c = name2code.get(n)
        if c:
            target_codes.append(c)
        else:
            print(f"[WARN] 名称未映射到代码，跳过：{n}")
    if not target_codes:
        raise ValueError("STOCK_POOL 中没有任何名称能映射到 data/hfq_clean.csv 的代码")
    target_codes = set(target_codes)

    parts = []
    for chunk in pd.read_csv(DATA_PATH, chunksize=500_000, dtype={"stock_code": str}):
        chunk["stock_code"] = chunk["stock_code"].astype(str).str.zfill(6)
        sub = chunk[chunk["stock_code"].isin(target_codes)]
        if len(sub):
            parts.append(sub)
    if not parts:
        raise ValueError("过滤后无数据，检查 STOCK_POOL 代码与 backtest_window 区间是否覆盖")
    raw = pd.concat(parts, ignore_index=True)
    raw["date"] = pd.to_datetime(raw["TRADINGDAY"], format="%Y/%m/%d")
    # 只载入 close 口径列：open/high/low 复权口径与 close 不一致（见数据契约），不载入以防误用
    df = raw[["date", "stock_code", "ADJCLOSEPRICE", "ADJPREVCLOSE", "pct_chg"]].copy()
    df = df.rename(columns={"ADJCLOSEPRICE": "close", "ADJPREVCLOSE": "preclose"})
    df = df.sort_values(["stock_code", "date"]).reset_index(drop=True)
    # 按 D.backtest_window 过滤区间
    df = df[(df["date"] >= START_DATE) & (df["date"] <= END_DATE)]
    return df

# ========== 基准加载（真实指数优先，行业等权篮子兜底） ==========
def load_benchmark(df, strategy_dates):
    """返回与策略等长的基准净值序列（首日=1），以及基准名称。
    - 优先：data/index_clean.csv 中匹配 BENCHMARK_INDEX_CODE 或 SECTOR_HINT 的真实指数；
      指数日收益 = close/preclose - 1（价格口径，禁止用 open）。
    - 兜底：文件中无匹配 → 用 STOCK_POOL 成分股构建“行业内等权篮子”买入持有净值
      （等同方案 A 篮子，仅 stats 无信号/无成本）。
    """
    strategy_dates = pd.DatetimeIndex(strategy_dates).sort_values()
    # —— 尝试真实指数 ——
    if os.path.exists(INDEX_PATH):
        idx = pd.read_csv(INDEX_PATH, dtype={"index_code": str})
        if not idx.empty:
            chosen = None
            if BENCHMARK_INDEX_CODE:
                sub = idx[idx["index_code"] == str(BENCHMARK_INDEX_CODE)]
                if not sub.empty:
                    chosen = sub
            if chosen is None and SECTOR_HINT:
                sub = idx[idx["index_name"].astype(str).str.contains(SECTOR_HINT, na=False)]
                if not sub.empty:
                    chosen = sub
            if chosen is not None:
                chosen = chosen.copy()
                chosen["date"] = pd.to_datetime(chosen["TRADINGDAY"], format="%Y/%m/%d")
                chosen = chosen[(chosen["date"] >= START_DATE) & (chosen["date"] <= END_DATE)]
                chosen = chosen.dropna(subset=["date"]).sort_values("date").drop_duplicates("date")
                chosen["ret"] = chosen["close"].pct_change().fillna(0)
                bench = chosen.set_index("date")["ret"].reindex(strategy_dates, method="ffill").fillna(0)
                bench_net = (1 + bench).cumprod()
                bench_net = bench_net / bench_net.iloc[0]
                name = str(chosen["index_name"].iloc[0])
                print(f"[INFO] 基准=真实指数：{name} ({chosen['index_code'].iloc[0]})")
                return bench_net, name
    # —— 兜底：行业内等权篮子（买入持有，无信号/无成本） ——
    print("[INFO] 未匹配到真实指数，基准回退为行业内等权篮子（买入持有）")
    d = df.copy()
    d["ret"] = d["close"] / d["preclose"] - 1
    daily = d.groupby("date")["ret"].mean().reindex(strategy_dates, method="ffill").fillna(0)
    net = (1 + daily).cumprod()
    net = net / net.iloc[0]
    return net, "行业内等权篮子(买入持有)"

# ========== 信号生成 ==========
def generate_signals(df):
    """
    信号只用 close 口径。禁止计算 open_ret / 使用 open 列。
    四股等权组合净值：按日等权平均日收益 -> 组合净值（close 口径）。
    买入信号：组合净值上穿60日均线（T日收盘决定）-> T+1 生效；卖出信号：下穿60日均线。
    """
    df = df.copy()
    df["close_ret"] = df["close"] / df["preclose"] - 1
    # 逐股真实涨跌停阈值：_limit_threshold 返回小数，pct_chg 为 %，×100
    df["limit_pct"] = df["stock_code"].apply(lambda c: _limit_threshold(c) * 100)
    df["hit_limit"] = (df["pct_chg"].abs() >= df["limit_pct"]).astype(int)
    daily = df.groupby("date").agg(
        close_ret=("close_ret", "mean"),
        hit_limit=("hit_limit", "max"),   # 任一成分股触限 → 当日不调仓
    ).reset_index()
    daily = daily.sort_values("date").reset_index(drop=True)
    daily["net"] = (1 + daily["close_ret"]).cumprod()
    daily["ma60"] = daily["net"].rolling(60).mean()
    daily["signal"] = 0
    daily.loc[daily["net"] > daily["ma60"], "signal"] = 1   # T 日收盘决定
    return daily

# ========== 回测执行 ==========
def run_backtest(daily, max_weight=MAX_WEIGHT, entry_batches=ENTRY_BATCHES,
                 stop_loss=STOP_LOSS, take_profit=TAKE_PROFIT, holding_period=HOLDING_PERIOD):
    """
    close-to-close 逐日模拟（唯一口径：close，禁止用 open）。
    - T 日收盘出 signal → T+1 日收盘成交，成交日组合收益 = close_ret（= close/preclose-1）。
    - 组合目标仓位 = initial * max_weight；分 entry_batches 批等额建仓；
      止损/止盈按组合净值（持仓市值 / 累计投入 - 1）判定。
    - 执行顺序：①持仓先吃当日收益 → ②收盘执行（止损止盈优先 > 分批买入 > 信号卖出）→ ③估值 → ④止损止盈检查。
    - 涨停不买/跌停不卖：成交日 hit_limit=True（篮子内任一成分股触限）→ 放弃/顺延；止损止盈必须执行。
    - 最大持有期：持仓天数 > HOLDING_PERIOD（交易日）则强制出场（再平衡）。
    """
    daily = daily.sort_values("date").reset_index(drop=True)
    n = len(daily)
    initial = 1_000_000.0
    cash = initial
    position = 0.0                # 持仓市值
    target_position = initial * max_weight      # 组合目标仓位（市值）
    batch_size = target_position / entry_batches  # 每批买入额
    batches_done = 0              # 已建仓批次
    entry_cost = 0.0              # 累计投入
    hold_days = 0                 # 持仓天数计数
    stop_pending = False
    take_profit_pending = False
    rows, trades = [], []

    for i in range(n):
        date = daily["date"].iloc[i]
        close_ret = daily["close_ret"].iloc[i]                       # 当日组合收益
        hit_limit = bool(daily["hit_limit"].iloc[i]) if "hit_limit" in daily else (abs(close_ret) >= 0.098)
        prev_signal = int(daily["signal"].iloc[i - 1]) if i > 0 else 0  # T 日信号 → 今日成交

        # ① 已有持仓吃当日收益（持有到当日收盘）
        if position > 0:
            position *= (1 + close_ret)
            hold_days += 1

        # ② 收盘执行（优先级：止损/止盈 > 最大持有期到期 > 分批建仓 > 信号卖出）
        if position > 0 and (stop_pending or take_profit_pending or hold_days > holding_period):
            # 强制出场（止损/止盈/到期必须执行，跌停也卖，收盘价成交）
            cash += position * (1 - SELL_COST_RATE)
            position = 0.0
            entry_cost = 0.0
            batches_done = 0
            hold_days = 0
            stop_pending = False
            take_profit_pending = False
            trades.append({"date": date, "action": "sell", "ret": close_ret})
        elif prev_signal == 1 and batches_done < entry_batches and cash > 0:
            # 分批建仓：信号=1 且尚有批次未建、有现金、当日无成分股触涨跌停 → 买入一批
            if not hit_limit:
                invest = min(batch_size, cash)
                cash -= invest * (1 + BUY_COST_RATE)
                position += invest
                entry_cost += invest
                batches_done += 1
                trades.append({"date": date, "action": "buy", "ret": close_ret})
        elif position > 0 and prev_signal == 0:
            # 信号卖出：T 日信号翻空 → 今日收盘卖出（跌停顺延到次日）
            if not hit_limit:
                cash += position * (1 - SELL_COST_RATE)
                position = 0.0
                entry_cost = 0.0
                batches_done = 0
                hold_days = 0
                trades.append({"date": date, "action": "sell", "ret": close_ret})

        # ③ 收盘估值
        total = cash + position

        # ④ 止损/止盈检查（组合净值层面，相对累计投入；收盘后判定，次日收盘执行）
        if position > 0 and entry_cost > 0:
            pnl = position / entry_cost - 1
            if pnl <= stop_loss:
                stop_pending = True
            elif take_profit is not None and pnl >= take_profit:
                take_profit_pending = True

        rows.append({"date": date, "value": total})

    equity_curve = pd.DataFrame(rows)
    equity_curve["cum_return"] = equity_curve["value"] / initial
    trades_df = pd.DataFrame(trades)
    return equity_curve, trades_df

# ========== 绩效分析 ==========
def calculate_metrics(equity_curve, trades_df=None, bench_net=None,
                      bench_name="无基准", risk_free_rate=0.02):
    if equity_curve.empty or len(equity_curve) < 2:
        return {"annual_return": np.nan, "max_drawdown": np.nan,
                "sharpe": np.nan, "excess_return": np.nan,
                "info_ratio": np.nan, "benchmark_name": bench_name}
    v = equity_curve["value"].values
    dates = equity_curve["date"]
    total_ret = v[-1] / v[0] - 1
    days = (dates.iloc[-1] - dates.iloc[0]).days
    annual_return = (1 + total_ret) ** (365.0 / days) - 1 if days > 0 else np.nan
    peak = np.maximum.accumulate(v)
    max_drawdown = float(((peak - v) / peak).max())
    daily_ret = equity_curve["value"].pct_change().fillna(0)
    sharpe = (np.sqrt(252) * daily_ret.mean() / daily_ret.std()
              if daily_ret.std() and len(daily_ret) >= 2 else np.nan)
    info_ratio = np.nan
    excess_return = np.nan
    if bench_net is not None and len(bench_net) == len(equity_curve):
        bench_daily = bench_net.pct_change().fillna(0).set_axis(equity_curve.index)
        excess = (daily_ret - bench_daily).iloc[1:]
        if len(excess) >= 2 and excess.std() > 0:
            info_ratio = excess.mean() / excess.std() * np.sqrt(252)
        strategy_net_last = equity_curve["value"].iloc[-1] / equity_curve["value"].iloc[0]
        bench_last = bench_net.iloc[-1]
        excess_return = strategy_net_last / bench_last - 1 if bench_last else np.nan
    return {"annual_return": annual_return, "max_drawdown": max_drawdown,
            "sharpe": sharpe, "excess_return": excess_return,
            "info_ratio": info_ratio, "benchmark_name": bench_name}

# ========== 可视化 ==========
def plot_results(equity_curve, metrics, trades_df=None, bench_net=None,
                 equity_orig=None, metrics_orig=None):
    import matplotlib.pyplot as plt
    plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
    plt.rcParams['axes.unicode_minus'] = False
    if equity_curve.empty:
        print("[WARN] 无回测数据，跳过绘图")
        return
    v = equity_curve["value"].values
    peak = np.maximum.accumulate(v)
    drawdown = (peak - v) / peak
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    axes[0, 0].plot(equity_curve["date"], equity_curve["value"] / v[0], label="策略净值")
    if bench_net is not None and len(bench_net) == len(equity_curve):
        axes[0, 0].plot(equity_curve["date"], bench_net.values / bench_net.values[0],
                         label="基准净值", linestyle="--", alpha=0.8)
    if equity_orig is not None and len(equity_orig) == len(equity_curve):
        axes[0, 0].plot(equity_curve["date"], equity_orig["value"].values / equity_orig["value"].iloc[0],
                         label="原策略净值", linestyle=":", alpha=0.8)
    axes[0, 0].set_title("净值曲线（策略 vs 基准）"); axes[0, 0].legend(); axes[0, 0].grid(alpha=0.3)
    if trades_df is not None and not trades_df.empty:
        buys = trades_df[trades_df["action"] == "buy"]
        sells = trades_df[trades_df["action"] == "sell"]
        axes[0, 0].scatter(buys["date"], [1] * len(buys), marker="^", color="green", s=80, label="买入")
        axes[0, 0].scatter(sells["date"], [1] * len(sells), marker="v", color="red", s=80, label="卖出")
        axes[0, 0].legend()
    axes[0, 1].fill_between(equity_curve["date"], -drawdown, 0, color="red", alpha=0.3)
    axes[0, 1].set_title("回撤曲线"); axes[0, 1].grid(alpha=0.3)
    monthly = equity_curve.set_index("date")["value"].resample("ME").last().pct_change().dropna()
    axes[1, 0].bar(range(len(monthly)), monthly.values, color="steelblue", alpha=0.7)
    axes[1, 0].set_title("月度收益分布"); axes[1, 0].grid(alpha=0.3)
    axes[1, 1].axis("off")
    if metrics_orig is not None:
        text = (f"【系统建议(保守)】\n年化: {metrics['annual_return']:.2%} 回撤: {metrics['max_drawdown']:.2%} 夏普: {metrics['sharpe']:.2f}\n"
                f"超额: {metrics['excess_return']:.2%} 信息比: {metrics['info_ratio']:.2f}\n"
                f"【原始意图】\n年化: {metrics_orig['annual_return']:.2%} 回撤: {metrics_orig['max_drawdown']:.2%} 夏普: {metrics_orig['sharpe']:.2f}\n"
                f"超额: {metrics_orig['excess_return']:.2%} 信息比: {metrics_orig['info_ratio']:.2f}\n"
                f"基准: {metrics['benchmark_name']}")
    else:
        text = (f"年化收益: {metrics['annual_return']:.2%}\n最大回撤: {metrics['max_drawdown']:.2%}\n"
                f"夏普比率: {metrics['sharpe']:.2f}\n超额收益: {metrics['excess_return']:.2%}\n"
                f"信息比率: {metrics['info_ratio']:.2f}\n基准: {metrics['benchmark_name']}")
    axes[1, 1].text(0.5, 0.5, text, transform=axes[1, 1].transAxes, ha="center", va="center",
                    fontsize=12, bbox=dict(boxstyle="round", facecolor="wheat", alpha=0.5))
    plt.tight_layout()
    out_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(out_dir, exist_ok=True)
    plt.savefig(os.path.join(out_dir, "backtest_plot.png"), dpi=150)
    plt.close()

if __name__ == "__main__":
    df = load_data()
    print(f"[INFO] 加载数据行数：{len(df)}")
    daily = generate_signals(df)
    print(f"[INFO] 信号日期范围：{daily['date'].min()} 至 {daily['date'].max()}")
    bench_net, bench_name = load_benchmark(df, daily["date"])
    bench_align = bench_net.reindex(daily["date"]).ffill().fillna(1.0)

    def _run_and_report(label, mw, eb, sl, tp, hp):
        eq, tr = run_backtest(daily, mw, eb, sl, tp, hp)
        m = calculate_metrics(eq, tr, bench_net=bench_align, bench_name=bench_name)
        print(f"=== 回测结果（{label}）===")
        print(f"年化收益: {m['annual_return']:.2%}")
        print(f"最大回撤: {m['max_drawdown']:.2%}")
        print(f"夏普比率: {m['sharpe']:.2f}")
        print(f"超额收益: {m['excess_return']:.2%}")
        print(f"信息比率: {m['info_ratio']}")
        print(f"基准: {m['benchmark_name']}")
        print(f"交易次数: {len(tr)}")
        print(f"策略末期净值: {eq['value'].iloc[-1]/eq['value'].iloc[0]:.3f}")
        print(f"基准末期净值: {bench_align.iloc[-1]:.3f}")
        return eq, tr, m

    equity_cons, trades_cons, metrics_cons = _run_and_report(
        "系统建议(保守)", MAX_WEIGHT, ENTRY_BATCHES, STOP_LOSS, TAKE_PROFIT, HOLDING_PERIOD)

    equity_orig = trades_orig = metrics_orig = None
    if BRANCH_MODE:
        equity_orig, trades_orig, metrics_orig = _run_and_report(
            "原始意图", ORIGINAL_MAX_WEIGHT, ORIGINAL_ENTRY_BATCHES,
            ORIGINAL_STOP_LOSS, ORIGINAL_TAKE_PROFIT, HOLDING_PERIOD)
        cons_ex = metrics_cons.get("excess_return"); orig_ex = metrics_orig.get("excess_return")
        if cons_ex is not None and orig_ex is not None and not (pd.isna(cons_ex) or pd.isna(orig_ex)):
            print(f"[INFO] 保守模式拖累(超额收益差)= 保守超额 {cons_ex:.2%} - 原策略超额 {orig_ex:.2%} = {cons_ex - orig_ex:.2%}")

    plot_results(equity_cons, metrics_cons, trades_cons, bench_net=bench_align,
                 equity_orig=equity_orig, metrics_orig=metrics_orig)