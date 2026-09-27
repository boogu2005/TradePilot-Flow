"""
止损管理器 — 统一管理止损的创建、更新、替换。
核心安全模式：新单先挂 → 旧单后撤，避免裸仓风险。

======================= OKX = 唯一真相源 =======================
所有 SL 数量：
1. 实时 fetch_position() 获取 OKX 实际持仓
2. normalize_order_amount() 校验精度/最小值
3. 禁止使用 trade.amount 作为下单数量

v4: 所有创建统一走 ProtectionCreator（状态机守卫 + 去重 + clientOrderId）
================================================================
"""
from __future__ import annotations

from loguru import logger
from sqlalchemy.orm import Session

from database.models import Trade, Order
from core.exchange_runtime import runtime


def _norm_amt(symbol: str, amount: float, exchange: str = "okx") -> float | None:
    """Normalize order amount (delegates to exchange utility)."""
    from exchange_engine.exchange import normalize_order_amount
    return normalize_order_amount(symbol, amount, exchange)


class StopLossManager:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def _norm_inst_id(pair: str) -> str:
        """Normalize pair to OKX instId format: BTC-USDT-SWAP"""
        s = (pair or "").upper().replace("/", "").replace(":USDT", "")
        if s.endswith("USDT"):
            return f"{s[:-4]}-USDT-SWAP"
        return s

    async def _get_real_contracts(self, trade: Trade, label: str = "") -> float | None:
        """
        获取实时持仓 — 三级 fallback 链：

        Level 1: WS PositionTracker（零延迟，99% 命中）
        Level 2: REST fetch_positions()（绕过缓存，强制刷新）
        Level 3: trade.amount DB 快照（最后手段）

        设计参考: Freqtrade 的 WS → REST 双通道架构 +
                  poly-position-watcher 的 HTTP fallback 模式
        """
        trade_ex = trade.exchange or "okx"

        # ———— Level 1: WS PositionTracker ————
        pos = runtime.get_position(trade.pair)
        if pos and pos.get("contracts", 0) > 0:
            return pos["contracts"]

        # ———— Level 2: REST API (force refresh, bypass cache) ————
        try:
            from exchange_engine.exchange import fetch_positions
            from core.position_tracker import position_tracker
            positions = await fetch_positions(exchange=trade_ex)
            inst_id = self._norm_inst_id(trade.pair)
            found_contracts = None
            for p in positions:
                p_symbol = (p.get("symbol", "") or "").upper().replace("/", "").replace(":USDT", "")
                if p_symbol.endswith("USDT"):
                    p_symbol = f"{p_symbol[:-4]}-USDT-SWAP"
                if p_symbol == inst_id:
                    contracts = float(p.get("contracts", 0))
                    if contracts > 0:
                        logger.info(
                            f"[{trade_ex}] REST fallback 获取仓位 {trade.pair}: "
                            f"{contracts}张 (WS未命中)"
                        )
                        found_contracts = contracts
            # v6: Backfill WS cache from REST data so next lookup hits WS
            if positions:
                position_tracker.update_from_rest(positions)
            if found_contracts is not None:
                return found_contracts
        except Exception as e:
            logger.warning(f"[{trade_ex}] REST fallback 失败 {trade.pair}: {e}")

        # ———— Level 3: DB 快照 (last resort) ————
        if trade.amount > 0:
            logger.warning(
                f"[{trade_ex}] WS/REST 均未命中 {trade.pair}，"
                f"使用 DB 快照={trade.amount}张 作为 fallback"
            )
            return trade.amount

        return None

    async def update_stop_loss(self, trade: Trade, new_sl: float, sync_exchange=True, commit=True) -> bool:
        """
        更新止损位。

        ===== OKX 真相源 =====
        SL 数量使用实时 OKX 持仓。
        v4: 创建统一走 ProtectionCreator。
        ======================

        策略：
          - sync_exchange=True: 同步到交易所（新单先挂 → 旧单后撤）
          - sync_exchange=False: 仅更新 DB

        commit 参数：
          - commit=True（默认）: 内部执行 session.commit()
          - commit=False: 仅 session.flush()，由调用方统一提交
        """
        if new_sl <= 0:
            return False

        trade_ex = trade.exchange or "okx"
        trade.stop_loss = new_sl

        if sync_exchange:
            # 获取实时持仓
            real_contracts = await self._get_real_contracts(trade, "更新SL前")
            if real_contracts is None or real_contracts <= 0:
                logger.warning(f"[{trade_ex}] 更新SL跳过：{trade.pair} 无实时仓位")
                return False

            if trade.open_sl_orders:
                ok = await self._replace_exchange_sl(trade, new_sl, real_contracts)
            elif real_contracts > 0 and new_sl > 0:
                ok = await self._place_new_sl(trade, new_sl, real_contracts)
            else:
                ok = True
            if not ok:
                await self._emergency_protect(trade)
                return False

        if commit:
            self.session.commit()
        else:
            self.session.flush()

        logger.info(f"[{trade_ex}] 止损更新 {trade.pair} SL={new_sl}")
        return True

    async def _replace_exchange_sl(self, trade: Trade, new_sl: float, real_contracts: float) -> bool:
        """
        原子替换交易所止损单：撤销所有旧SL → 挂新SL。
        保证同一仓位始终只有 1 个止损单。

        OKX 全仓止损单在减仓后依然有效，不需要保留旧单作为"安全网"。
        每次替换都是：取消全部旧单 → 创建1个新单。

        v6: 取消所有旧 SL（stoploss + trailing_stop），不再保留"安全网"。
            create_sl() 内部 Guard 5b 处理价格不匹配的旧单撤销。
        """
        trade_ex = trade.exchange or "okx"
        sl_amt = _norm_amt(trade.pair, real_contracts, trade_ex)
        if sl_amt is None or sl_amt <= 0:
            logger.warning(f"[{trade_ex}] 替换SL数量={sl_amt}，跳过")
            return False

        # v6: 先撤销所有旧 SL 订单（从 DB 记录 + 交易所）
        # create_sl() 的 Guard 5b 也会撤销 API 能看到的旧单，但这里是
        # DB 侧的兜底 — 确保 DB 里记录的旧单也被标记为关闭。
        all_old_sl = [o for o in trade.open_sl_orders
                      if o.ft_order_role in ("stoploss", "trailing_stop")]
        for old_sl in all_old_sl:
            try:
                await runtime.cancel_order(old_sl.order_id, trade.pair)
                old_sl.ft_is_open = False
                old_sl.status = "canceled"
            except Exception as e:
                err_str = str(e)
                if "51400" in err_str:
                    # Already gone from OKX
                    old_sl.ft_is_open = False
                    old_sl.status = "canceled"
                else:
                    logger.warning(f"[{trade_ex}] 撤旧SL失败 {trade.pair} {old_sl.order_id}: {e}")
        self.session.flush()

        # v5: 统一走 ProtectionCreator
        from core.protection_creator import protection_creator
        # Bump clOrdId version so OKX creates a new order (not dedup to old one)
        protection_creator.bump_clordid_version(trade.id, "sl")
        result = await protection_creator.create_sl(trade, self.session)
        if not result.success:
            # v6: cooldown 是速率限制，不是致命错误
            if "cooldown" in (result.error or "").lower():
                logger.info(f"[{trade_ex}] SL替换冷却中 {trade.pair}，等待下次轮询: {result.error}")
                return True  # 非致命，trade.stop_loss 已在 DB 中更新
            logger.error(f"[{trade_ex}] ProtectionCreator.create_sl 失败 {trade.pair}: {result.error}")
            return False

        logger.info(f"[{trade_ex}] SL 替换成功 {trade.pair} algoId={result.algo_id} (旧单已全撤)")
        return True

    async def _place_new_sl(self, trade: Trade, new_sl: float, real_contracts: float) -> bool:
        """
        直接挂新止损单（基于 OKX 实时持仓）。

        v4: 统一走 ProtectionCreator。
        v6: cooldown 失败不视为致命错误。
        """
        from core.protection_creator import protection_creator
        result = await protection_creator.create_sl(trade, self.session)
        if result.success:
            logger.info(f"[{trade.exchange}] 新SL挂载成功 {trade.pair} algoId={result.algo_id}")
            return True
        # v6: cooldown 是速率限制，不是致命错误
        if "cooldown" in (result.error or "").lower():
            logger.info(f"[{trade.exchange}] 新SL冷却中 {trade.pair}，等待下次轮询: {result.error}")
            return True  # 非致命，trade.stop_loss 已在 DB 中更新
        logger.error(f"[{trade.exchange}] ProtectionCreator.create_sl 失败 {trade.pair}: {result.error}")
        return False

    async def _emergency_protect(self, trade: Trade):
        """紧急保护：止损挂载失败时，市价平仓（比裸仓安全）"""
        logger.critical(f"[{trade.exchange}] 紧急保护 {trade.pair} — 止损挂载失败，市价全平")
        from exchange_engine.trade_executor import execute_trade_exit
        ok = await execute_trade_exit(trade, self.session, exit_reason="emergency_protect", ordertype="market")
        if not ok:
            # 平仓未确认 → 仓位仍开放：不强制标记关闭，等修复/人工介入
            logger.critical(
                f"[{trade.exchange}] 紧急保护失败 {trade.pair} — 市价平仓未确认，"
                f"仓位仍开放，需人工介入"
            )

    async def place_exchange_sl(self, trade: Trade) -> bool:
        """
        在交易所挂载止损单（用于 recovery 等场景）。

        ===== OKX 真相源 =====
        挂载前先获取 OKX 实时持仓。

        v4: 统一走 ProtectionCreator。
        v6: 增加 clOrdId version bump + cooldown 非致命处理。
        ======================
        """
        from core.protection_creator import protection_creator

        trade_ex = trade.exchange or "okx"

        # 获取实时持仓
        real_contracts = await self._get_real_contracts(trade, "place_exchange_sl")
        if real_contracts is None or real_contracts <= 0:
            logger.warning(f"[{trade_ex}] place_exchange_sl 跳过：{trade.pair} 无实时仓位")
            return False

        sl_amt = _norm_amt(trade.pair, real_contracts, trade_ex)
        if sl_amt is None or sl_amt <= 0:
            logger.warning(f"[{trade_ex}] place_exchange_sl 数量={sl_amt}，跳过")
            return False

        if trade.stop_loss <= 0:
            return False

        # v6: Bump clOrdId version for deliberate replacement
        protection_creator.bump_clordid_version(trade.id, "sl")
        result = await protection_creator.create_sl(trade, self.session)
        if result.success:
            self.session.commit()
            logger.info(
                f"[{trade_ex}] 止损挂载 {trade.pair} @ {trade.stop_loss} "
                f"algoId={result.algo_id} 数量={sl_amt} (基于OKX持仓 {real_contracts}张)"
            )
            return True
        else:
            # v6: cooldown 是速率限制，不是致命错误
            if "cooldown" in (result.error or "").lower():
                logger.info(f"[{trade_ex}] 止损挂载冷却中 {trade.pair}，等待下次轮询: {result.error}")
                return True
            logger.error(f"[{trade_ex}] 止损挂载失败 {trade.pair}: {result.error}")
            return False
