"""
策略名称：白酒龙头20日均线趋势跟踪策略
回测说明：对贵州茅台、五粮液、泸州老窖及同赛道可比标的（山西汾酒、洋河股份、古井贡酒）分别执行个股级20日均线上下穿策略，独立信号、独立仓位与风控，组合净值汇总。
假设前提：
- 采用收盘价（ADJCLOSEPRICE）计算20日简单移动平均线（用户未指定，采用默认）。
- 买入条件：个股收盘价上穿20日均线时买入（用户指定）。
- 卖出条件：个股收盘价下穿20日均线时卖出（用户指定）。
- 单票最大仓位：保守版16.65%（触发保守模式，满仓三票等权33.3%减半），原策略版33.3%（用户明示满仓推断等权）。
- 建仓批次：保守版4批，原策略版3批（保守模式+1）。
- 止损/止盈：保守版-10%/+20%，原策略版-15%/+20%（止盈来自默认，止损保守收紧）。
- 持有周期：最长4期（约252个交易日），到期强制平仓。
- 信号检查频率：每日（signal_check_freq=daily）。
- 交易成本：佣金万2.5双边 + 印花税万5卖出 + 滑点万5双边。
- 数据无成交量，未做流动性过滤。
数据来源：data/hfq_clean.csv（个股复权日频），标的名称经 data/industry_clean.csv 映射为代码；基准来自 data/index_clean.csv（真实指数）或行业内等权篮子兜底。
"""
import pandas as pd
import numpy as np
import os

# ========== 参数配置 ==========
STOCK_POOL = ["贵州茅台", "五粮液", "泸州老窖", "山西汾酒", "洋河股份", "古井贡酒"]
BUY_CONDITION = "个股收盘价上穿20日简单移动平均线"   # 用户指定
SELL_CONDITION = "个股收盘价下穿20日简单移动平均线"   # 用户指定
HOLDING_PERIOD = 252      # 交易日，来自 D.holding_period 最长4期（约4个季度）
STOP_LOSS = -0.10         # 来自 D.risk_control.stop_loss（保守版）
TAKE_PROFIT = 0.20        # 来自 D.risk_control.take_profit

# 个股级单票仓位/分批（保守版）
MAX_WEIGHT = 0.1665       # 单票最大仓位（组合初始资金占比）
ENTRY_BATCHES = 4         # 单票分批建仓次数

# 双分支回测：原策略参数（保守覆盖前）
BRANCH_MODE = True
ORIGINAL_MAX_WEIGHT = 0.333
ORIGINAL_ENTRY_BATCHES = 3
ORIGINAL_STOP_LOSS = -0.15
ORIGINAL_TAKE_PROFIT = 0.20

# 回测窗口：来自 D.backtest_window
START_DATE = pd.to_datetime("2023-05-22")
END_DATE = pd.to_datetime("2026-05-22")

# 基准选择：优先用 data/index_clean.csv 中与想法板块匹配的真实指数；
BENCHMARK_INDEX_CODE = None      # 手动指定指数 code；None 时按 SECTOR_HINT 子串匹配 index_name
SECTOR_HINT = "白酒"             # 行业关键词
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
FALLBACK_MAP_PATH = "data/stock_list_clean.csv"

def _name_to_code():
    ind = pd.read_csv(MAP_PATH, dtype={"stock_code": str})
    ind["stock_code"] = ind["stock_code"].str.zfill(6)
    m = dict(zip(ind["stock_name"], ind["stock_code"]))
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
    # 只载入 close 口径列：open/high/low 复权口径与 close 不一致，不载入
    df = raw[["date", "stock_code", "ADJCLOSEPRICE", "ADJPREVCLOSE", "pct_chg"]].copy()
    df = df.rename(columns={"ADJCLOSEPRICE": "close", "ADJPREVCLOSE": "preclose"})
    df = df.sort_values(["stock_code", "date"]).reset_index(drop=True)
    df = df[(df["date"] >= START_DATE) & (df["date"] <= END_DATE)]
    return df

# ========== 基准加载（真实指数优先，行业等权篮子兜底） ==========
def load_benchmark(df, strategy_dates):
    strategy_dates = pd.DatetimeIndex(strategy_dates).sort_values()
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
    生成每只股票的20日均线上下穿信号。
    返回：
      signals: DataFrame，index=日期，columns=stock_code，值为信号（1=买入，-1=卖出，0=无）
      stock_data: dict，key=stock_code，value=DataFrame(index=日期，包含 close_ret, pct_chg, hit_limit_up, hit_limit_down)
    信号生成：T日收盘后判断是否上穿/下穿，信号用于T+1日执行。
    """
    df = df.copy()
    df["close_ret"] = df["close"] / df["preclose"] - 1
    df["limit_pct"] = df["stock_code"].apply(lambda c: _limit_threshold(c) * 100)
    df["hit_limit_up"] = (df["pct_chg"] >= df["limit_pct"]).astype(int)
    df["hit_limit_down"] = (df["pct_chg"] <= -df["limit_pct"]).astype(int)

    # 获取所有交易日
    all_dates = sorted(df["date"].unique())
    all_dates = pd.DatetimeIndex(all_dates)

    signals_list = {}
    stock_data = {}

    for code, group in df.groupby("stock_code"):
        group = group.sort_values("date").set_index("date")
        # 计算20日均线
        group["ma20"] = group["close"].rolling(20).mean()
        # 上穿：昨日收盘<=昨日均线，今日收盘>今日均线
        up_cross = (group["close"] > group["ma20"]) & (group["close"].shift(1) <= group["ma20"].shift(1))
        # 下穿：昨日收盘>=昨日均线，今日收盘<今日均线
        down_cross = (group["close"] < group["ma20"]) & (group["close"].shift(1) >= group["ma20"].shift(1))
        signal = pd.Series(0, index=group.index)
        signal[up_cross] = 1
        signal[down_cross] = -1
        # 重索引到统一日期
        signal = signal.reindex(all_dates, fill_value=0)
        signals_list[code] = signal

        # 保留每日必要数据
        stock_data[code] = group[["close_ret", "pct_chg", "hit_limit_up", "hit_limit_down"]].reindex(all_dates, fill_value=0)

    signals = pd.DataFrame(signals_list, index=all_dates)
    # 填充 NaN 为 0
    signals = signals.fillna(0).astype(int)
    return signals, stock_data

# ========== 回测执行 ==========
def run_backtest(signals, stock_data, max_weight=MAX_WEIGHT, entry_batches=ENTRY_BATCHES,
                 stop_loss=STOP_LOSS, take_profit=TAKE_PROFIT, holding_period=HOLDING_PERIOD):
    """
    多标的独立信号回测。
    signals: DataFrame，index=date, columns=stock_code, values=signal (1买,-1卖,0无)
    stock_data: dict of DataFrame，每只股票每日的 close_ret, pct_chg, hit_limit_up, hit_limit_down
    初始资金 1,000,000，单票最大投入 = initial * max_weight，分 entry_batches 批建仓。
    止损/止盈按单票净值（持仓市值 / 累计投入 - 1）判定；到期强制平仓。
    """
    codes = list(signals.columns)
    # 初始化状态
    initial = 1_000_000.0
    cash = initial
    positions = {code: 0.0 for code in codes}
    entry_costs = {code: 0.0 for code in codes}
    batches_done = {code: 0 for code in codes}
    hold_days = {code: 0 for code in codes}
    stop_pending = {code: False for code in codes}
    take_profit_pending = {code: False for code in codes}
    target_pos = initial * max_weight        # 单票目标投入
    batch_size = target_pos / entry_batches  # 每批投入额

    dates = signals.index
    n = len(dates)
    rows = []
    trades = []

    # 提前将 signals 向前移一行，表示 T-1 日信号（用于 T 日执行）
    shifted_signals = signals.shift(1)

    for i in range(n):
        date = dates[i]
        # 当日信号（来自 T-1 日）
        prev_signal = shifted_signals.iloc[i] if i > 0 else pd.Series(0, index=signals.columns)

        # 先处理卖出（强制卖出优先，再信号卖出）
        for code in codes:
            if positions[code] <= 0:
                continue
            # 获取当日该股数据
            cdata = stock_data[code].loc[date]
            close_ret = cdata["close_ret"]
            # ① 更新持仓市值
            positions[code] *= (1 + close_ret)
            hold_days[code] += 1

            # ② 强制出场（止损/止盈/到期）
            if stop_pending[code] or take_profit_pending[code] or hold_days[code] > holding_period:
                sell_amount = positions[code]
                cash += sell_amount * (1 - SELL_COST_RATE)
                positions[code] = 0.0
                entry_costs[code] = 0.0
                batches_done[code] = 0
                hold_days[code] = 0
                stop_pending[code] = False
                take_profit_pending[code] = False
                trades.append({"date": date, "action": "sell", "stock_code": code, "ret": close_ret})
                continue

            # ③ 信号卖出（下穿，且当日未跌停）
            if prev_signal.get(code, 0) == -1:
                if not bool(cdata["hit_limit_down"]):
                    sell_amount = positions[code]
                    cash += sell_amount * (1 - SELL_COST_RATE)
                    positions[code] = 0.0
                    entry_costs[code] = 0.0
                    batches_done[code] = 0
                    hold_days[code] = 0
                    trades.append({"date": date, "action": "sell", "stock_code": code, "ret": close_ret})

        # 再处理买入（信号买入，分批建仓）
        for code in codes:
            cdata = stock_data[code].loc[date]
            if prev_signal.get(code, 0) == 1 and batches_done[code] < entry_batches and cash > 0:
                # 当日未涨停才可买入
                if not bool(cdata["hit_limit_up"]):
                    invest = min(batch_size, cash)
                    cash -= invest * (1 + BUY_COST_RATE)
                    positions[code] += invest
                    entry_costs[code] += invest
                    batches_done[code] += 1
                    if batches_done[code] == 1:  # 首次买入，开始计持有天数
                        hold_days[code] = 1
                    else:
                        hold_days[code] = max(hold_days[code], 1)
                    trades.append({"date": date, "action": "buy", "stock_code": code, "ret": cdata["close_ret"]})

        # ④ 收盘后止损止盈检查（基于当前持仓）
        for code in codes:
            if positions[code] > 0 and entry_costs[code] > 0:
                pnl = positions[code] / entry_costs[code] - 1
                if pnl <= stop_loss:
                    stop_pending[code] = True
                elif take_profit is not None and pnl >= take_profit:
                    take_profit_pending[code] = True

        # ⑤ 记录总资产
        total_value = cash + sum(positions.values())
        rows.append({"date": date, "value": total_value})

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
    axes[0, 0].set_title("净值曲线（策略 vs 基准）")
    axes[0, 0].legend()
    axes[0, 0].grid(alpha=0.3)
    if trades_df is not None and not trades_df.empty:
        buys = trades_df[trades_df["action"] == "buy"]
        sells = trades_df[trades_df["action"] == "sell"]
        axes[0, 0].scatter(buys["date"], [1] * len(buys), marker="^", color="green", s=80, label="买入")
        axes[0, 0].scatter(sells["date"], [1] * len(sells), marker="v", color="red", s=80, label="卖出")
        axes[0, 0].legend()
    axes[0, 1].fill_between(equity_curve["date"], -drawdown, 0, color="red", alpha=0.3)
    axes[0, 1].set_title("回撤曲线")
    axes[0, 1].grid(alpha=0.3)
    monthly = equity_curve.set_index("date")["value"].resample("ME").last().pct_change().dropna()
    axes[1, 0].bar(range(len(monthly)), monthly.values, color="steelblue", alpha=0.7)
    axes[1, 0].set_title("月度收益分布")
    axes[1, 0].grid(alpha=0.3)
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
    # 1. 数据加载
    df = load_data()
    print(f"[INFO] 加载数据行数：{len(df)}")
    # 2. 信号生成
    signals, stock_data = generate_signals(df)
    print(f"[INFO] 信号日期范围：{signals.index.min()} 至 {signals.index.max()}")
    # 3. 基准加载
    bench_net, bench_name = load_benchmark(df, signals.index)
    bench_align = bench_net.reindex(signals.index).ffill().fillna(1.0)

    # 4. 回测函数封装
    def _run_and_report(label, mw, eb, sl, tp, hp):
        eq, tr = run_backtest(signals, stock_data, max_weight=mw, entry_batches=eb,
                              stop_loss=sl, take_profit=tp, holding_period=hp)
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

    # 保守版
    equity_cons, trades_cons, metrics_cons = _run_and_report(
        "系统建议(保守)", MAX_WEIGHT, ENTRY_BATCHES, STOP_LOSS, TAKE_PROFIT, HOLDING_PERIOD)

    # 原策略版
    equity_orig = trades_orig = metrics_orig = None
    if BRANCH_MODE:
        equity_orig, trades_orig, metrics_orig = _run_and_report(
            "原始意图", ORIGINAL_MAX_WEIGHT, ORIGINAL_ENTRY_BATCHES,
            ORIGINAL_STOP_LOSS, ORIGINAL_TAKE_PROFIT, HOLDING_PERIOD)
        cons_ex = metrics_cons.get("excess_return")
        orig_ex = metrics_orig.get("excess_return")
        if cons_ex is not None and orig_ex is not None and not (pd.isna(cons_ex) or pd.isna(orig_ex)):
            print(f"[INFO] 保守模式拖累(超额收益差)= 保守超额 {cons_ex:.2%} - 原策略超额 {orig_ex:.2%} = {cons_ex - orig_ex:.2%}")

    # 5. 绘图
    plot_results(equity_cons, metrics_cons, trades_cons, bench_net=bench_align,
                 equity_orig=equity_orig, metrics_orig=metrics_orig)