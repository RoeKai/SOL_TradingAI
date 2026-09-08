"""Rules-based, timezone-aware daily closed-trade review."""

from __future__ import annotations

from collections import defaultdict
from datetime import date as Date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import math
import tempfile


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError, ValueError):
        return 0.0


class DailyReview:
    def __init__(self, report_dir: str | Path = "reports", timezone: str = "Asia/Kuala_Lumpur", *, paths=None,
                 strategies=()) -> None:
        self.report_dir = Path(report_dir)
        self.timezone = ZoneInfo(timezone)
        self.paths, self.strategies = paths, tuple(strategies)
        if paths:
            paths.require(self.report_dir, f'reports/{paths.mode}')

    def _datetime(self, value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (float, int)):
            try:
                parsed = datetime.fromtimestamp(value / 1000 if value > 10**11 else value, self.timezone)
            except (ValueError, OverflowError, OSError):
                return None
        elif isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        # All runtime timestamps are aware UTC. Legacy naive timestamps are
        # interpreted in the explicitly configured report timezone.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.timezone)
        return parsed.astimezone(self.timezone)

    @staticmethod
    def _net(trade: dict[str, Any]) -> float:
        if trade.get("net_pnl") is not None:
            return _number(trade["net_pnl"])
        return _number(trade.get("realized_pnl", trade.get("pnl", 0))) - _number(trade.get("fees", 0))

    def _duration(self, trade: dict[str, Any]) -> float:
        if trade.get("holding_seconds") is not None:
            return max(0.0, _number(trade["holding_seconds"]))
        opened = self._datetime(trade.get("opened_at", trade.get("entry_time")))
        closed = self._datetime(trade.get("closed_at", trade.get("exit_time")))
        return max(0.0, (closed - opened).total_seconds()) if opened and closed else 0.0

    def generate(self, trades: list[dict[str, Any]], date: str | None = None, *, context=None) -> str:
        target = Date.fromisoformat(date) if date else datetime.now(self.timezone).date()
        rows: list[tuple[datetime, dict[str, Any]]] = []
        excluded = 0
        for trade in trades:
            if str(trade.get("status", "closed")).lower() not in {"closed", "filled", "completed"}:
                continue
            when = self._datetime(trade.get("closed_at", trade.get("exit_time")))
            if when is None:
                excluded += 1
                continue
            if when.date() == target:
                rows.append((when, trade))
        rows.sort(key=lambda pair: pair[0])
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for _, trade in rows:
            groups[str(trade.get("strategy", "unknown"))].append(trade)
        for strategy in self.strategies:
            groups.setdefault(strategy, [])
        net = sum(self._net(trade) for _, trade in rows)
        lines = [f"# SOL AI 每日复盘 · {target.isoformat()}", "",
                 f"统计时区：{self.timezone.key}；仅统计该日已平仓交易，盈亏采用扣除费用后的净值。", "",
                 f"- 完成交易：{len(rows)} 笔", f"- 净盈亏：{net:+.4f} USDT",
                 f"- 缺少有效平仓时间而排除的记录：{excluded} 笔", "",
                 "## 策略表现", "",
                 "| 策略 | 笔数 | 胜率 | 净盈亏 USDT | 盈亏比 | 利润因子 | 单笔期望 | 已实现最大回撤 USDT | 平均持仓 |",
                 "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        recommendations = []
        for strategy, items in sorted(groups.items()):
            if not items:
                lines.append(f'| {strategy} | 0 | — | 0 | — | — | — | — | — |')
                recommendations.append(f'{strategy}：没有已平仓样本，不能评价策略收益。')
                continue
            values = [self._net(item) for item in items]
            wins = [value for value in values if value > 0]
            losses = [-value for value in values if value < 0]
            gross_win, gross_loss = sum(wins), sum(losses)
            payoff = f"{(gross_win / len(wins)) / (gross_loss / len(losses)):.2f}" if wins and losses else "—"
            factor = f"{gross_win / gross_loss:.2f}" if gross_loss else ("∞" if gross_win else "—")
            equity = peak = drawdown = 0.0
            for value in values:
                equity += value
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
            avg_seconds = sum(self._duration(item) for item in items) / len(items)
            safe_strategy = strategy.replace("|", "/").replace("\n", " ")
            lines.append(f"| {safe_strategy} | {len(items)} | {len(wins) / len(items):.1%} | "
                         f"{sum(values):+.4f} | {payoff} | {factor} | {sum(values) / len(values):+.4f} | "
                         f"{drawdown:.4f} | {avg_seconds / 60:.1f} 分钟 |")
            if len(items) < 10:
                recommendations.append(f"{safe_strategy}：样本仅 {len(items)} 笔，不足以推断稳定性，不建议据此扩大仓位。")
            if sum(values) < 0:
                recommendations.append(f"{safe_strategy}：当日净亏损，建议暂停新增开仓并检查滑点、手续费和大盘过滤；不要扩大亏损预算。")
            if len(losses) >= 2:
                recommendations.append(f"{safe_strategy}：复核连续亏损熔断是否触发，酌情收紧单笔风险和反弹确认阈值。")
        if not rows:
            lines.append("| 无已平仓交易 | 0 | — | 0 | — | — | — | — | — |")
        lines.extend(["", "## 规则化建议（仅建议，不会自动改参或下单）", ""])
        lines.extend(f"- {item}" for item in recommendations or ["暂无足够信息调整策略，保持原风控约束并继续模拟观察。"])
        if context is not None:
            lines.extend(['', '## 闭环运行证据（PAPER 模拟，不是实盘）', '',
                          '| 策略 | 评分次数 | 候选信号 | 风控通过 | 风控拒绝 |',
                          '| --- | ---: | ---: | ---: | ---: |'])
            for name in self.strategies:
                item = context.get('decisions', {}).get(name, {})
                lines.append(f"| {name} | {item.get('evaluations', 0)} | {item.get('signals', 0)} | "
                             f"{item.get('approved', 0)} | {item.get('rejected', 0)} |")
            eq = context.get('equity', {})
            lines.extend(['', f"- 账户含浮盈采样最大回撤：{eq.get('max_drawdown', 0):.4f} USDT；"
                          f"有效采样 {eq.get('samples', 0)} 次（断线/停机缺口不作插值）。",
                          f"- 报告生成时未平仓：{context.get('open_positions', 0)}；未平仓盈亏不计入策略胜率。",
                          '- 私有交易客户端：未构造；真实订单路径：硬关闭。'])
        lines.extend(["", "## 口径与限制", "",
                      "- 胜率：净盈亏大于零的交易数 / 已平仓交易数；零收益不是胜单。",
                      "- 盈亏比：平均盈利 / 平均亏损绝对值；利润因子：总盈利 / 总亏损绝对值。",
                      "- 最大回撤按当日已实现净盈亏曲线计算，不代表账户含浮动盈亏的完整回撤。",
                      "- 不调用大模型；模拟结果不证明真实成交、未来盈利或交易所保护单已生效。", ""])
        return "\n".join(lines)

    def write(self, trades: list[dict[str, Any]], date: str | None = None, *, context=None) -> Path:
        target = Date.fromisoformat(date) if date else datetime.now(self.timezone).date()
        report = self.generate(trades, target.isoformat(), context=context)
        if self.paths:
            self.paths.file(f'reports/{self.paths.mode}/review-{target.isoformat()}.md')
            self.paths.directory(f'reports/{self.paths.mode}')
        else:
            self.report_dir.mkdir(parents=True, exist_ok=True)
        path = self.report_dir / f"review-{target.isoformat()}.md"
        # A web read and the midnight scheduler may generate the same day at
        # once. Unique temp names keep the final replace atomic for both.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.report_dir,
                                         prefix=f".review-{target.isoformat()}-", suffix=".tmp", delete=False) as stream:
            stream.write(report)
            temporary = Path(stream.name)
        temporary.replace(path)
        return path
